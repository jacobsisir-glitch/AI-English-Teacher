import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - local fallback for missing optional dependency
    load_dotenv = None


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
SILERO_SAMPLE_RATE = int(os.getenv("SILERO_SAMPLE_RATE", "16000") or "16000")
SILERO_CHANNELS = int(os.getenv("SILERO_CHANNELS", "1") or "1")
SILERO_VAD_THRESHOLD = float(os.getenv("SILERO_VAD_THRESHOLD", "0.45") or "0.45")
SILERO_MIN_SILENCE_MS = int(os.getenv("SILERO_MIN_SILENCE_MS", "450") or "450")
SILERO_PRE_SPEECH_MS = int(os.getenv("SILERO_PRE_SPEECH_MS", "300") or "300")
SILERO_MIN_SPEECH_MS = int(os.getenv("SILERO_MIN_SPEECH_MS", "180") or "180")
VOICE_DEBUG_FORCE_SEGMENT_MODE = str(os.getenv("VOICE_DEBUG_FORCE_SEGMENT_MODE", "false")).strip().lower() in {"1", "true", "yes", "on"}
VOICE_DEBUG_FORCE_SEGMENT_MS = int(os.getenv("VOICE_DEBUG_FORCE_SEGMENT_MS", "3000") or "3000")
