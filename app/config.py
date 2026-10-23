import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

_db = Path(os.getenv("DB_PATH", "data/app.db"))
DB_PATH = str(_db if _db.is_absolute() else BASE_DIR / _db)
MENU_PATH = BASE_DIR / "data" / "menu.json"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
THINKING = os.getenv("DEEPSEEK_THINKING", "disabled")  # "disabled" | "enabled"

MAX_TOOL_ROUNDS = 6      # safety cap on tool-call iterations per user turn
HISTORY_LIMIT = 20       # prior user/assistant messages replayed within a conversation

# --- Pairing (combo) memory ---
COMBO_HALF_LIFE_DAYS = float(os.getenv("COMBO_HALF_LIFE_DAYS", "60"))  # evidence loses half its weight every N days
COMBO_STATED_WEIGHT = float(os.getenv("COMBO_STATED_WEIGHT", "2.0"))   # a guest's own statement counts like 2 fresh orders
COMBO_MIN_CO_ORDERS = int(os.getenv("COMBO_MIN_CO_ORDERS", "2"))       # one shared order is not yet a habit
COMBO_MIN_SCORE = float(os.getenv("COMBO_MIN_SCORE", "0.5"))           # below this a pairing is treated as forgotten
