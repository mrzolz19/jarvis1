import glob
import json
import os
import time
import uuid
import warnings
from os import path
from urllib.parse import urlparse, urlunparse

import ffmpeg
import requests
import telebot
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

# Suppress noisy warning from packaged torch module used by TTS model.
warnings.filterwarnings(
    "ignore",
    category=SyntaxWarning,
    message=r".*invalid escape sequence '\\\^'.*",
    module=r".*multi_acc_v3_package.*",
)

bot_token = os.getenv("BOT_TELEGRAM_API")
if not bot_token:
    raise ValueError("Не задан BOT_TELEGRAM_API в .env")

bot = telebot.TeleBot(bot_token)  # API Токен бота в телеграмме
webhook_n8n = os.getenv("WEBHOOK_N8N")  # Вставьте сюда URL вашего webhook из n8n
owner_id_raw = os.getenv("OWNER_ID")
N8N_CONNECT_TIMEOUT = float(os.getenv("N8N_CONNECT_TIMEOUT", "10"))
N8N_READ_TIMEOUT = float(os.getenv("N8N_READ_TIMEOUT", "120"))
N8N_RETRY_ATTEMPTS = int(os.getenv("N8N_RETRY_ATTEMPTS", "2"))
N8N_RETRY_DELAY_SECONDS = float(os.getenv("N8N_RETRY_DELAY_SECONDS", "1.5"))
if not owner_id_raw:
    raise ValueError("Не задан OWNER_ID в .env")
if not webhook_n8n:
    raise ValueError("Не задан WEBHOOK_N8N в .env")
try:
    OWNER_ID = int(owner_id_raw)
except ValueError as exc:
    raise ValueError("OWNER_ID должен быть числом (Telegram user id)") from exc

SESSIONS_FILE = "user_sessions.json"
user_sessions = {}


def _load_sessions() -> None:
    global user_sessions
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                user_sessions = json.load(f)
            # Migration
            for uid, data in user_sessions.items():
                if "history" in data:
                    new_sessions = {}
                    for s in data["history"]:
                        new_sessions[s] = {"title": "Без названия", "messages": 0}
                    data["sessions"] = new_sessions
                    del data["history"]
            print(f"Загружены сессии для {len(user_sessions)} пользователей")
        except (json.JSONDecodeError, IOError) as e:
            print(f"Ошибка загрузки сессий: {e}. Начнём с чистого листа.")
            user_sessions = {}
    else:
        user_sessions = {}


def _save_sessions() -> None:
    try:
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(user_sessions, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"Ошибка сохранения сессий: {e}")


_load_sessions()


def _generate_session_id() -> str:
    return str(uuid.uuid4())


def _generate_title(text: str) -> str:
    try:
        client = Groq()
        response = client.chat.completions.create(
            model="llama3-8b-8192",
            messages=[
                {
                    "role": "user",
                    "content": f"Опиши этот запрос в 2-4 словах, очень кратко, без кавычек и точек: {text}",
                }
            ],
            max_tokens=15,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip().replace('"', "")
    except Exception:
        words = text.split()
        return " ".join(words[:4]) + "..." if len(words) > 4 else text


def _ensure_current_session(user_id: int, initial_text: str = "") -> str:
    user_id_str = str(user_id)
    session_state = user_sessions.get(user_id_str)
    if (
        session_state
        and "current" in session_state
        and session_state["current"] in session_state.get("sessions", {})
    ):
        return session_state["current"]

    new_session = _generate_session_id()
    title = _generate_title(initial_text) if initial_text else "Без названия"
    if not session_state:
        user_sessions[user_id_str] = {"current": new_session, "sessions": {}}
    user_sessions[user_id_str]["sessions"][new_session] = {
        "title": title,
        "messages": 0,
    }
    user_sessions[user_id_str]["current"] = new_session
    _save_sessions()
    return new_session


def _create_new_session(user_id: int) -> str:
    new_session = _generate_session_id()
    session_state = user_sessions.setdefault(
        str(user_id), {"current": new_session, "sessions": {}}
    )
    session_state["sessions"][new_session] = {"title": "Без названия", "messages": 0}
    session_state["current"] = new_session
    _save_sessions()
    return new_session


def _set_current_session(user_id: int, requested_session: str) -> bool:
    user_id_str = str(user_id)
    session_state = user_sessions.get(user_id_str)
    if not session_state or "sessions" not in session_state:
        return False

    if requested_session not in session_state["sessions"]:
        return False

    session_state["current"] = requested_session
    _save_sessions()
    return True


def _delete_session(user_id: int, requested_session: str) -> bool:
    user_id_str = str(user_id)
    session_state = user_sessions.get(user_id_str)
    if not session_state or "sessions" not in session_state:
        return False

    if requested_session not in session_state["sessions"]:
        return False

    del session_state["sessions"][requested_session]
    if session_state["current"] == requested_session:
        if session_state["sessions"]:
            session_state["current"] = list(session_state["sessions"].keys())[-1]
        else:
            session_state["current"] = ""
    _save_sessions()
    return True


def _increment_message_count(user_id: int, session_id: str) -> None:
    user_id_str = str(user_id)
    session_state = user_sessions.get(user_id_str)
    if (
        session_state
        and "sessions" in session_state
        and session_id in session_state["sessions"]
    ):
        session_state["sessions"][session_id]["messages"] += 1
        # Update title if it's "Без названия"
        _save_sessions()


def _get_user_sessions(user_id: int) -> dict:
    user_id_str = str(user_id)
    session_state = user_sessions.get(user_id_str)
    if not session_state:
        return {}
    return session_state.get("sessions", {})


def _http_fallback_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        return url
    return urlunparse(parsed._replace(scheme="http"))


def _post_n8n(url: str, data: dict) -> dict:
    total_attempts = max(1, N8N_RETRY_ATTEMPTS + 1)
    for attempt in range(1, total_attempts + 1):
        try:
            response = requests.post(
                url,
                json=data,
                timeout=(N8N_CONNECT_TIMEOUT, N8N_READ_TIMEOUT),
            )
            response.raise_for_status()
            return response.json()
        except (
            requests.exceptions.ReadTimeout,
            requests.exceptions.ConnectTimeout,
        ) as timeout_err:
            if attempt == total_attempts:
                raise timeout_err
            print(
                f"Таймаут запроса к n8n (попытка {attempt}/{total_attempts}), повтор через "
                f"{N8N_RETRY_DELAY_SECONDS}с: {timeout_err}"
            )
            time.sleep(N8N_RETRY_DELAY_SECONDS)


def ai_response(user_message: str, session_id: str) -> str:
    data = {"action": "sendMessage", "chatInput": user_message, "sessionId": session_id}
    try:
        result = _post_n8n(webhook_n8n, data)
        output = result.get("output") if isinstance(result, dict) else None
        if output is None:
            print(f"Ответ n8n без поля output: {result}")
            return "Запрос обработан, но формат ответа n8n отличается от ожидаемого."
        print(output)
        return output

    except requests.exceptions.SSLError as ssl_err:
        # Частый кейс: endpoint работает по HTTP, а в .env указан HTTPS.
        if "WRONG_VERSION_NUMBER" in str(
            ssl_err
        ).upper() and webhook_n8n.lower().startswith("https://"):
            fallback_url = _http_fallback_url(webhook_n8n)
            try:
                result = _post_n8n(fallback_url, data)
                output = result.get("output") if isinstance(result, dict) else None
                if output is None:
                    print(f"Ответ n8n без поля output (fallback HTTP): {result}")
                    return "Запрос обработан, но формат ответа n8n отличается от ожидаемого."
                print(output)
                return output
            except requests.exceptions.RequestException as fallback_err:
                print(f"Ошибка запроса к n8n (fallback HTTP): {fallback_err}")
                return "Извините, произошла ошибка при обработке запроса."
        print(f"SSL ошибка при запросе к n8n: {ssl_err}")
        return "Извините, произошла ошибка при обработке запроса."

    except requests.exceptions.ReadTimeout as timeout_err:
        print(f"Таймаут чтения ответа от n8n: {timeout_err}")
        return (
            "Сервис n8n отвечает слишком долго. Попробуйте ещё раз через минуту "
            "или уменьшите сложность запроса."
        )

    except requests.exceptions.RequestException as e:
        print(f"Ошибка запроса к n8n: {e}")
        return "Извините, произошла ошибка при обработке запроса."


def audio_response(text: str) -> None:
    from speakerpy.lib_speak import Speaker as sp

    speaker = sp(model_id="v5_1_ru", language="ru", speaker="aidar", device="cpu")
    speaker.to_mp3(
        text=str(text),
        name_text="response",
        sample_rate=48000,
        audio_dir=".",
        put_accent=True,
        put_yo=True,
    )

    # Найти сгенерированный файл (имя содержит хеш: out_response<hash>.mp3)
    mp3_files = glob.glob("out_response*.mp3")
    if not mp3_files:
        raise FileNotFoundError("Сгенерированный MP3 файл не найден")
    mp3_file = mp3_files[0]

    ffmpeg.input(mp3_file).output("output.ogg", format="ogg").overwrite_output().run()
    os.remove(mp3_file)


@bot.message_handler(content_types=["text"])
def text_processing(message) -> None:
    if message.from_user.id != OWNER_ID:
        return

    text = (message.text or "").strip()
    if not text:
        return

    command_parts = text.split(maxsplit=1)
    command = command_parts[0].lower()
    command_arg = command_parts[1].strip() if len(command_parts) > 1 else ""

    if command in {"/new", "/newsession"}:
        new_session = _create_new_session(message.from_user.id)
        bot.send_message(
            message.from_user.id,
            f"Создана новая сессия.\nSession ID: {new_session}",
        )
        return

    if command in {"/del_session", "/rmsession"}:
        if not command_arg:
            bot.send_message(
                message.from_user.id,
                "Использование: /rmsession <session_id>",
            )
            return

        if _delete_session(message.from_user.id, command_arg):
            bot.send_message(
                message.from_user.id,
                f"Сессия {command_arg} удалена.",
            )
        else:
            bot.send_message(
                message.from_user.id,
                "Сессия не найдена.",
            )
        return

    if command == "/sessions":
        sessions = _get_user_sessions(message.from_user.id)
        if not sessions:
            bot.send_message(message.from_user.id, "Сессий пока нет.")
            return

        current_session = _ensure_current_session(message.from_user.id)
        formatted = []
        for session_id, meta in list(sessions.items())[-10:]:
            marker = " (активна)" if session_id == current_session else ""
            title = meta.get("title", "Без названия")
            messages = meta.get("messages", 0)
            formatted.append(
                f"- {title} ({messages} сообщ.)\n  ID: {session_id}{marker}"
            )
        bot.send_message(
            message.from_user.id,
            "Последние сессии:\n\n" + "\n\n".join(formatted),
        )
        return

    if command == "/session":
        current_session = _ensure_current_session(message.from_user.id)
        sessions = _get_user_sessions(message.from_user.id)
        meta = sessions.get(current_session, {})
        title = meta.get("title", "Без названия")
        messages = meta.get("messages", 0)
        bot.send_message(
            message.from_user.id,
            f"Текущая сессия:\nНазвание: {title} ({messages} сообщ.)\nID: {current_session}",
        )
        return

    if command == "/switch":
        if not command_arg:
            bot.send_message(
                message.from_user.id,
                "Использование: /switch <session_id>",
            )
            return

        requested_session = command_arg
        if _set_current_session(message.from_user.id, requested_session):
            bot.send_message(
                message.from_user.id,
                f"Активная сессия переключена на:\n{requested_session}",
            )
        else:
            bot.send_message(
                message.from_user.id,
                "Не удалось переключить сессию. Проверьте ID и команду /sessions.",
            )
        return

    if command == "/help":
        bot.send_message(
            message.from_user.id,
            "Команды:\n"
            "/newsession или /new — создать новую сессию\n"
            "/session — показать активную сессию\n"
            "/sessions — список последних сессий\n"
            "/switch <session_id> — переключиться на сессию\n"
            "/rmsession <session_id> — удалить сессию",
        )
        return

    current_session = _ensure_current_session(message.from_user.id, initial_text=text)
    _increment_message_count(message.from_user.id, current_session)

    # Check if we should update the title
    sessions = _get_user_sessions(message.from_user.id)
    if (
        current_session in sessions
        and sessions[current_session].get("title") == "Без названия"
    ):
        sessions[current_session]["title"] = _generate_title(text)
        _save_sessions()

    ai_reply_text = ai_response(text, current_session)
    bot.send_message(message.from_user.id, ai_reply_text)
    audio_response(ai_reply_text)
    with open("output.ogg", "rb") as audio:
        bot.send_voice(message.from_user.id, audio)
    os.remove("output.ogg")


@bot.message_handler(content_types=["voice"])
def audio_processing(message):
    if message.from_user.id != OWNER_ID:
        return
    voice = message.voice
    print(voice)
    file_id = voice.file_id
    print(file_id)
    file_info = bot.get_file(file_id)
    print(file_info)
    downloaded_file = bot.download_file(file_info.file_path)

    with open("input.ogg", "wb") as file:
        file.write(downloaded_file)
    ffmpeg.input("input.ogg").output(
        "output.wav", format="wav"
    ).overwrite_output().run()
    audio_file = path.join(path.dirname(path.realpath(__file__)), "output.wav")

    client = Groq()

    with open(audio_file, "rb") as file:
        transcription = client.audio.transcriptions.create(
            file=(audio_file, file.read()),
            model="whisper-large-v3",
            temperature=0,
            response_format="verbose_json",
        )
        recognized_text = transcription.text.strip()

    os.remove("input.ogg")
    os.remove("output.wav")

    if not recognized_text:
        fallback_msg = "Голосовое сообщение не содержит разборчивой речи. Пожалуйста, повторите команду четче."
        bot.send_message(message.from_user.id, fallback_msg)
        audio_response(fallback_msg)
        try:
            with open("output.ogg", "rb") as audio:
                bot.send_voice(message.from_user.id, audio)
            os.remove("output.ogg")
        except Exception as e:
            print(f"Failed to send fallback audio: {e}")
        return

    # Отправляем распознанный текст через AI и возвращаем ответ пользователю
    current_session = _ensure_current_session(
        message.from_user.id, initial_text=recognized_text
    )
    _increment_message_count(message.from_user.id, current_session)

    # Check if we should update the title
    sessions = _get_user_sessions(message.from_user.id)
    if (
        current_session in sessions
        and sessions[current_session].get("title") == "Без названия"
    ):
        sessions[current_session]["title"] = _generate_title(recognized_text)
        _save_sessions()

    ai_reply_text = ai_response(recognized_text, current_session)
    bot.send_message(message.from_user.id, ai_reply_text)
    audio_response(ai_reply_text)
    with open("output.ogg", "rb") as audio:
        bot.send_voice(message.from_user.id, audio)
    os.remove("output.ogg")


bot.polling(none_stop=True, interval=0)
