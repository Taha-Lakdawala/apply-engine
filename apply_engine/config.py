import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "answers.db"
PROFILE_PATH = ROOT / "profile.yaml"
PROFILE_EXAMPLE_PATH = ROOT / "profile.example.yaml"

GEMINI_MODEL = os.environ.get("APPLY_ENGINE_MODEL", "gemini-2.5-flash")
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"

DATA_DIR.mkdir(exist_ok=True)
