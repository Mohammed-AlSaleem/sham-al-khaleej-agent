"""Ordering a dish without an ingredient ("Fresh Mint Lemonade without sugar")."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, memory, menu, tools  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    memory.init_db()


def guest(name="sara"):
    u = memory.get_or_create_user(name)["id"]
    return u, {"user_id": u, "memory_events": []}


def mem(ctx, kind, subject, **extra):
    return tools.execute_tool("update_memory", {"op": "set", "kind": kind, "subject": subject, **extra}, ctx)


def order(ctx, items, tool="quote_order"):
    return tools.execute_tool(tool, {"items": items}, ctx)


def sugar_guest():
    u, ctx = guest()
    mem(ctx, "other", "sugar-free")
    return u, ctx


# ---------------------------------------------------------------- the requested feature
def test_lemonade_without_sugar_is_allowed_for_a_sugar_free_guest():
    u, ctx = sugar_guest()
    q = order(ctx, [{"item_id": "dr-lemonmint", "quantity": 1, "without": ["sugar"]}])
    assert "error" not in q and q["total_sar"] == 16
    assert q["lines"][0]["without"] == ["sugar"]
    assert any("kitchen" in w for w in q["warnings"])           # cross-contact reminder


def test_same_lemonade_without_the_modification_is_rejected_with_a_hint():
    u, ctx = sugar_guest()
    bad = order(ctx, [{"item_id": "dr-lemonmint", "quantity": 1}])
    assert "error" in bad and "without ['sugar']" in bad["problems"][0]


def test_modifications_work_for_any_guest_not_only_restricted_ones():
    u, ctx = guest()
    q = order(ctx, [{"item_id": "mn-shawarma", "quantity": 1, "without": ["Pickles"]}])   # case-insensitive
    assert q["lines"][0]["without"] == ["pickles"] and q["warnings"] == []


def test_only_listed_ingredients_can_be_removed():
    u, ctx = guest()
    bad = order(ctx, [{"item_id": "st-hummus", "quantity": 1, "without": ["chickpeas"]}])
    assert "error" in bad and "garlic" in bad["problems"][0]
    assert "error" in order(ctx, [{"item_id": "st-hummus", "quantity": 1, "without": ["unicorn"]}])


# ---------------------------------------------------------------- safety: removal never clears allergies or diets
def test_removing_an_ingredient_never_clears_an_allergy():
    u, ctx = guest()
    mem(ctx, "allergen", "nuts")
    bad = order(ctx, [{"item_id": "mn-kabsa", "quantity": 1, "without": ["almonds"]}])
    assert "error" in bad and "nuts" in bad["problems"][0]


def test_removing_an_ingredient_never_clears_a_diet():
    u, ctx = guest()
    mem(ctx, "diet", "vegetarian")
    bad = order(ctx, [{"item_id": "mn-shawarma", "quantity": 1, "without": ["pickles"]}])
    assert "error" in bad and "vegetarian" in bad["problems"][0]


def test_dish_that_still_has_the_ingredient_after_removal_is_rejected():
    u, ctx = sugar_guest()
    bad = order(ctx, [{"item_id": "ds-umali", "quantity": 1, "without": ["raisins"]}])   # sugar is not removable here
    assert "error" in bad


# ---------------------------------------------------------------- discovery
def test_search_offers_dishes_that_fit_when_made_without_the_ingredient():
    u, ctx = sugar_guest()
    res = tools.execute_tool("search_menu", {"query": "lemonade"}, ctx)
    assert [i["name"] for i in res["items"]] == ["Fresh Mint Lemonade"]
    assert res["items"][0]["must_order_without"] == ["sugar"]
    assert res["hidden_by_hard_constraints"] == []


def test_allergy_hidden_dishes_are_not_offered_as_modifiable():
    u, ctx = guest()
    mem(ctx, "allergen", "nuts")
    res = tools.execute_tool("search_menu", {"query": "kabsa"}, ctx)
    assert res["count"] == 0 and res["hidden_by_hard_constraints"] == ["Lamb Kabsa"]


def test_get_item_reports_removable_ingredients_and_fix():
    u, ctx = sugar_guest()
    r = tools.execute_tool("get_item", {"item_id": "dr-lemonmint"}, ctx)
    assert r["removable_ingredients"] == ["sugar"] and r["fixable_by_removing"] == ["sugar"]
    assert "contains sugar" in r["conflicts_with_guest_constraints"]


# ---------------------------------------------------------------- persistence
def test_record_order_stores_modifications_and_mixed_lines():
    u, ctx = sugar_guest()
    res = order(ctx, [{"item_id": "dr-lemonmint", "quantity": 2, "without": ["sugar"]},
                      {"item_id": "mn-shawarma", "quantity": 1, "without": ["pickles", "garlic sauce"]}], "record_order")
    assert res["saved"] and res["total_sar"] == 32 + 42
    with memory.db() as c:
        mods = c.execute("SELECT item_id, removed_ingredient, quantity FROM order_modifications "
                         "WHERE order_id = ? ORDER BY id", (res["order_id"],)).fetchall()
        items = c.execute("SELECT item_id, quantity FROM order_items WHERE order_id = ? ORDER BY item_id",
                          (res["order_id"],)).fetchall()
    assert [tuple(m) for m in mods] == [("dr-lemonmint", "sugar", 2), ("mn-shawarma", "garlic sauce", 1),
                                        ("mn-shawarma", "pickles", 1)]
    assert [tuple(i) for i in items] == [("dr-lemonmint", 2), ("mn-shawarma", 1)]


def test_same_item_prepared_two_ways_in_one_order():
    u, ctx = guest()
    res = order(ctx, [{"item_id": "dr-lemonmint", "quantity": 1},
                      {"item_id": "dr-lemonmint", "quantity": 1, "without": ["sugar"]}], "record_order")
    assert res["saved"] and res["total_sar"] == 32
    with memory.db() as c:
        assert c.execute("SELECT quantity FROM order_items WHERE order_id = ?", (res["order_id"],)).fetchone()[0] == 2
        assert c.execute("SELECT quantity FROM order_modifications WHERE order_id = ?",
                         (res["order_id"],)).fetchone()[0] == 1


def test_for_others_and_without_combine():
    u, ctx = sugar_guest()
    res = order(ctx, [{"item_id": "ds-kunafa", "quantity": 1, "for_others": 1, "without": ["pistachios"]}])
    assert "error" not in res and res["lines"][0]["without"] == ["pistachios"]


# ---------------------------------------------------------------- menu data sanity
def test_every_removable_ingredient_exists_in_its_item():
    for item in menu.all_items():
        have = {i.lower() for i in item["ingredients"]}
        assert {r.lower() for r in item.get("removable_ingredients", [])} <= have, item["id"]
