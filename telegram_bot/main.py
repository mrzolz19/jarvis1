import json
import logging
import tempfile
import time
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import ffmpeg
import requests
import telebot
from dotenv import load_dotenv
from groq import Groq


# Убираем шумное предупреждение из упакованного torch-модуля TTS.
warnings.filterwarnings(
    "ignore",
    category=SyntaxWarning,
    message=r".*invalid escape sequence '\\\^'.*",
    module=r".*multi_acc_v3_package.*",
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


BASE_DIR = Path(__file__).resolve().parent
SESSIONS_FILE = BASE_DIR / "user_sessions.json"
DEFAULT_SESSION_TITLE = "Без названия"

UNINTELLIGIBLE_VOICE_MESSAGE = (
    "Голосовое сообщение не содержит разборчивой речи. "
    "Пожалуйста, повторите команду четче."
)

# Пороги качества распознавания речи Groq Whisper.
NO_SPEECH_PROB_THRESHOLD = 0.6
LOW_CONFIDENCE_AVG_LOGPROB_THRESHOLD = -1.0
NO_SPEECH_WITH_LOW_CONFIDENCE_THRESHOLD = 0.45
NO_SPEECH_SEGMENT_RATIO_THRESHOLD = 0.8


@dataclass(frozen=True)
class AppConfig:
    """Настройки приложения из .env."""

    bot_token: str
    webhook_n8n: str
    owner_id: int
    n8n_connect_timeout: float
    n8n_read_timeout: float
    n8n_retry_attempts: int
    n8n_retry_delay_seconds: float

    @classmethod
    def from_env(cls) -> "AppConfig":
        bot_token = _get_required_env("BOT_TELEGRAM_API")
        webhook_n8n = _get_required_env("WEBHOOK_N8N")
        owner_id_raw = _get_required_env("OWNER_ID")

        try:
            owner_id = int(owner_id_raw)
        except ValueError as exc:
            raise ValueError("OWNER_ID должен быть числом (Telegram user id)") from exc

        return cls(
            bot_token=bot_token,
            webhook_n8n=webhook_n8n,
            owner_id=owner_id,
            n8n_connect_timeout=float(_get_env("N8N_CONNECT_TIMEOUT", "10")),
            n8n_read_timeout=float(_get_env("N8N_READ_TIMEOUT", "120")),
            n8n_retry_attempts=int(_get_env("N8N_RETRY_ATTEMPTS", "2")),
            n8n_retry_delay_seconds=float(
                _get_env("N8N_RETRY_DELAY_SECONDS", "1.5")
            ),
        )


class SessionStorage:
    """Хранит пользовательские сессии и активную сессию Telegram-пользователя."""

    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.sessions: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        if not self.file_path.exists():
            self.sessions = {}
            return

        try:
            with self.file_path.open("r", encoding="utf-8") as file:
                self.sessions = json.load(file)
            self._migrate_legacy_history()
            logger.info("Загружены сессии для %s пользователей", len(self.sessions))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Ошибка загрузки сессий: %s. Начинаем с чистого листа.", exc)
            self.sessions = {}

    def save(self) -> None:
        try:
            with self.file_path.open("w", encoding="utf-8") as file:
                json.dump(self.sessions, file, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.error("Ошибка сохранения сессий: %s", exc)

    def ensure_current_session(self, user_id: int, initial_text: str = "") -> str:
        user_state = self.sessions.get(str(user_id))
        if self._has_valid_current_session(user_state):
            return str(user_state["current"])

        title = generate_title(initial_text) if initial_text else DEFAULT_SESSION_TITLE
        return self.create_session(user_id, title=title)

    def create_session(self, user_id: int, title: str = DEFAULT_SESSION_TITLE) -> str:
        session_id = str(uuid.uuid4())
        user_state = self.sessions.setdefault(
            str(user_id),
            {"current": session_id, "sessions": {}},
        )

        user_state.setdefault("sessions", {})
        user_state["sessions"][session_id] = {"title": title, "messages": 0}
        user_state["current"] = session_id
        self.save()
        return session_id

    def set_current_session(self, user_id: int, session_id: str) -> bool:
        user_state = self.sessions.get(str(user_id))
        if not self._session_exists(user_state, session_id):
            return False

        user_state["current"] = session_id
        self.save()
        return True

    def delete_session(self, user_id: int, session_id: str) -> bool:
        user_state = self.sessions.get(str(user_id))
        if not self._session_exists(user_state, session_id):
            return False

        del user_state["sessions"][session_id]
        if user_state.get("current") == session_id:
            user_state["current"] = self._last_session_id(user_state)

        self.save()
        return True

    def increment_message_count(self, user_id: int, session_id: str) -> None:
        session = self.get_user_sessions(user_id).get(session_id)
        if not session:
            return

        session["messages"] = int(session.get("messages", 0)) + 1
        self.save()

    def update_empty_title(self, user_id: int, session_id: str, text: str) -> None:
        session = self.get_user_sessions(user_id).get(session_id)
        if not session or session.get("title") != DEFAULT_SESSION_TITLE:
            return

        session["title"] = generate_title(text)
        self.save()

    def get_user_sessions(self, user_id: int) -> dict[str, dict[str, Any]]:
        user_state = self.sessions.get(str(user_id), {})
        return user_state.get("sessions", {})

    def _migrate_legacy_history(self) -> None:
        """Поддержка старого формата, где был список history."""
        migrated = False
        for user_state in self.sessions.values():
            if "history" not in user_state:
                continue

            user_state["sessions"] = {
                session_id: {"title": DEFAULT_SESSION_TITLE, "messages": 0}
                for session_id in user_state["history"]
            }
            del user_state["history"]
            migrated = True

        if migrated:
            self.save()

    @staticmethod
    def _has_valid_current_session(user_state: dict[str, Any] | None) -> bool:
        if not user_state:
            return False

        current_session = user_state.get("current")
        return bool(
            current_session
            and current_session in user_state.get("sessions", {})
        )

    @staticmethod
    def _session_exists(user_state: dict[str, Any] | None, session_id: str) -> bool:
        return bool(user_state and session_id in user_state.get("sessions", {}))

    @staticmethod
    def _last_session_id(user_state: dict[str, Any]) -> str:
        sessions = user_state.get("sessions", {})
        return next(reversed(sessions), "") if sessions else ""


class N8nClient:
    """Клиент для отправки сообщений в workflow n8n."""

    def __init__(self, config: AppConfig) -> None:
        self.webhook_url = config.webhook_n8n
        self.connect_timeout = config.n8n_connect_timeout
        self.read_timeout = config.n8n_read_timeout
        self.retry_attempts = config.n8n_retry_attempts
        self.retry_delay_seconds = config.n8n_retry_delay_seconds

    def get_ai_response(self, user_message: str, session_id: str) -> str:
        payload = {
            "action": "sendMessage",
            "chatInput": user_message,
            "sessionId": session_id,
        }

        try:
            return self._request_output(self.webhook_url, payload)
        except requests.exceptions.SSLError as exc:
            return self._handle_ssl_error(exc, payload)
        except requests.exceptions.ReadTimeout as exc:
            logger.error("Таймаут чтения ответа от n8n: %s", exc)
            return (
                "Сервис n8n отвечает слишком долго. Попробуйте ещё раз через минуту "
                "или уменьшите сложность запроса."
            )
        except requests.exceptions.RequestException as exc:
            logger.error("Ошибка запроса к n8n: %s", exc)
            return "Извините, произошла ошибка при обработке запроса."

    def _request_output(self, url: str, payload: dict[str, str]) -> str:
        response_json = self._post(url, payload)
        output = (
            response_json.get("output") if isinstance(response_json, dict) else None
        )

        if output is None:
            logger.warning("Ответ n8n без поля output: %s", response_json)
            return "Запрос обработан, но формат ответа n8n отличается от ожидаемого."

        logger.info("Ответ n8n: %s", output)
        return str(output)

    def _post(self, url: str, payload: dict[str, str]) -> dict[str, Any]:
        total_attempts = max(1, self.retry_attempts + 1)

        for attempt in range(1, total_attempts + 1):
            try:
                response = requests.post(
                    url,
                    json=payload,
                    timeout=(self.connect_timeout, self.read_timeout),
                )
                response.raise_for_status()
                return response.json()
            except (
                requests.exceptions.ConnectTimeout,
                requests.exceptions.ReadTimeout,
            ) as exc:
                if attempt == total_attempts:
                    raise

                logger.warning(
                    "Таймаут запроса к n8n (попытка %s/%s), повтор через %.1fс: %s",
                    attempt,
                    total_attempts,
                    self.retry_delay_seconds,
                    exc,
                )
                time.sleep(self.retry_delay_seconds)

        raise requests.exceptions.RequestException("Не удалось получить ответ от n8n")

    def _handle_ssl_error(
        self,
        error: requests.exceptions.SSLError,
        payload: dict[str, str],
    ) -> str:
        # Частый кейс: endpoint работает по HTTP, а в .env указан HTTPS.
        if "WRONG_VERSION_NUMBER" not in str(error).upper():
            logger.error("SSL ошибка при запросе к n8n: %s", error)
            return "Извините, произошла ошибка при обработке запроса."

        if not self.webhook_url.lower().startswith("https://"):
            logger.error("SSL ошибка при запросе к n8n: %s", error)
            return "Извините, произошла ошибка при обработке запроса."

        fallback_url = self._http_fallback_url(self.webhook_url)
        try:
            return self._request_output(fallback_url, payload)
        except requests.exceptions.RequestException as exc:
            logger.error("Ошибка запроса к n8n (fallback HTTP): %s", exc)
            return "Извините, произошла ошибка при обработке запроса."

    @staticmethod
    def _http_fallback_url(url: str) -> str:
        parsed_url = urlparse(url)
        return urlunparse(parsed_url._replace(scheme="http"))


class VoiceService:
    """Распознает голосовые сообщения и генерирует голосовые ответы."""

    def __init__(self) -> None:
        self._speaker = None

    def synthesize_to_ogg(self, text: str) -> Path:
        speaker = self._get_speaker()

        with tempfile.TemporaryDirectory(dir=BASE_DIR) as temp_dir:
            temp_path = Path(temp_dir)
            speaker.to_mp3(
                text=str(text),
                name_text="response",
                sample_rate=48000,
                audio_dir=str(temp_path),
                put_accent=True,
                put_yo=True,
            )

            mp3_path = self._find_generated_mp3(temp_path)
            ogg_path = BASE_DIR / f"voice_response_{uuid.uuid4().hex}.ogg"
            ffmpeg.input(str(mp3_path)).output(
                str(ogg_path),
                format="ogg",
            ).overwrite_output().run(quiet=True)
            return ogg_path

    def transcribe_telegram_voice(self, voice_bytes: bytes) -> Any:
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as temp_dir:
            temp_path = Path(temp_dir)
            input_ogg = temp_path / "input.ogg"
            output_wav = temp_path / "output.wav"

            input_ogg.write_bytes(voice_bytes)
            ffmpeg.input(str(input_ogg)).output(
                str(output_wav),
                format="wav",
            ).overwrite_output().run(quiet=True)

            client = Groq()
            with output_wav.open("rb") as audio_file:
                return client.audio.transcriptions.create(
                    file=(str(output_wav), audio_file.read()),
                    model="whisper-large-v3",
                    temperature=0,
                    response_format="verbose_json",
                )

    def _get_speaker(self) -> Any:
        # Импорт тяжелой TTS-библиотеки откладываем до первого голосового ответа.
        if self._speaker is None:
            from speakerpy.lib_speak import Speaker

            self._speaker = Speaker(
                model_id="v5_1_ru",
                language="ru",
                speaker="aidar",
                device="cpu",
            )
        return self._speaker

    @staticmethod
    def _find_generated_mp3(directory: Path) -> Path:
        mp3_files = list(directory.glob("out_response*.mp3"))
        if not mp3_files:
            raise FileNotFoundError("Сгенерированный MP3 файл не найден")
        return mp3_files[0]


def _get_env(name: str, default: str) -> str:
    return str(_value or default) if (_value := _read_env(name)) else default


def _get_required_env(name: str) -> str:
    value = _read_env(name)
    if not value:
        raise ValueError(f"Не задан {name} в .env")
    return value


def _read_env(name: str) -> str:
    import os

    return os.getenv(name, "").strip()


def generate_title(text: str) -> str:
    """Создает короткое название сессии через Groq, а при ошибке берет первые слова."""
    stripped_text = text.strip()
    if not stripped_text:
        return DEFAULT_SESSION_TITLE

    try:
        client = Groq()
        response = client.chat.completions.create(
            model="llama3-8b-8192",
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Опиши этот запрос в 2-4 словах, очень кратко, "
                        f"без кавычек и точек: {stripped_text}"
                    ),
                }
            ],
            max_tokens=15,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip().replace('"', "")
    except Exception as exc:
        logger.warning("Не удалось сгенерировать название сессии: %s", exc)
        words = stripped_text.split()
        return " ".join(words[:4]) + "..." if len(words) > 4 else stripped_text


def metadata_value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def is_unintelligible_transcription(transcription: Any) -> bool:
    """Проверяет, похоже ли распознавание на шум или неразборчивую речь."""
    recognized_text = (metadata_value(transcription, "text", "") or "").strip()
    if not recognized_text:
        return True

    segments = metadata_value(transcription, "segments", None) or []
    if not segments:
        return False

    no_speech_probs = _collect_float_metadata(segments, "no_speech_prob")
    avg_logprobs = _collect_float_metadata(segments, "avg_logprob")
    if not no_speech_probs and not avg_logprobs:
        return False

    mean_no_speech_prob = _mean(no_speech_probs)
    mean_avg_logprob = _mean(avg_logprobs)
    high_no_speech_ratio = _ratio_at_least(
        no_speech_probs,
        NO_SPEECH_PROB_THRESHOLD,
    )

    return (
        mean_no_speech_prob >= NO_SPEECH_PROB_THRESHOLD
        or high_no_speech_ratio >= NO_SPEECH_SEGMENT_RATIO_THRESHOLD
        or mean_avg_logprob <= LOW_CONFIDENCE_AVG_LOGPROB_THRESHOLD
        or (
            mean_no_speech_prob >= NO_SPEECH_WITH_LOW_CONFIDENCE_THRESHOLD
            and mean_avg_logprob <= -0.7
        )
    )


def _collect_float_metadata(items: list[Any], key: str) -> list[float]:
    values = []
    for item in items:
        value = metadata_value(item, key)
        if value is not None:
            values.append(float(value))
    return values


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _ratio_at_least(values: list[float], threshold: float) -> float:
    if not values:
        return 0.0
    return sum(1 for value in values if value >= threshold) / len(values)


def parse_command(text: str) -> tuple[str, str]:
    command_parts = text.split(maxsplit=1)
    command = command_parts[0].lower()
    command_arg = command_parts[1].strip() if len(command_parts) > 1 else ""
    return command, command_arg


def build_sessions_message(
    sessions: dict[str, dict[str, Any]],
    current_session: str,
) -> str:
    formatted_sessions = []
    for session_id, meta in list(sessions.items())[-10:]:
        marker = " (активна)" if session_id == current_session else ""
        title = meta.get("title", DEFAULT_SESSION_TITLE)
        messages = meta.get("messages", 0)
        formatted_sessions.append(
            f"- {title} ({messages} сообщ.)\n  ID: {session_id}{marker}"
        )

    return "Последние сессии:\n\n" + "\n\n".join(formatted_sessions)


class TelegramAssistantBot:
    """Связывает Telegram, n8n, сессии и голосовые функции."""

    def __init__(
        self,
        config: AppConfig,
        bot: telebot.TeleBot,
        sessions: SessionStorage,
        n8n_client: N8nClient,
        voice_service: VoiceService,
    ) -> None:
        self.config = config
        self.bot = bot
        self.sessions = sessions
        self.n8n_client = n8n_client
        self.voice_service = voice_service

    def process_text_message(self, message: Any) -> None:
        if not self._is_owner(message):
            return

        text = (message.text or "").strip()
        if not text:
            return

        command, command_arg = parse_command(text)
        if command.startswith("/") and self._handle_command(
            message,
            command,
            command_arg,
        ):
            return

        self._process_user_request(message.from_user.id, text)

    def process_voice_message(self, message: Any) -> None:
        if not self._is_owner(message):
            return

        try:
            file_info = self.bot.get_file(message.voice.file_id)
            voice_bytes = self.bot.download_file(file_info.file_path)
            transcription = self.voice_service.transcribe_telegram_voice(voice_bytes)
            recognized_text = metadata_value(transcription, "text", "").strip()
        except Exception as exc:
            logger.exception("Ошибка обработки голосового сообщения: %s", exc)
            self.bot.send_message(
                message.from_user.id,
                "Не удалось обработать голосовое сообщение.",
            )
            return

        if is_unintelligible_transcription(transcription):
            self.bot.send_message(message.from_user.id, UNINTELLIGIBLE_VOICE_MESSAGE)
            self._send_voice_answer(
                message.from_user.id,
                UNINTELLIGIBLE_VOICE_MESSAGE,
            )
            return

        self._process_user_request(message.from_user.id, recognized_text)

    def _handle_command(self, message: Any, command: str, command_arg: str) -> bool:
        user_id = message.from_user.id

        if command in {"/new", "/newsession"}:
            session_id = self.sessions.create_session(user_id)
            self.bot.send_message(
                user_id,
                f"Создана новая сессия.\nSession ID: {session_id}",
            )
            return True

        if command in {"/del_session", "/rmsession"}:
            self._handle_delete_session(user_id, command_arg)
            return True

        if command == "/sessions":
            self._handle_sessions_list(user_id)
            return True

        if command == "/session":
            self._handle_current_session(user_id)
            return True

        if command == "/switch":
            self._handle_switch_session(user_id, command_arg)
            return True

        if command == "/help":
            self._send_help(user_id)
            return True

        return False

    def _handle_delete_session(self, user_id: int, session_id: str) -> None:
        if not session_id:
            self.bot.send_message(user_id, "Использование: /rmsession <session_id>")
            return

        if self.sessions.delete_session(user_id, session_id):
            self.bot.send_message(user_id, f"Сессия {session_id} удалена.")
        else:
            self.bot.send_message(user_id, "Сессия не найдена.")

    def _handle_sessions_list(self, user_id: int) -> None:
        sessions = self.sessions.get_user_sessions(user_id)
        if not sessions:
            self.bot.send_message(user_id, "Сессий пока нет.")
            return

        current_session = self.sessions.ensure_current_session(user_id)
        self.bot.send_message(
            user_id,
            build_sessions_message(sessions, current_session),
        )

    def _handle_current_session(self, user_id: int) -> None:
        current_session = self.sessions.ensure_current_session(user_id)
        session = self.sessions.get_user_sessions(user_id).get(current_session, {})
        title = session.get("title", DEFAULT_SESSION_TITLE)
        messages = session.get("messages", 0)

        self.bot.send_message(
            user_id,
            (
                "Текущая сессия:\n"
                f"Название: {title} ({messages} сообщ.)\n"
                f"ID: {current_session}"
            ),
        )

    def _handle_switch_session(self, user_id: int, session_id: str) -> None:
        if not session_id:
            self.bot.send_message(user_id, "Использование: /switch <session_id>")
            return

        if self.sessions.set_current_session(user_id, session_id):
            self.bot.send_message(
                user_id,
                f"Активная сессия переключена на:\n{session_id}",
            )
        else:
            self.bot.send_message(
                user_id,
                "Не удалось переключить сессию. Проверьте ID и команду /sessions.",
            )

    def _send_help(self, user_id: int) -> None:
        self.bot.send_message(
            user_id,
            "Команды:\n"
            "/newsession или /new - создать новую сессию\n"
            "/session - показать активную сессию\n"
            "/sessions - список последних сессий\n"
            "/switch <session_id> - переключиться на сессию\n"
            "/rmsession <session_id> - удалить сессию",
        )

    def _process_user_request(self, user_id: int, text: str) -> None:
        session_id = self.sessions.ensure_current_session(user_id, initial_text=text)
        self.sessions.increment_message_count(user_id, session_id)
        self.sessions.update_empty_title(user_id, session_id, text)

        ai_reply_text = self.n8n_client.get_ai_response(text, session_id)
        self.bot.send_message(user_id, ai_reply_text)
        self._send_voice_answer(user_id, ai_reply_text)

    def _send_voice_answer(self, user_id: int, text: str) -> None:
        try:
            ogg_path = self.voice_service.synthesize_to_ogg(text)
            with ogg_path.open("rb") as audio:
                self.bot.send_voice(user_id, audio)
        except Exception as exc:
            logger.exception("Ошибка отправки голосового ответа: %s", exc)
        finally:
            if "ogg_path" in locals():
                ogg_path.unlink(missing_ok=True)

    def _is_owner(self, message: Any) -> bool:
        return message.from_user.id == self.config.owner_id


config = AppConfig.from_env()
telegram_bot = telebot.TeleBot(config.bot_token)
session_storage = SessionStorage(SESSIONS_FILE)
assistant = TelegramAssistantBot(
    config=config,
    bot=telegram_bot,
    sessions=session_storage,
    n8n_client=N8nClient(config),
    voice_service=VoiceService(),
)


@telegram_bot.message_handler(content_types=["text"])
def text_processing(message: Any) -> None:
    assistant.process_text_message(message)


@telegram_bot.message_handler(content_types=["voice"])
def audio_processing(message: Any) -> None:
    assistant.process_voice_message(message)


if __name__ == "__main__":
    telegram_bot.polling(none_stop=True, interval=0)
