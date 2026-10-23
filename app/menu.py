"""Menu access layer. The only source of truth for dishes, prices, ingredients and allergens."""
import json
from functools import lru_cache

from . import config

# Diet name -> predicate telling whether an item is compatible with that diet.
DIET_RULES = {
    "vegetarian": lambda i: i["vegetarian"],
    "vegan": lambda i: i["vegan"],
    "pescatarian": lambda i: i["vegetarian"] or "seafood" in i["tags"],
    "gluten-free": lambda i: "gluten" not in i["allergens"],
    "dairy-free": lambda i: "dairy" not in i["allergens"],
    "halal": lambda i: True,  # the whole menu is halal
}

# Common user words -> allergen vocabulary terms used in menu.json.
ALLERGEN_ALIASES = {
    "peanut": ["nuts"], "peanuts": ["nuts"], "nut": ["nuts"], "tree nuts": ["nuts"],
    "walnut": ["nuts"], "walnuts": ["nuts"], "almond": ["nuts"], "almonds": ["nuts"],
    "pistachio": ["nuts"], "pistachios": ["nuts"], "pine nuts": ["nuts"],
    "wheat": ["gluten"], "milk": ["dairy"], "lactose": ["dairy"],
    "egg": ["eggs"], "shrimp": ["shellfish"], "prawn": ["shellfish"], "prawns": ["shellfish"],
    "crab": ["shellfish"], "lobster": ["shellfish"], "sesame seeds": ["sesame"],
    "seafood": ["fish", "shellfish"],
}


@lru_cache(maxsize=1)
def load_menu() -> dict:
    with open(config.MENU_PATH, encoding="utf-8") as f:
        return json.load(f)


def all_items() -> list[dict]:
    return load_menu()["items"]


def allergen_vocab() -> list[str]:
    return load_menu()["restaurant"]["allergen_vocabulary"]


def categories() -> list[str]:
    seen = []
    for i in all_items():
        if i["category"] not in seen:
            seen.append(i["category"])
    return seen


def normalize_allergen(subject: str) -> list[str] | None:
    """Map a free-text allergen to menu vocabulary terms, or None if unknown."""
    s = subject.strip().lower()
    if s in allergen_vocab():
        return [s]
    return ALLERGEN_ALIASES.get(s)


def get_item(item_id: str) -> dict | None:
    return next((i for i in all_items() if i["id"] == item_id), None)


def search_items(*, category=None, diets=(), exclude_allergens=(), exclude_ingredients=(),
                 max_price=None, min_price=None, max_spice=None, min_spice=None,
                 tags=(), query=None) -> list[dict]:
    order = {c: n for n, c in enumerate(categories())}
    ex_all = {a.lower() for a in exclude_allergens}
    ex_ing = [s.lower() for s in exclude_ingredients]
    want_tags = {t.lower() for t in tags}
    words = query.lower().split() if query else []
    scored = []
    for it in all_items():
        if category and it["category"] != category:
            continue
        if any(not DIET_RULES[d](it) for d in diets):
            continue
        if ex_all & set(it["allergens"]):
            continue
        if any(s in ing.lower() for s in ex_ing for ing in it["ingredients"]):
            continue
        if max_price is not None and it["price"] > max_price:
            continue
        if min_price is not None and it["price"] < min_price:
            continue
        if max_spice is not None and it["spice_level"] > max_spice:
            continue
        if min_spice is not None and it["spice_level"] < min_spice:
            continue
        if want_tags and not want_tags <= set(it["tags"]):
            continue
        score = 0
        if words:
            hay = " ".join([it["name"], it["description"], *it["ingredients"], *it["tags"]]).lower()
            score = sum(w in hay for w in words)
            if score == 0:
                continue
        scored.append((-score, order[it["category"]], it["price"], it))
    scored.sort(key=lambda t: t[:3])
    return [t[3] for t in scored]


def house_pairs(item_id: str) -> list[str]:
    """Curated 'pairs_well_with' from the menu, treated as symmetric. Never learned from other guests."""
    item = get_item(item_id)
    if not item:
        return []
    out = list(item.get("pairs_well_with", []))
    for other in all_items():
        if item_id in other.get("pairs_well_with", []) and other["id"] not in out:
            out.append(other["id"])
    return [i for i in out if get_item(i)]
