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
FUNASR_WS_URL = os.getenv("FUNASR_WS_URL", "").strip()
FUNASR_MODE = os.getenv("FUNASR_MODE", "2pass").strip() or "2pass"
FUNASR_MODEL_NAME = os.getenv("FUNASR_MODEL_NAME", "").strip()
FUNASR_CHUNK_SIZE = _get_int_list_env("FUNASR_CHUNK_SIZE", "5,8,4")
FUNASR_CHUNK_INTERVAL = int(os.getenv("FUNASR_CHUNK_INTERVAL", "8") or "8")
FUNASR_FINAL_WAIT_OFFLINE_MS = int(os.getenv("FUNASR_FINAL_WAIT_OFFLINE_MS", "5000") or "5000")
FUNASR_FINAL_WAIT_FALLBACK_MS = int(os.getenv("FUNASR_FINAL_WAIT_FALLBACK_MS", "1000") or "1000")
FUNASR_FINAL_DRAIN_WINDOW_MS = int(os.getenv("FUNASR_FINAL_DRAIN_WINDOW_MS", "4000") or "4000")
FUNASR_FINAL_RESCUE_WAIT_MS = int(os.getenv("FUNASR_FINAL_RESCUE_WAIT_MS", "6000") or "6000")
FUNASR_LATE_FINAL_GRACE_MS = int(os.getenv("FUNASR_LATE_FINAL_GRACE_MS", "12000") or "12000")
FUNASR_FALLBACK_MIN_STABLE_MS = int(os.getenv("FUNASR_FALLBACK_MIN_STABLE_MS", "240") or "240")
SILERO_SAMPLE_RATE = int(os.getenv("SILERO_SAMPLE_RATE", "16000") or "16000")
SILERO_CHANNELS = int(os.getenv("SILERO_CHANNELS", "1") or "1")
SILERO_VAD_THRESHOLD = float(os.getenv("SILERO_VAD_THRESHOLD", "0.38") or "0.38")
SILERO_MIN_SILENCE_MS = int(os.getenv("SILERO_MIN_SILENCE_MS", "1200") or "1200")
SILERO_PRE_SPEECH_MS = int(os.getenv("SILERO_PRE_SPEECH_MS", "480") or "480")
SILERO_MIN_SPEECH_MS = int(os.getenv("SILERO_MIN_SPEECH_MS", "320") or "320")
SILERO_SPEECH_END_HOLD_MS = int(os.getenv("SILERO_SPEECH_END_HOLD_MS", "720") or "720")
VOICE_DEBUG_FORCE_SEGMENT_MODE = str(os.getenv("VOICE_DEBUG_FORCE_SEGMENT_MODE", "false")).strip().lower() in {"1", "true", "yes", "on"}
VOICE_DEBUG_FORCE_SEGMENT_MS = int(os.getenv("VOICE_DEBUG_FORCE_SEGMENT_MS", "3000") or "3000")
