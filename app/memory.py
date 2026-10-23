"""Persistent per-user memory: users, conversations, structured preferences, orders.

Key rules
- Preferences are never edited in place. A change marks the old row 'superseded'
  and inserts a new one linked via supersedes_id, so history stays auditable.
- Every query is scoped by user_id, which is what isolates users from each other.
"""
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config

KINDS = ("diet", "allergen", "ingredient", "dish", "category", "spice", "budget", "other")
POLARITIES = ("like", "avoid", "limit")
# Mutually exclusive diets: stating one replaces the others (vegetarian -> pescatarian).
DIET_EXCLUSIVE = ("vegetarian", "vegan", "pescatarian")


TS_FMT = "%Y-%m-%d %H:%M:%S"


def utcnow() -> datetime:
    """Naive UTC datetime, second resolution (matches SQLite datetime('now'))."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def fmt_ts(dt: datetime) -> str:
    return dt.strftime(TS_FMT)


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, TS_FMT)


@contextmanager
def db():
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript(config.SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate(c)


def _migrate(c) -> None:
    """Upgrade databases created before a column existed (CREATE TABLE IF NOT EXISTS will not add it)."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(order_items)")}
    if "quantity_for_others" not in cols:
        c.execute("ALTER TABLE order_items ADD COLUMN quantity_for_others INTEGER NOT NULL DEFAULT 0")


# ---------------------------------------------------------------- users
def normalize_username(raw: str) -> str:
    name = re.sub(r"\s+", " ", (raw or "").strip()).lower()
    if not 2 <= len(name) <= 40:
        raise ValueError("Username must be 2-40 characters.")
    return name


def get_or_create_user(username: str) -> dict:
    name = normalize_username(username)
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()
        if row is None:
            uid = str(uuid.uuid4())
            c.execute("INSERT INTO users (id, username, display_name) VALUES (?, ?, ?)", (uid, name, name))
        else:
            uid = row["id"]
        c.execute("UPDATE users SET last_seen_at = datetime('now') WHERE id = ?", (uid,))
        n_conv = c.execute("SELECT COUNT(*) FROM conversations WHERE user_id = ?", (uid,)).fetchone()[0]
    return {"id": uid, "username": name, "is_returning": n_conv > 0, "conversation_count": n_conv}


# ---------------------------------------------------------------- conversations
def create_conversation(user_id: str) -> str:
    cid = str(uuid.uuid4())
    with db() as c:
        c.execute("INSERT INTO conversations (id, user_id) VALUES (?, ?)", (cid, user_id))
    return cid


def conversation_belongs_to(conversation_id: str, user_id: str) -> bool:
    with db() as c:
        return c.execute("SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
                         (conversation_id, user_id)).fetchone() is not None


def log_message(conversation_id: str, role: str, content: str) -> int:
    with db() as c:
        cur = c.execute("INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
                        (conversation_id, role, content))
        return cur.lastrowid


def get_history(conversation_id: str, limit: int = config.HISTORY_LIMIT) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? AND role IN ('user','assistant') "
            "ORDER BY id DESC LIMIT ?", (conversation_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


# ---------------------------------------------------------------- preferences
def _pref(row: sqlite3.Row) -> dict:
    d = dict(row)
    raw = d.pop("value_json", None)
    d["value"] = json.loads(raw) if raw else None
    return d


def get_active_preferences(user_id: str) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT * FROM preferences WHERE user_id = ? AND status = 'active' "
            "ORDER BY (strength = 'hard') DESC, kind, id", (user_id,)).fetchall()
    return [_pref(r) for r in rows]


def get_preference_history(user_id: str) -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM preferences WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
    return [_pref(r) for r in rows]


def set_preference(user_id: str, kind: str, subject: str, polarity: str, strength: str = "soft",
                   value: dict | None = None, note: str | None = None,
                   source_message_id: int | None = None) -> dict:
    """Add a preference, or supersede the active one for the same (kind, subject)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if polarity not in POLARITIES:
        raise ValueError(f"polarity must be one of {POLARITIES}")
    subject = subject.strip().lower()
    if kind == "diet":
        polarity, strength = "like", "hard"       # a diet is something the guest follows, always binding
    if kind == "allergen":
        polarity, strength = "avoid", "hard"
    value_json = json.dumps(value, sort_keys=True) if value else None

    with db() as c:
        existing = c.execute(
            "SELECT * FROM preferences WHERE user_id = ? AND kind = ? AND subject = ? AND status = 'active'",
            (user_id, kind, subject)).fetchone()
        if (existing and existing["polarity"] == polarity and existing["strength"] == strength
                and existing["value_json"] == value_json):
            c.execute("UPDATE preferences SET last_confirmed_at = datetime('now') WHERE id = ?", (existing["id"],))
            return {"action": "confirmed", "kind": kind, "subject": subject}

        old_rows = [existing] if existing else []
        if kind == "diet" and subject in DIET_EXCLUSIVE:
            marks = ",".join("?" * len(DIET_EXCLUSIVE))
            old_rows += c.execute(
                f"SELECT * FROM preferences WHERE user_id = ? AND kind = 'diet' AND status = 'active' "
                f"AND subject IN ({marks}) AND subject <> ?", (user_id, *DIET_EXCLUSIVE, subject)).fetchall()
        for r in old_rows:
            c.execute("UPDATE preferences SET status = 'superseded', superseded_at = datetime('now') WHERE id = ?",
                      (r["id"],))
        c.execute(
            "INSERT INTO preferences (user_id, kind, subject, polarity, strength, value_json, note, "
            "supersedes_id, source_message_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, kind, subject, polarity, strength, value_json, note,
             old_rows[0]["id"] if old_rows else None, source_message_id))
        return {"action": "updated" if old_rows else "added", "kind": kind, "subject": subject,
                "polarity": polarity, "strength": strength,
                "replaced": [{"kind": r["kind"], "subject": r["subject"], "polarity": r["polarity"]}
                             for r in old_rows]}


def remove_preference(user_id: str, kind: str, subject: str) -> dict:
    subject = subject.strip().lower()
    with db() as c:
        cur = c.execute(
            "UPDATE preferences SET status = 'retracted', superseded_at = datetime('now') "
            "WHERE user_id = ? AND kind = ? AND subject = ? AND status = 'active'", (user_id, kind, subject))
    return {"action": "removed" if cur.rowcount else "not_found", "kind": kind, "subject": subject}


def format_memory_for_prompt(prefs: list[dict]) -> str:
    if not prefs:
        return "Nothing is known about this guest yet."
    hard, likes, dislikes, other = [], [], [], []
    for p in prefs:
        label = f"{p['subject']}"
        if p["kind"] in ("spice", "budget") and p["value"]:
            label = f"{p['kind']}: {json.dumps(p['value'])}"
        if p["strength"] == "hard":
            hard.append(f"{p['kind']}: {p['subject']}" + (f" ({p['polarity']})" if p["kind"] not in ("diet", "allergen") else ""))
        elif p["kind"] in ("spice", "budget"):
            other.append(label)
        elif p["polarity"] == "like":
            likes.append(f"{p['subject']} ({p['kind']})")
        elif p["polarity"] == "avoid":
            dislikes.append(f"{p['subject']} ({p['kind']})")
        else:
            other.append(f"{p['kind']}: {p['subject']}")
    lines = []
    if hard:
        lines.append("HARD constraints (never violate): " + "; ".join(hard))
    if likes:
        lines.append("Likes: " + ", ".join(likes))
    if dislikes:
        lines.append("Dislikes: " + ", ".join(dislikes))
    if other:
        lines.append("Other: " + "; ".join(other))
    return "\n".join(lines)


# ---------------------------------------------------------------- orders
def record_order(user_id: str, conversation_id: str | None, lines: list[tuple], total: float,
                 created_at: str | None = None) -> int:
    """lines: (item_id, quantity) or (item_id, quantity, quantity_for_others).
    created_at is for seeding demo data and tests; normal orders use the current time."""
    with db() as c:
        if created_at:
            cur = c.execute("INSERT INTO orders (user_id, conversation_id, total_price, created_at) VALUES (?,?,?,?)",
                            (user_id, conversation_id, total, created_at))
        else:
            cur = c.execute("INSERT INTO orders (user_id, conversation_id, total_price) VALUES (?,?,?)",
                            (user_id, conversation_id, total))
        oid = cur.lastrowid
        for item_id, qty, *rest in lines:
            others = rest[0] if rest else 0
            c.execute("INSERT INTO order_items (order_id, item_id, quantity, quantity_for_others) VALUES (?,?,?,?) "
                      "ON CONFLICT(order_id, item_id) DO UPDATE SET quantity = quantity + excluded.quantity, "
                      "quantity_for_others = quantity_for_others + excluded.quantity_for_others",
                      (oid, item_id, qty, others))
    return oid


def get_recent_order_items(user_id: str, limit_orders: int = 5) -> list[str]:
    with db() as c:
        rows = c.execute(
            "SELECT oi.item_id FROM order_items oi JOIN orders o ON o.id = oi.order_id "
            "WHERE o.user_id = ? AND oi.quantity > oi.quantity_for_others AND o.id IN (SELECT id FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?) "
            "ORDER BY o.id DESC", (user_id, user_id, limit_orders)).fetchall()
    seen = []
    for r in rows:
        if r["item_id"] not in seen:
            seen.append(r["item_id"])
    return seen


def get_order_events(user_id: str) -> list[tuple[str, list[str]]]:
    """[(created_at, [item_id, ...]), ...] one entry per order, oldest first."""
    with db() as c:
        rows = c.execute(
            "SELECT o.id AS oid, o.created_at, oi.item_id FROM orders o "
            "JOIN order_items oi ON oi.order_id = o.id WHERE o.user_id = ? AND oi.quantity > oi.quantity_for_others "
            "ORDER BY o.id", (user_id,)).fetchall()
    grouped: dict[int, tuple[str, list[str]]] = {}
    for r in rows:
        grouped.setdefault(r["oid"], (r["created_at"], []))[1].append(r["item_id"])
    return list(grouped.values())


# ---------------------------------------------------------------- pairings (combos)
def _pair(a: str, b: str) -> tuple[str, str]:
    return tuple(sorted((a, b)))  # type: ignore[return-value]


def set_combo_event(user_id: str, a: str, b: str, stance: str, source_message_id: int | None = None,
                    at: datetime | None = None) -> dict:
    """Append a 'like' or 'drop' event for a pair. Events are never edited; the latest one wins."""
    a, b = _pair(a, b)
    with db() as c:
        row = c.execute("SELECT stance FROM combo_events WHERE user_id = ? AND item_a = ? AND item_b = ? "
                        "ORDER BY id DESC LIMIT 1", (user_id, a, b)).fetchone()
        prev = row["stance"] if row else None
        c.execute("INSERT INTO combo_events (user_id, item_a, item_b, stance, source_message_id, created_at) "
                  "VALUES (?,?,?,?,?,?)", (user_id, a, b, stance, source_message_id, fmt_ts(at or utcnow())))
    if stance == "like":
        action = "added" if prev is None else "confirmed" if prev == "like" else "updated"
    else:
        action = "confirmed" if prev == "drop" else "removed"
    return {"action": action, "items": [a, b], "stance": stance}


def get_combo_state(user_id: str) -> dict[tuple[str, str], dict]:
    """pair -> {stance, stance_ts, last_drop_ts} from the latest events."""
    with db() as c:
        rows = c.execute("SELECT item_a, item_b, stance, created_at FROM combo_events "
                         "WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
    state: dict[tuple[str, str], dict] = {}
    for r in rows:
        s = state.setdefault((r["item_a"], r["item_b"]), {"stance": None, "stance_ts": None, "last_drop_ts": None})
        s["stance"], s["stance_ts"] = r["stance"], r["created_at"]
        if r["stance"] == "drop":
            s["last_drop_ts"] = r["created_at"]
    return state
