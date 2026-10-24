"""Open-ended restrictions: anything the guest says is saved, and enforced wherever the menu data allows."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, memory, menu, tools

TOTAL = len(menu.all_items())

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    memory.init_db()


def guest(name="sara"):
    u = memory.get_or_create_user(name)["id"]
    return u, {"user_id": u, "memory_events": []}


def say(ctx, kind, subject, op="set", **extra):
    return tools.execute_tool("update_memory", {"op": op, "kind": kind, "subject": subject, **extra}, ctx)


def rows(u):
    return {(p["kind"], p["subject"], p["polarity"], p["strength"]) for p in memory.get_active_preferences(u)}


SUGAR_DISHES = {"Umm Ali", "Kunafa Nabulsia", "Fresh Mint Lemonade"}      # all list sugar
STILL_HIDDEN = {"Umm Ali", "Kunafa Nabulsia"}                              # lemonade fits if made without sugar


@pytest.mark.parametrize("kind", ["diet", "other", "ingredient"])
def test_sugar_free_is_saved_and_enforced_whatever_kind_the_model_picks(kind):
    u, ctx = guest()
    res = say(ctx, kind, "sugar-free" if kind != "ingredient" else "sugar",
              **({"polarity": "avoid", "strength": "hard"} if kind == "ingredient" else {}))
    assert "error" not in res
    assert ("ingredient", "sugar", "avoid", "hard") in rows(u)
    assert set(res["dishes_affected"]) == SUGAR_DISHES
    assert res["can_be_ordered_without"] == {"Fresh Mint Lemonade": ["sugar"]}
    search = tools.execute_tool("search_menu", {}, ctx)
    assert set(search["hidden_by_hard_constraints"]) == STILL_HIDDEN
    assert search["count"] == TOTAL - 2
    lemonade = next(i for i in search["items"] if i["name"] == "Fresh Mint Lemonade")
    assert lemonade["must_order_without"] == ["sugar"]


@pytest.mark.parametrize("phrase", ["no sugar", "without sugar", "sugar free", "Sugar-Free"])
def test_phrasing_variants_route_the_same_way(phrase):
    u, ctx = guest()
    say(ctx, "other", phrase)
    assert ("ingredient", "sugar", "avoid", "hard") in rows(u)


def test_hidden_dishes_come_with_reasons():
    u, ctx = guest()
    say(ctx, "ingredient", "sugar", polarity="avoid", strength="hard")
    say(ctx, "diet", "vegan")
    reasons = {r["item"]: r["because"] for r in tools.execute_tool("search_menu", {}, ctx)["hidden_reasons"]}
    assert "contains sugar" in reasons["Umm Ali"]
    assert "not vegan" in reasons["Lamb Kabsa"]
    assert "not vegan" in reasons["Umm Ali"] and "contains sugar" in reasons["Umm Ali"]   # every cause is listed


def test_plural_ingredient_names_still_match():
    u, ctx = guest()
    res = say(ctx, "other", "no onions")
    assert ("ingredient", "onion", "avoid", "hard") in rows(u)
    assert {"Falafel Plate", "Lamb Kabsa"} <= set(res["dishes_affected"])


def test_known_allergen_words_become_allergens_not_ingredients():
    u, ctx = guest()
    say(ctx, "other", "nut-free")
    say(ctx, "other", "dairy-free")                        # a real diet
    assert ("allergen", "nuts", "avoid", "hard") in rows(u)
    assert ("diet", "dairy-free", "like", "hard") in rows(u)


def test_unverifiable_diets_are_saved_as_notes_and_not_enforced():
    u, ctx = guest()
    res = say(ctx, "diet", "keto")
    assert ("other", "keto", "like", "soft") in rows(u)
    assert "cannot be checked" in res["note"] or "no data to enforce" in res["note"]
    assert tools.execute_tool("search_menu", {}, ctx)["count"] == TOTAL
    prompt_memory = memory.format_memory_for_prompt(memory.get_active_preferences(u))
    assert "keto [note, not enforced]" in prompt_memory and "Likes" not in prompt_memory


def test_other_can_never_be_hard():
    u, ctx = guest()
    say(ctx, "other", "allergic to feelings", strength="hard")
    assert all(p["strength"] == "soft" for p in memory.get_active_preferences(u) if p["kind"] == "other")


def test_ingredient_nothing_matches_tells_the_model_to_be_honest():
    u, ctx = guest()
    res = say(ctx, "other", "caffeine-free")
    assert res["dishes_affected"] == [] and "cannot verify" in res["note"]


def test_removing_a_routed_restriction_clears_it():
    u, ctx = guest()
    say(ctx, "other", "sugar-free")
    say(ctx, "other", "sugar-free", op="remove")
    assert not any(p["subject"] == "sugar" for p in memory.get_active_preferences(u))
    assert tools.execute_tool("search_menu", {}, ctx)["count"] == TOTAL


def test_routed_restrictions_show_up_as_memory_chips():
    u, ctx = guest()
    say(ctx, "other", "sugar-free")
    assert ctx["memory_events"][0]["kind"] == "ingredient" and ctx["memory_events"][0]["subject"] == "sugar"
