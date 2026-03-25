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
