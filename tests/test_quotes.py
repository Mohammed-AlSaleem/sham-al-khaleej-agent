"""Confirming records exactly what was quoted, even though chat history keeps no tool arguments."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import agent, config, memory, tools  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    memory.init_db()


def setup(name="sara"):
    u = memory.get_or_create_user(name)["id"]
    cid = memory.create_conversation(u)
    return u, cid, {"user_id": u, "conversation_id": cid, "memory_events": []}


def sugar_guest():
    u, cid, ctx = setup()
    tools.execute_tool("update_memory", {"op": "set", "kind": "other", "subject": "sugar-free"}, ctx)
    return u, cid, ctx


LEMONADE = {"item_id": "dr-lemonmint", "quantity": 1, "without": ["sugar"]}


def mods(oid):
    with memory.db() as c:
        return [tuple(r) for r in c.execute(
            "SELECT item_id, removed_ingredient, quantity FROM order_modifications WHERE order_id = ?", (oid,))]


def test_confirming_without_items_records_exactly_the_quote():
    u, cid, ctx = sugar_guest()
    tools.execute_tool("quote_order", {"items": [LEMONADE, {"item_id": "mn-kabsa", "quantity": 2, "for_others": 1}]}, ctx)
    res = tools.execute_tool("record_order", {}, ctx)
    assert res["saved"] and res["total_sar"] == 16 + 2 * 58
    assert mods(res["order_id"]) == [("dr-lemonmint", "sugar", 1)]
    assert [l["for_others"] for l in res["lines"]] == [0, 1]
    assert "error" in tools.execute_tool("record_order", {}, ctx)        # consumed: no double order


def test_nothing_quoted_means_nothing_to_confirm():
    u, cid, ctx = setup()
    assert "quote_order first" in tools.execute_tool("record_order", {}, ctx)["problems"][0]


def test_a_new_quote_replaces_the_old_one():
    u, cid, ctx = setup()
    tools.execute_tool("quote_order", {"items": [{"item_id": "mn-kabsa", "quantity": 1}]}, ctx)
    tools.execute_tool("quote_order", {"items": [{"item_id": "dr-ayran", "quantity": 3}]}, ctx)
    assert tools.execute_tool("record_order", {}, ctx)["total_sar"] == 36


def test_an_invalid_quote_clears_the_old_one():
    u, cid, ctx = sugar_guest()
    tools.execute_tool("quote_order", {"items": [LEMONADE]}, ctx)
    bad = tools.execute_tool("quote_order", {"items": [{"item_id": "dr-lemonmint", "quantity": 1}]}, ctx)
    assert "error" in bad
    assert "error" in tools.execute_tool("record_order", {}, ctx)


def test_quotes_are_per_conversation():
    u, cid, ctx = setup()
    tools.execute_tool("quote_order", {"items": [{"item_id": "mn-kabsa", "quantity": 1}]}, ctx)
    other = {**ctx, "conversation_id": memory.create_conversation(u)}
    assert "error" in tools.execute_tool("record_order", {}, other)


def test_explicit_items_still_work_and_consume_the_quote():
    u, cid, ctx = setup()
    tools.execute_tool("quote_order", {"items": [{"item_id": "mn-kabsa", "quantity": 1}]}, ctx)
    res = tools.execute_tool("record_order", {"items": [{"item_id": "dr-ayran", "quantity": 1}]}, ctx)
    assert res["saved"] and res["total_sar"] == 12
    assert "error" in tools.execute_tool("record_order", {}, ctx)


def scripted(steps):
    state = {"i": 0}

    def create(**kw):
        step = steps[state["i"]]
        state["i"] += 1
        return NS(choices=[NS(message=step)])
    return NS(chat=NS(completions=NS(create=create)))


def call(name, args):
    return NS(content=None, reasoning_content=None,
              tool_calls=[NS(id=f"c-{name}", function=NS(name=name, arguments=json.dumps(args)))])


def text(t):
    return NS(content=t, tool_calls=None, reasoning_content=None)


def test_customisation_survives_across_turns_with_text_only_history(monkeypatch):
    user = memory.get_or_create_user("sara")
    cid = memory.create_conversation(user["id"])
    client = scripted([
        call("update_memory", {"op": "set", "kind": "other", "subject": "sugar-free"}), text("Noted, no sugar."),
        call("quote_order", {"items": [LEMONADE]}), text("Lemonade without sugar, 16 SAR. Confirm?"),
        call("record_order", {}),            # the model forgets the customisation: the server remembers it
        text("Done!")])
    monkeypatch.setattr(agent, "get_client", lambda: client)
    for msg in ("I can't have sugar", "One fresh mint lemonade", "Yes"):
        out = agent.run_turn(user, cid, msg)
    assert out["tool_trace"][0]["tool"] == "record_order"
    with memory.db() as c:
        oid = c.execute("SELECT id FROM orders WHERE user_id = ?", (user["id"],)).fetchone()[0]
    assert mods(oid) == [("dr-lemonmint", "sugar", 1)]
