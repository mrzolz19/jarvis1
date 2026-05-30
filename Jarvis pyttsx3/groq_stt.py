from typing import Any

from groq import Groq


NO_SPEECH_PROB_THRESHOLD = 0.6
LOW_CONFIDENCE_AVG_LOGPROB_THRESHOLD = -1.0
NO_SPEECH_WITH_LOW_CONFIDENCE_THRESHOLD = 0.45
NO_SPEECH_SEGMENT_RATIO_THRESHOLD = 0.8


class UnintelligibleSpeechError(Exception):
    """Raised when Whisper returns empty text or low-confidence speech."""


class GroqSpeechRecognizer:
    """Transcribes microphone audio through Groq Whisper."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "whisper-large-v3",
    ) -> None:
        self._api_key = api_key
        self._client: Groq | None = None
        self._model = model

    def recognize(self, audio: Any) -> str:
        wav_bytes = audio.get_wav_data(convert_rate=16000, convert_width=2)
        transcription = self._get_client().audio.transcriptions.create(
            file=("microphone.wav", wav_bytes),
            model=self._model,
            temperature=0,
            response_format="verbose_json",
        )

        if is_unintelligible_transcription(transcription):
            raise UnintelligibleSpeechError("Речь не распознана или похожа на шум")

        return str(metadata_value(transcription, "text", "")).strip()

    def _get_client(self) -> Groq:
        if self._client is None:
            self._client = Groq(api_key=self._api_key) if self._api_key else Groq()
        return self._client


def metadata_value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def is_unintelligible_transcription(transcription: Any) -> bool:
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
