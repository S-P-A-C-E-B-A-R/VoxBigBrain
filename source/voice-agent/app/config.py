import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    whisper_base_url: str = os.environ["WHISPER_BASE_URL"]
    whisper_model: str = os.environ["WHISPER_MODEL"]
    whisper_streaming_enabled: bool = os.getenv("WHISPER_STREAMING_ENABLED", "true").lower() == "true"
    whisper_ws_url: str = os.getenv("WHISPER_WS_URL", "ws://whisper:8000/v1/audio/transcriptions")
    whisper_language: str = os.getenv("WHISPER_LANGUAGE", "en")
    whisper_temperature: float = float(os.getenv("WHISPER_TEMPERATURE", "0.0"))
    whisper_vad_filter: bool = os.getenv("WHISPER_VAD_FILTER", "false").lower() == "true"
    whisper_ws_finalization_seconds: float = float(os.getenv("WHISPER_WS_FINALIZATION_SECONDS", "60"))
    kokoro_base_url: str = os.environ["KOKORO_BASE_URL"]
    kokoro_model: str = os.environ["KOKORO_MODEL"]
    kokoro_voice: str = os.environ["KOKORO_VOICE"]
    qwen_base_url: str = os.environ["QWEN_BASE_URL"]
    qwen_api_key: str = os.environ["QWEN_API_KEY"]
    qwen_model: str = os.environ["QWEN_MODEL"]
    kagi_api_key: str = os.environ["KAGI_API_KEY"]
    timezone: str = os.environ.get("LOCAL_TIMEZONE", "America/New_York")
    vad_activation_threshold: float = float(os.getenv("VAD_ACTIVATION_THRESHOLD", "0.65"))
    vad_min_speech_duration: float = float(os.getenv("VAD_MIN_SPEECH_DURATION", "0.20"))
    vad_min_silence_duration: float = float(os.getenv("VAD_MIN_SILENCE_DURATION", "0.50"))
    vad_prefix_padding_duration: float = float(os.getenv("VAD_PREFIX_PADDING_DURATION", "0.20"))
    interruption_min_duration: float = float(os.getenv("INTERRUPTION_MIN_DURATION", "0.40"))
    interruption_min_words: int = int(os.getenv("INTERRUPTION_MIN_WORDS", "0"))
    false_interruption_timeout: float = float(os.getenv("FALSE_INTERRUPTION_TIMEOUT", "0.80"))
    interruption_transcription_settle_seconds: float = float(os.getenv("INTERRUPTION_TRANSCRIPTION_SETTLE_SECONDS", "60.0"))
    interruption_confirm_final_only: bool = os.getenv("INTERRUPTION_CONFIRM_FINAL_ONLY", "true").lower() == "true"
    web_internal_url: str = os.getenv("WEB_INTERNAL_URL", "http://web-ui:8080")
    internal_agent_secret: str = os.environ["INTERNAL_AGENT_SECRET"]


config = Config()
