"""Deterministic tests for per-user pairing memory (no network, no LLM)."""
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import agent, combos, config, memory, menu, tools  # noqa: E402

KABSA, AYRAN, HUMMUS, GRILL = "mn-kabsa", "dr-ayran", "st-hummus", "mn-mixedgrill"


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    memory.init_db()


def uid(name="sara"):
    return memory.get_or_create_user(name)["id"]


def order(u, items, days_ago):
    total = sum(menu.get_item(i)["price"] for i in items)
    ts = memory.fmt_ts(memory.utcnow() - timedelta(days=days_ago))
    memory.record_order(u, None, [(i, 1) for i in items], total, created_at=ts)


def pairs(u, now=None):
    return [tuple(c["items"]) for c in combos.personal_combos(u, now)]


KA = tuple(sorted((KABSA, AYRAN)))
HG = tuple(sorted((HUMMUS, GRILL)))


# ---------------------------------------------------------------- learning from orders
def test_one_shared_order_is_not_a_habit_two_are():
    u = uid()
    order(u, [KABSA, AYRAN], 3)
    assert pairs(u) == []
    order(u, [KABSA, AYRAN], 1)
    assert pairs(u) == [KA]
    assert combos.personal_combos(u)[0]["co_orders"] == 2


def test_old_habit_decays_but_recent_one_stays():
    u = uid()
    for d in (170, 180, 200):
        order(u, [HUMMUS, GRILL], d)
    for d in (5, 20):
        order(u, [KABSA, AYRAN], d)
    assert pairs(u) == [KA]          # the ~6-month-old habit has faded


def test_habit_lifecycle_fades_then_returns_when_repeated():
    u = uid()
    for d in (28, 21, 14, 7):
        order(u, [KABSA, AYRAN], d)
    assert pairs(u) == [KA]
    assert pairs(u, now=memory.utcnow() + timedelta(days=200)) == []
    order(u, [KABSA, AYRAN], 1)
    order(u, [KABSA, AYRAN], 0)
    assert pairs(u) == [KA]


# ---------------------------------------------------------------- stated pairings
def test_stated_pairing_counts_then_fades_without_reinforcement():
    u = uid()
    memory.set_combo_event(u, HUMMUS, GRILL, "like")
    c = combos.personal_combos(u)[0]
    assert c["source"] == "stated" and c["score"] == pytest.approx(2.0, abs=0.05)
    assert pairs(u, now=memory.utcnow() + timedelta(days=150)) == []


def test_restating_refreshes_and_orders_reinforce_to_both():
    u = uid()
    memory.set_combo_event(u, HUMMUS, GRILL, "like")
    order(u, [HUMMUS, GRILL], 2)
    order(u, [HUMMUS, GRILL], 1)
    assert combos.personal_combos(u)[0]["source"] == "both"


# ---------------------------------------------------------------- dropping
def test_drop_hides_habit_and_it_only_returns_after_new_co_orders():
    u = uid()
    for d in (30, 20, 10):
        order(u, [KABSA, AYRAN], d)
    assert pairs(u) == [KA]
    memory.set_combo_event(u, KABSA, AYRAN, "drop", at=memory.utcnow() - timedelta(days=5))
    assert pairs(u) == []                        # old order evidence is discarded by the drop
    order(u, [KABSA, AYRAN], 2)
    assert pairs(u) == []                        # one new order is not yet a habit again
    order(u, [KABSA, AYRAN], 1)
    assert pairs(u) == [KA] and combos.personal_combos(u)[0]["co_orders"] == 2


def test_event_actions():
    u = uid()
    assert memory.set_combo_event(u, HUMMUS, GRILL, "like")["action"] == "added"
    assert memory.set_combo_event(u, GRILL, HUMMUS, "like")["action"] == "confirmed"   # order of ids irrelevant
    assert memory.set_combo_event(u, HUMMUS, GRILL, "drop")["action"] == "removed"
    assert memory.set_combo_event(u, HUMMUS, GRILL, "drop")["action"] == "confirmed"
    assert memory.set_combo_event(u, HUMMUS, GRILL, "like")["action"] == "updated"


# ---------------------------------------------------------------- constraints and fallbacks
def ctx(u):
    return {"user_id": u, "memory_events": []}


def test_hard_constraints_beat_learned_habits_and_house_pairings():
    u = uid()
    for d in (5, 10, 15):
        order(u, [KABSA, AYRAN], d)
    assert [p["item_id"] for p in tools.execute_tool("get_item", {"item_id": KABSA}, ctx(u))["pairings"]][0] == AYRAN
    tools.execute_tool("update_memory", {"op": "set", "kind": "diet", "subject": "dairy-free"}, ctx(u))
    prefs = memory.get_active_preferences(u)
    assert tools.safe_personal_combos(u, prefs) == []
    ids = [p["item_id"] for p in tools.execute_tool("get_item", {"item_id": KABSA}, ctx(u))["pairings"]]
    assert AYRAN not in ids and "st-fattoush" in ids
    assert tools.combo_prompt_lines(u, prefs) == []


def test_new_guest_gets_house_pairings_labelled_as_house():
    u = uid()
    pr = tools.execute_tool("get_item", {"item_id": KABSA}, ctx(u))["pairings"]
    assert AYRAN in [p["item_id"] for p in pr] and {p["source"] for p in pr} == {"house"}


def test_dropped_pair_is_not_resuggested_by_house_fallback():
    u = uid()
    tools.execute_tool("remember_combo", {"op": "drop", "item_ids": [KABSA, AYRAN]}, ctx(u))
    ids = [p["item_id"] for p in tools.execute_tool("get_item", {"item_id": KABSA}, ctx(u))["pairings"]]
    assert AYRAN not in ids


def test_pairing_memory_is_per_user():
    a, b = uid("alice"), uid("bob")
    for d in (3, 9):
        order(a, [KABSA, AYRAN], d)
    assert pairs(b) == []
    pr = tools.execute_tool("get_item", {"item_id": KABSA}, ctx(b))["pairings"]
    assert {p["source"] for p in pr} == {"house"}                      # bob never sees alice's habit
    assert [p["source"] for p in tools.execute_tool("get_item", {"item_id": KABSA}, ctx(a))["pairings"]][0] == "guest_habit"


# ---------------------------------------------------------------- tool + prompt + API
def test_remember_combo_tool_validates_and_logs_event():
    u = uid()
    c = ctx(u)
    assert "error" in tools.execute_tool("remember_combo", {"op": "set", "item_ids": [KABSA, KABSA]}, c)
    assert "error" in tools.execute_tool("remember_combo", {"op": "set", "item_ids": [KABSA, "nope"]}, c)
    res = tools.execute_tool("remember_combo", {"op": "set", "item_ids": [HUMMUS, GRILL]}, c)
    assert res["result"]["action"] == "added"
    assert c["memory_events"][0] == {"action": "added", "kind": "combo", "subject": "Mixed Grill Platter + Classic Hummus"}


def test_remember_combo_warns_when_pair_conflicts_with_constraints():
    u = uid()
    tools.execute_tool("update_memory", {"op": "set", "kind": "diet", "subject": "vegan"}, ctx(u))
    res = tools.execute_tool("remember_combo", {"op": "set", "item_ids": [KABSA, AYRAN]}, ctx(u))
    assert "note" in res and pairs(u) == [KA]            # stored, but filtered at suggestion time
    assert tools.safe_personal_combos(u, memory.get_active_preferences(u)) == []


def test_system_prompt_lists_usual_pairings_only_while_active():
    user = memory.get_or_create_user("sara")
    for d in (2, 8):
        order(user["id"], [KABSA, AYRAN], d)
    prompt = agent.build_system_prompt(user, memory.get_active_preferences(user["id"]))
    assert "Ayran + Lamb Kabsa" in prompt and "ordered together 2 times" in prompt
    memory.set_combo_event(user["id"], KABSA, AYRAN, "drop")
    prompt = agent.build_system_prompt(user, memory.get_active_preferences(user["id"]))
    assert "Ayran + Lamb Kabsa" not in prompt


def test_api_exposes_pairings_for_the_ui():
    from fastapi.testclient import TestClient
    from app.main import app
    u = uid("sara")
    for d in (2, 8):
        order(u, [KABSA, AYRAN], d)
    with TestClient(app) as client:
        body = client.post("/api/login", json={"username": "sara"}).json()
        assert body["combos"][0]["names"] == ["Ayran", "Lamb Kabsa"]
        assert client.get("/api/memory", params={"username": "sara"}).json()["combos"]
        assert client.get("/api/memory", params={"username": "other"}).json()["combos"] == []
