"""Group orders (items for other people), server-side quoting, and the order_items migration."""
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import combos, config, memory, menu, tools  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    memory.init_db()


def dairy_guest():
    u = memory.get_or_create_user("abu")["id"]
    ctx = {"user_id": u, "memory_events": []}
    tools.execute_tool("update_memory", {"op": "set", "kind": "allergen", "subject": "dairy"}, ctx)
    return u, ctx


def n_orders(u):
    with memory.db() as c:
        return c.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (u,)).fetchone()[0]


# ---------------------------------------------------------------- the reported bug
def test_dairy_allergic_guest_can_order_ayran_for_friends():
    u, ctx = dairy_guest()
    res = tools.execute_tool("record_order", {"items": [
        {"item_id": "mn-kabsa", "quantity": 1},
        {"item_id": "dr-ayran", "quantity": 2, "for_others": 2}]}, ctx)
    assert res["saved"] and res["total_sar"] == 58 + 2 * 12
    assert any("Ayran" in w and "dairy" in w for w in res["warnings"])      # the model is told to flag it
    assert [l["for_others"] for l in res["lines"]] == [0, 2]


def test_ayran_for_the_guest_is_still_rejected():
    u, ctx = dairy_guest()
    bad = tools.execute_tool("record_order", {"items": [{"item_id": "dr-ayran", "quantity": 1}]}, ctx)
    assert "error" in bad and "for_others" in bad["problems"][0]
    assert n_orders(u) == 0


def test_mixed_units_any_unit_for_the_guest_triggers_the_check():
    u, ctx = dairy_guest()
    bad = tools.execute_tool("record_order", {"items": [{"item_id": "dr-ayran", "quantity": 2, "for_others": 1}]}, ctx)
    assert "error" in bad and n_orders(u) == 0


def test_for_others_must_be_within_quantity():
    u, ctx = dairy_guest()
    for others in (3, -1):
        r = tools.execute_tool("record_order", {"items": [{"item_id": "dr-ayran", "quantity": 2, "for_others": others}]}, ctx)
        assert "error" in r


def test_search_for_others_ignores_the_guests_constraints_but_normal_search_does_not():
    u, ctx = dairy_guest()
    own = tools.execute_tool("search_menu", {"query": "ayran"}, ctx)
    assert own["count"] == 0 and "Ayran" in own["hidden_by_hard_constraints"]
    others = tools.execute_tool("search_menu", {"query": "ayran", "for_others": True}, ctx)
    assert [i["id"] for i in others["items"]] == ["dr-ayran"]


# ---------------------------------------------------------------- friends' food must not become the guest's memory
def test_items_for_others_do_not_create_habits_or_history():
    u, ctx = dairy_guest()
    for d in (3, 9):
        ts = memory.fmt_ts(memory.utcnow() - timedelta(days=d))
        memory.record_order(u, None, [("mn-kabsa", 1, 0), ("dr-ayran", 2, 2)], 82, created_at=ts)
    assert combos.personal_combos(u) == []                       # no Kabsa+Ayran "habit"
    assert memory.get_recent_order_items(u) == ["mn-kabsa"]      # Ayran is not in the guest's own history


def test_same_item_for_guest_and_others_still_counts_for_the_guest():
    u = memory.get_or_create_user("sara")["id"]
    for d in (3, 9):
        ts = memory.fmt_ts(memory.utcnow() - timedelta(days=d))
        memory.record_order(u, None, [("mn-kabsa", 3, 2), ("dr-ayran", 1, 0)], 186, created_at=ts)
    assert [tuple(c["items"]) for c in combos.personal_combos(u)] == [("dr-ayran", "mn-kabsa")]


# ---------------------------------------------------------------- prices and totals come from the server
def test_quote_order_prices_from_the_menu_and_saves_nothing():
    u = memory.get_or_create_user("sara")["id"]
    ctx = {"user_id": u, "memory_events": []}
    q = tools.execute_tool("quote_order", {"items": [
        {"item_id": "st-hummus", "quantity": 1}, {"item_id": "mn-kabsa", "quantity": 2}]}, ctx)
    hummus = menu.get_item("st-hummus")["price"]
    assert q["saved"] is False and n_orders(u) == 0
    assert q["total_sar"] == hummus + 2 * 58
    assert [l["unit_price"] for l in q["lines"]] == [hummus, 58]


def test_quote_flags_the_conflict_before_the_guest_confirms():
    u, ctx = dairy_guest()
    q = tools.execute_tool("quote_order", {"items": [{"item_id": "dr-ayran", "quantity": 2}]}, ctx)
    assert "error" in q and "conflicts" in q["problems"][0]


# ---------------------------------------------------------------- migration of an existing database
def test_old_database_without_the_column_is_upgraded(tmp_path, monkeypatch):
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript("""
        CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT,
                            created_at TEXT NOT NULL DEFAULT (datetime('now')), last_seen_at TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, conversation_id TEXT,
                             created_at TEXT NOT NULL DEFAULT (datetime('now')), total_price REAL);
        CREATE TABLE order_items (order_id INTEGER NOT NULL, item_id TEXT NOT NULL,
                                  quantity INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (order_id, item_id));
        INSERT INTO users (id, username) VALUES ('u1', 'legacy');
        INSERT INTO orders (user_id, total_price) VALUES ('u1', 58);
        INSERT INTO order_items VALUES (1, 'mn-kabsa', 1);""")
    conn.commit(); conn.close()
    monkeypatch.setattr(config, "DB_PATH", str(old))
    memory.init_db()
    memory.init_db()                                           # idempotent
    with memory.db() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(order_items)")}
        assert "quantity_for_others" in cols
    assert memory.get_recent_order_items("u1") == ["mn-kabsa"]  # legacy rows count as the guest's own
