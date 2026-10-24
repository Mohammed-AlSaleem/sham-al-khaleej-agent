import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import agent, config, memory, menu, tools  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    memory.init_db()


def uid(name="sara"):
    return memory.get_or_create_user(name)["id"]


# ---------------------------------------------------------------- memory
def test_correction_supersedes_and_keeps_history():
    u = uid()
    memory.set_preference(u, "diet", "vegetarian", "like")
    r = memory.set_preference(u, "diet", "pescatarian", "like")
    assert r["action"] == "updated"
    active = [p["subject"] for p in memory.get_active_preferences(u) if p["kind"] == "diet"]
    assert active == ["pescatarian"]
    hist = [(p["subject"], p["status"]) for p in memory.get_preference_history(u)]
    assert ("vegetarian", "superseded") in hist


def test_independent_diets_coexist():
    u = uid()
    memory.set_preference(u, "diet", "vegetarian", "like")
    memory.set_preference(u, "diet", "gluten-free", "like")
    assert {p["subject"] for p in memory.get_active_preferences(u)} == {"vegetarian", "gluten-free"}


def test_flip_like_to_avoid_and_remove():
    u = uid()
    memory.set_preference(u, "ingredient", "eggplant", "like")
    memory.set_preference(u, "ingredient", "eggplant", "avoid")
    assert [p["polarity"] for p in memory.get_active_preferences(u)] == ["avoid"]
    assert memory.remove_preference(u, "ingredient", "eggplant")["action"] == "removed"
    assert memory.get_active_preferences(u) == []


def test_users_are_isolated():
    a, b = uid("alice"), uid("bob")
    memory.set_preference(a, "allergen", "nuts", "avoid")
    assert memory.get_active_preferences(b) == []
    ctx = {"user_id": b, "memory_events": []}
    res = tools.execute_tool("search_menu", {}, ctx)
    assert any("nuts" in i["allergens"] for i in res["items"])  # bob's search is unaffected by alice's allergy


def test_username_normalisation_returns_same_user():
    assert uid("  Sara ") == uid("sara")


# ---------------------------------------------------------------- hard constraints
def test_allergy_enforced_even_if_model_forgets_it():
    u = uid()
    ctx = {"user_id": u, "memory_events": []}
    tools.execute_tool("update_memory", {"op": "set", "kind": "allergen", "subject": "peanuts"}, ctx)
    res = tools.execute_tool("search_menu", {}, ctx)   # model passes NO filters
    assert res["count"] > 0 and all("nuts" not in i["allergens"] for i in res["items"])
    assert "Lamb Kabsa" in res["hidden_by_hard_constraints"]


def test_vegan_diet_enforced_and_order_rejected_on_conflict():
    u = uid()
    ctx = {"user_id": u, "memory_events": []}
    tools.execute_tool("update_memory", {"op": "set", "kind": "diet", "subject": "vegan"}, ctx)
    res = tools.execute_tool("search_menu", {}, ctx)
    assert all(i["vegan"] for i in res["items"])
    bad = tools.execute_tool("record_order", {"items": [{"item_id": "mn-kabsa"}]}, ctx)
    assert "error" in bad
    ok = tools.execute_tool("record_order", {"items": [{"item_id": "mn-maqluba", "quantity": 2}]}, ctx)
    assert ok["total_sar"] == 68


def test_unknown_allergen_is_saved_as_hard_ingredient_exclusion():
    ctx = {"user_id": uid(), "memory_events": []}
    res = tools.execute_tool("update_memory", {"op": "set", "kind": "allergen", "subject": "kiwi"}, ctx)
    assert "error" not in res
    assert (res["results"][0]["kind"], res["results"][0]["strength"]) == ("ingredient", "hard")
    assert "kitchen" in res["note"] and "No menu dish" in res["note"]


def test_conflicting_requirements_return_honest_empty_or_small_sets():
    res = menu.search_items(diets=["vegan"], exclude_allergens=["nuts", "sesame", "gluten"], min_spice=2)
    assert res == []


# ---------------------------------------------------------------- agent loop with a scripted fake LLM
def fake_client(script):
    calls = {"n": 0, "seen": []}

    def create(**kw):
        calls["seen"].append(kw["messages"])
        step = script[calls["n"]]
        calls["n"] += 1
        return NS(choices=[NS(message=step)])

    return NS(chat=NS(completions=NS(create=create))), calls


def tool_msg(name, args, reasoning=None):
    tc = NS(id=f"c{name}", function=NS(name=name, arguments=json.dumps(args)))
    return NS(content=None, tool_calls=[tc], reasoning_content=reasoning)


def test_agent_loop_saves_memory_and_replays_reasoning(monkeypatch):
    user = memory.get_or_create_user("sara")
    cid = memory.create_conversation(user["id"])
    client, calls = fake_client([
        tool_msg("update_memory", {"op": "set", "kind": "diet", "subject": "vegetarian"}, reasoning="think-1"),
        NS(content="Welcome! Try the Vegetable Maqluba (34 SAR).", tool_calls=None, reasoning_content="think-2"),
    ])
    monkeypatch.setattr(agent, "get_client", lambda: client)
    out = agent.run_turn(user, cid, "I'm vegetarian")
    assert "Maqluba" in out["reply"]
    assert out["memory_events"][0]["action"] == "added"
    second_call = calls["seen"][1]
    assert any(m.get("reasoning_content") == "think-1" for m in second_call if m["role"] == "assistant")


def test_returning_user_memory_reaches_next_conversation_prompt():
    u = memory.get_or_create_user("sara")
    memory.set_preference(u["id"], "allergen", "shellfish", "avoid")
    memory.create_conversation(u["id"])
    again = memory.get_or_create_user("sara")
    assert again["is_returning"]
    prompt = agent.build_system_prompt(again, memory.get_active_preferences(again["id"]))
    assert "shellfish" in prompt and "returning" in prompt
