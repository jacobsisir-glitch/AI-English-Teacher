import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - local fallback for missing optional dependency
    load_dotenv = None


def _get_int_list_env(name: str, default: str) -> list[int]:
    raw = os.getenv(name, default).strip()
    values = [segment.strip() for segment in raw.split(",") if segment.strip()]
    try:
        parsed = [int(segment) for segment in values]
    except ValueError as exc:  # pragma: no cover - config validation
        raise ValueError(f"{name} must be a comma-separated list of integers, got {raw!r}.") from exc
    if len(parsed) != 3:
        raise ValueError(f"{name} must contain exactly 3 integers, got {raw!r}.")
    return parsed


BASE_DIR = Path(__file__).resolve().parent
if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEFAULT_STUDENT_ID = os.getenv("DEFAULT_STUDENT_ID", "TestUser")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{(DATA_DIR / 'ai_teacher.db').as_posix()}",
)

LIVEKIT_WS_URL = os.getenv("LIVEKIT_WS_URL", "").strip()
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "").strip()
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "").strip()
VOICE_DEFAULT_ROOM = os.getenv("VOICE_DEFAULT_ROOM", "ai-teacher-room").strip() or "ai-teacher-room"
VOICE_CONVERSATION_PROVIDER = os.getenv("VOICE_CONVERSATION_PROVIDER", "legacy").strip().lower() or "legacy"
TTS_ENABLED = str(os.getenv("TTS_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "doubao").strip().lower() or "doubao"
TTS_BASE_URL = os.getenv("TTS_BASE_URL", "http://127.0.0.1:8012").strip() or "http://127.0.0.1:8012"
TTS_DEFAULT_VOICE = os.getenv("TTS_DEFAULT_VOICE", "melo_teacher").strip() or "melo_teacher"
TTS_DEFAULT_LANG = os.getenv("TTS_DEFAULT_LANG", "zh").strip() or "zh"
TTS_DEFAULT_SPEED = float(os.getenv("TTS_DEFAULT_SPEED", "1.0") or "1.0")
TTS_TIMEOUT_SECONDS = float(os.getenv("TTS_TIMEOUT_SECONDS", "120") or "120")
DOUBAO_APP_ID = os.getenv("DOUBAO_APP_ID", "").strip()
DOUBAO_ACCESS_TOKEN = os.getenv("DOUBAO_ACCESS_TOKEN", "").strip()
DOUBAO_AUTH_MODE = os.getenv("DOUBAO_AUTH_MODE", "api_key").strip().lower() or "api_key"
DOUBAO_API_KEY = os.getenv("DOUBAO_API_KEY", "").strip()
DOUBAO_CLUSTER = os.getenv("DOUBAO_CLUSTER", "volcano_tts").strip() or "volcano_tts"
DOUBAO_TTS_API_VERSION = os.getenv("DOUBAO_TTS_API_VERSION", "v3").strip().lower() or "v3"
DOUBAO_TTS_V3_ENDPOINT = (
    os.getenv("DOUBAO_TTS_V3_ENDPOINT", "wss://openspeech.bytedance.com/api/v3/tts/bidirection").strip()
    or "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
)
DOUBAO_TTS_VOICE = os.getenv("DOUBAO_TTS_VOICE", "").strip()
DOUBAO_TTS_RESOURCE_ID = os.getenv("DOUBAO_TTS_RESOURCE_ID", "seed-tts-2.0").strip() or "seed-tts-2.0"
DOUBAO_TTS_RESOURCE_ID_HEADER = os.getenv("DOUBAO_TTS_RESOURCE_ID_HEADER", "X-Api-Resource-Id").strip() or "X-Api-Resource-Id"
DOUBAO_TTS_VOICE_TYPE = os.getenv("DOUBAO_TTS_VOICE_TYPE", "S_D9gzp4Q12").strip() or "S_D9gzp4Q12"
DOUBAO_TTS_SPEAKER_ID = os.getenv("DOUBAO_TTS_SPEAKER_ID", "").strip()
DOUBAO_TTS_MODEL = os.getenv("DOUBAO_TTS_MODEL", "").strip()
DOUBAO_TTS_ENABLE_STREAM = str(os.getenv("DOUBAO_TTS_ENABLE_STREAM", "true")).strip().lower() in {"1", "true", "yes", "on"}
TTS_FALLBACK_ON_ERROR = str(os.getenv("TTS_FALLBACK_ON_ERROR", "false")).strip().lower() in {"1", "true", "yes", "on"}
FUNASR_WS_URL = os.getenv("FUNASR_WS_URL", "").strip()
FUNASR_MODE = os.getenv("FUNASR_MODE", "2pass").strip() or "2pass"
FUNASR_MODEL_NAME = os.getenv("FUNASR_MODEL_NAME", "").strip()
FUNASR_CHUNK_SIZE = _get_int_list_env("FUNASR_CHUNK_SIZE", "5,8,4")
FUNASR_CHUNK_INTERVAL = int(os.getenv("FUNASR_CHUNK_INTERVAL", "8") or "8")
FUNASR_FINAL_WAIT_OFFLINE_MS = int(os.getenv("FUNASR_FINAL_WAIT_OFFLINE_MS", "6000") or "6000")
FUNASR_FINAL_WAIT_FALLBACK_MS = int(os.getenv("FUNASR_FINAL_WAIT_FALLBACK_MS", "0") or "0")
FUNASR_FINAL_DRAIN_MS: int
_old_drain_window = os.getenv("FUNASR_FINAL_DRAIN_WINDOW_MS", "").strip()
if _old_drain_window and not os.getenv("FUNASR_FINAL_DRAIN_MS", "").strip():
    import warnings
    warnings.warn(
        "FUNASR_FINAL_DRAIN_WINDOW_MS is deprecated. Use FUNASR_FINAL_DRAIN_MS instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    FUNASR_FINAL_DRAIN_MS = int(_old_drain_window)
else:
    FUNASR_FINAL_DRAIN_MS = int(os.getenv("FUNASR_FINAL_DRAIN_MS", "1000") or "1000")
FUNASR_FINAL_RESCUE_WAIT_MS = int(os.getenv("FUNASR_FINAL_RESCUE_WAIT_MS", "6000") or "6000")
FUNASR_LATE_FINAL_GRACE_MS = int(os.getenv("FUNASR_LATE_FINAL_GRACE_MS", "12000") or "12000")
FUNASR_FALLBACK_MIN_STABLE_MS = int(os.getenv("FUNASR_FALLBACK_MIN_STABLE_MS", "240") or "240")
VOICE_FINAL_ACK_TIMEOUT_MS = int(os.getenv("VOICE_FINAL_ACK_TIMEOUT_MS", "1000") or "1000")
VOICE_FINAL_ACK_RETRY = int(os.getenv("VOICE_FINAL_ACK_RETRY", "2") or "2")
VOICE_STOP_WAIT_FINAL_MS = int(os.getenv("VOICE_STOP_WAIT_FINAL_MS", "8000") or "8000")
SILERO_SAMPLE_RATE = int(os.getenv("SILERO_SAMPLE_RATE", "16000") or "16000")
SILERO_CHANNELS = int(os.getenv("SILERO_CHANNELS", "1") or "1")
SILERO_VAD_THRESHOLD = float(os.getenv("SILERO_VAD_THRESHOLD", "0.38") or "0.38")
SILERO_MIN_SILENCE_MS = int(os.getenv("SILERO_MIN_SILENCE_MS", "1200") or "1200")
SILERO_PRE_SPEECH_MS = int(os.getenv("SILERO_PRE_SPEECH_MS", "480") or "480")
SILERO_MIN_SPEECH_MS = int(os.getenv("SILERO_MIN_SPEECH_MS", "320") or "320")
SILERO_SPEECH_END_HOLD_MS = int(os.getenv("SILERO_SPEECH_END_HOLD_MS", "720") or "720")
VOICE_DEBUG_FORCE_SEGMENT_MODE = str(os.getenv("VOICE_DEBUG_FORCE_SEGMENT_MODE", "false")).strip().lower() in {"1", "true", "yes", "on"}
VOICE_DEBUG_FORCE_SEGMENT_MS = int(os.getenv("VOICE_DEBUG_FORCE_SEGMENT_MS", "3000") or "3000")

QWEN_REALTIME_API_KEY = os.getenv("QWEN_REALTIME_API_KEY", "").strip()
QWEN_REALTIME_WORKSPACE_ID = os.getenv("QWEN_REALTIME_WORKSPACE_ID", "").strip()
QWEN_REALTIME_MODEL = os.getenv("QWEN_REALTIME_MODEL", "qwen3.5-omni-flash-realtime").strip() or "qwen3.5-omni-flash-realtime"
QWEN_REALTIME_REGION = os.getenv("QWEN_REALTIME_REGION", "beijing").strip().lower() or "beijing"
QWEN_REALTIME_BASE_URL = os.getenv("QWEN_REALTIME_BASE_URL", "").strip()
_QWEN_REALTIME_VOICE_RAW = os.getenv("QWEN_REALTIME_VOICE")
QWEN_REALTIME_VOICE = "Tina" if _QWEN_REALTIME_VOICE_RAW is None else _QWEN_REALTIME_VOICE_RAW.strip()
QWEN_REALTIME_VAD_MODE = os.getenv("QWEN_REALTIME_VAD_MODE", "semantic_vad").strip().lower() or "semantic_vad"
QWEN_REALTIME_MAX_SESSION_MINUTES = int(os.getenv("QWEN_REALTIME_MAX_SESSION_MINUTES", "15") or "15")
QWEN_REALTIME_INPUT_SAMPLE_RATE = int(os.getenv("QWEN_REALTIME_INPUT_SAMPLE_RATE", "16000") or "16000")
QWEN_REALTIME_OUTPUT_SAMPLE_RATE = int(os.getenv("QWEN_REALTIME_OUTPUT_SAMPLE_RATE", "24000") or "24000")
QWEN_REALTIME_AUDIO_FORMAT = os.getenv("QWEN_REALTIME_AUDIO_FORMAT", "pcm").strip().lower() or "pcm"
QWEN_AUDIO_PLAYBACK_BUFFER_MS = max(0, int(os.getenv("QWEN_AUDIO_PLAYBACK_BUFFER_MS", "300") or "300"))
