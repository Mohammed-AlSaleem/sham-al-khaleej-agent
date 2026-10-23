"""Tools exposed to the LLM, plus server-side enforcement of hard constraints.

Design note: the model decides *what* to search for, but allergies and diets stored in
memory are applied here regardless of what the model passes. A forgotten constraint in
the prompt therefore cannot produce an unsafe recommendation.
"""
import copy

from . import combos, config, memory, menu

CATEGORIES = menu.categories()

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_menu",
            "description": (
                "Search the restaurant menu. Returns real items with prices, ingredients, allergens and spice "
                "levels. The guest's stored allergies/diets are applied automatically. Call with no arguments "
                "to list everything."),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": CATEGORIES},
                    "diet": {"type": "array", "items": {"type": "string", "enum": sorted(menu.DIET_RULES)},
                             "description": "Diets the dish must satisfy."},
                    "exclude_allergens": {"type": "array", "items": {"type": "string", "enum": menu.allergen_vocab()}},
                    "exclude_ingredients": {"type": "array", "items": {"type": "string"},
                                            "description": "Ingredients to avoid, e.g. 'eggplant', 'chicken'."},
                    "max_price": {"type": "number"}, "min_price": {"type": "number"},
                    "max_spice": {"type": "integer", "minimum": 0, "maximum": 3},
                    "min_spice": {"type": "integer", "minimum": 0, "maximum": 3},
                    "tags": {"type": "array", "items": {"type": "string"},
                             "description": "e.g. high-protein, light, hearty, shareable, seafood."},
                    "query": {"type": "string", "description": "Keywords matched against name, description, ingredients, tags."},
                    "for_others": {"type": "boolean",
                                   "description": "True ONLY when searching for dishes another person at the table will eat. "
                                                  "The guest's own allergies and diets are then not applied (allergens are still listed)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_item",
            "description": "Get full details of one menu item by id, including any conflict with the guest's hard constraints and suggested pairings (the guest's own habits first, then the restaurant's house pairings).",
            "parameters": {"type": "object", "properties": {"item_id": {"type": "string"}}, "required": ["item_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_memory",
            "description": (
                "Save, change or remove a durable fact about the guest. Use op='set' for new facts AND for "
                "corrections (it replaces the previous fact for the same subject). Use op='remove' when the guest "
                "drops a preference entirely. kinds: diet (subject e.g. vegetarian|vegan|pescatarian|gluten-free|"
                "dairy-free), allergen (subject from the menu allergen list or a common word like peanuts/shrimp), "
                "ingredient, dish (subject = menu item id), category, spice (subject 'tolerance', "
                "value {'max_level': 0-3}), budget (subject 'max_item_price' or 'max_total', value {'amount': N}), other."),
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["set", "remove"]},
                    "kind": {"type": "string", "enum": list(memory.KINDS)},
                    "subject": {"type": "string"},
                    "polarity": {"type": "string", "enum": list(memory.POLARITIES),
                                 "description": "like / avoid / limit (limit for spice and budget). Ignored for diet and allergen."},
                    "strength": {"type": "string", "enum": ["hard", "soft"],
                                 "description": "hard = must never be violated (e.g. 'I can't eat X'); soft = taste."},
                    "value": {"type": "object"},
                    "note": {"type": "string", "description": "The guest's own words, short."},
                },
                "required": ["op", "kind", "subject"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_order",
            "description": "Save the order ONLY after the guest confirmed the quote. Units the guest will eat are checked against their hard constraints; units for other people (for_others) are not.",
            "parameters": {
                "type": "object",
                "properties": {"items": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string"},
                        "quantity": {"type": "integer", "minimum": 1, "description": "Total units of this item."},
                        "for_others": {"type": "integer", "minimum": 0,
                                       "description": "How many of those units are for OTHER people, not the guest (0 to quantity). "
                                                      "Only when the guest says so. Default 0."}},
                    "required": ["item_id"]}}},
                "required": ["items"],
            },
        },
    },
]


REMEMBER_COMBO_TOOL = {
    "type": "function",
    "function": {
        "name": "remember_combo",
        "description": (
            "Save or drop a pairing the guest likes to eat together, e.g. hummus with the mixed grill, or Ayran with "
            "kabsa. Use op='set' when they state or confirm a favourite pairing, op='drop' when they say they are "
            "over it or do not want it suggested. Needs two different menu item ids (find them with search_menu). "
            "Habits from past orders are learned automatically: do not call this for those."),
        "parameters": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["set", "drop"]},
                "item_ids": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2},
            },
            "required": ["op", "item_ids"],
        },
    },
}
TOOLS.append(REMEMBER_COMBO_TOOL)

_quote = copy.deepcopy(next(t for t in TOOLS if t["function"]["name"] == "record_order"))
_quote["function"]["name"] = "quote_order"
_quote["function"]["description"] = (
    "Price an order WITHOUT saving it. Call this before asking the guest to confirm, and show its lines and total "
    "exactly as returned (never add up prices yourself). Returns problems if an item conflicts with the guest's "
    "constraints, and warnings for items marked for other people.")
TOOLS.append(_quote)


# ---------------------------------------------------------------- constraint helpers
def hard_constraints(prefs: list[dict]) -> dict:
    return {
        "diets": sorted({p["subject"] for p in prefs if p["kind"] == "diet" and p["subject"] in menu.DIET_RULES}),
        "allergens": sorted({p["subject"] for p in prefs if p["kind"] == "allergen"}),
        "ingredients": sorted({p["subject"] for p in prefs
                               if p["kind"] == "ingredient" and p["polarity"] == "avoid" and p["strength"] == "hard"}),
    }


def item_conflicts(item: dict, prefs: list[dict]) -> list[str]:
    hc = hard_constraints(prefs)
    out = []
    out += [f"contains {a}" for a in hc["allergens"] if a in item["allergens"]]
    out += [f"not {d}" for d in hc["diets"] if not menu.DIET_RULES[d](item)]
    out += [f"contains {s}" for s in hc["ingredients"] if any(s in ing.lower() for ing in item["ingredients"])]
    return out


def _safe(item_id: str, prefs: list[dict]) -> bool:
    item = menu.get_item(item_id)
    return item is not None and not item_conflicts(item, prefs)


def _name(item_id: str) -> str:
    return menu.get_item(item_id)["name"]


def _detail(c: dict) -> str:
    bits = []
    if c["source"] in ("stated", "both"):
        bits.append("stated favourite")
    if c["source"] in ("observed", "both"):
        bits.append(f"ordered together {c['co_orders']} times, last {c['last_ordered'][:10]}")
    return "; ".join(bits)


def safe_personal_combos(user_id: str, prefs: list[dict], now=None, limit: int = 5) -> list[dict]:
    """The guest's own pairings, minus any that involve an item violating a hard constraint."""
    found = [c for c in combos.personal_combos(user_id, now) if all(_safe(i, prefs) for i in c["items"])]
    return found[:limit]


def combo_prompt_lines(user_id: str, prefs: list[dict], now=None) -> list[str]:
    return [f"{_name(a)} + {_name(b)} ({_detail(c)})"
            for c in safe_personal_combos(user_id, prefs, now) for a, b in [c["items"]]]


def combos_for_display(user_id: str, prefs: list[dict], now=None) -> list[dict]:
    return [{"items": c["items"], "names": [_name(i) for i in c["items"]], "detail": _detail(c),
             "source": c["source"], "score": c["score"]} for c in safe_personal_combos(user_id, prefs, now)]


def pairings_for_item(user_id: str, item_id: str, prefs: list[dict], now=None) -> list[dict]:
    """Suggested partners for one item: the guest's own habits first, then house pairings.
    Anything violating a hard constraint is removed; a pair the guest dropped is not re-suggested."""
    out, seen = [], set()
    personal = combos.personal_combos(user_id, now)
    for c in personal:
        if item_id in c["items"]:
            other = c["items"][1] if c["items"][0] == item_id else c["items"][0]
            if _safe(other, prefs):
                out.append({"item_id": other, "name": _name(other), "price": menu.get_item(other)["price"],
                            "source": "guest_stated" if c["source"] == "stated" else "guest_habit",
                            "detail": _detail(c)})
                seen.add(other)
    shown = {tuple(c["items"]) for c in personal}
    dropped = {pair for pair, st in memory.get_combo_state(user_id).items() if st["stance"] == "drop"}
    for other in menu.house_pairs(item_id):
        pair = tuple(sorted((item_id, other)))
        if other in seen or not _safe(other, prefs) or (pair in dropped and pair not in shown):
            continue
        out.append({"item_id": other, "name": _name(other), "price": menu.get_item(other)["price"],
                    "source": "house", "detail": "restaurant's suggested pairing"})
    return out


def _build_order(args: dict, prefs: list[dict]):
    """Price an order from the menu and validate it. All arithmetic happens here, never in the LLM."""
    lines, problems, warnings, total = [], [], [], 0.0
    for entry in args.get("items") or []:
        item = menu.get_item(entry.get("item_id", ""))
        if not item:
            problems.append(f"unknown item {entry.get('item_id')}")
            continue
        qty = max(int(entry.get("quantity") or 1), 1)
        others = int(entry.get("for_others") or 0)
        if not 0 <= others <= qty:
            problems.append(f"{item['name']}: for_others must be between 0 and quantity ({qty})")
            continue
        conflicts = item_conflicts(item, prefs)
        if conflicts and qty - others > 0:
            problems.append(f"{item['name']} conflicts with the guest's constraints ({', '.join(conflicts)}). "
                            f"If it is only for other people, set for_others to the number of units they will eat.")
            continue
        if conflicts:
            warnings.append(f"{item['name']} ({', '.join(conflicts)}) is for other people: keep it separate "
                            f"from the guest's food and tell the kitchen.")
        total += item["price"] * qty
        lines.append({"item_id": item["id"], "name": item["name"], "unit_price": item["price"], "quantity": qty,
                      "for_others": others, "subtotal": item["price"] * qty})
    return lines, total, problems, warnings


def _compact(i: dict) -> dict:
    return {k: i[k] for k in ("id", "name", "category", "price", "spice_level", "vegetarian", "vegan",
                              "description", "ingredients", "allergens", "tags")}


# ---------------------------------------------------------------- dispatcher
def execute_tool(name: str, args: dict, ctx: dict) -> dict:
    """ctx: user_id, conversation_id, source_message_id, memory_events (list, mutated)."""
    uid = ctx["user_id"]

    if name == "search_menu":
        prefs = memory.get_active_preferences(uid)
        hc = hard_constraints(prefs)
        if args.get("for_others"):   # dishes for other people: the guest's own constraints do not apply
            hc = {"diets": [], "allergens": [], "ingredients": []}
        diets = args.get("diet") or []
        bad = [d for d in diets if d not in menu.DIET_RULES]
        if bad:
            return {"error": f"unknown diet {bad}; allowed: {sorted(menu.DIET_RULES)}"}
        base = dict(category=args.get("category"), exclude_allergens=args.get("exclude_allergens") or [],
                    exclude_ingredients=args.get("exclude_ingredients") or [], max_price=args.get("max_price"),
                    min_price=args.get("min_price"), max_spice=args.get("max_spice"), min_spice=args.get("min_spice"),
                    tags=args.get("tags") or [], query=args.get("query"))
        requested = menu.search_items(diets=diets, **base)
        final_args = dict(base)
        final_args["exclude_allergens"] = sorted(set(base["exclude_allergens"]) | set(hc["allergens"]))
        final_args["exclude_ingredients"] = sorted(set(base["exclude_ingredients"]) | set(hc["ingredients"]))
        final = menu.search_items(diets=sorted(set(diets) | set(hc["diets"])), **final_args)
        kept = {i["id"] for i in final}
        return {"count": len(final), "items": [_compact(i) for i in final],
                "hard_constraints_applied": hc,
                "hidden_by_hard_constraints": [i["name"] for i in requested if i["id"] not in kept]}

    if name == "get_item":
        item = menu.get_item(args.get("item_id", ""))
        if not item:
            return {"error": "unknown item_id"}
        prefs = memory.get_active_preferences(uid)
        return {"item": _compact(item), "conflicts_with_guest_constraints": item_conflicts(item, prefs),
                "pairings": pairings_for_item(uid, item["id"], prefs)}

    if name == "update_memory":
        op, kind, subject = args.get("op"), args.get("kind"), (args.get("subject") or "").strip()
        if not subject or kind not in memory.KINDS:
            return {"error": f"kind must be one of {list(memory.KINDS)} and subject is required"}
        subjects = [subject.lower()]
        if kind == "allergen":
            mapped = menu.normalize_allergen(subject)
            if not mapped:
                return {"error": f"'{subject}' is not in the menu allergen list {menu.allergen_vocab()}. "
                                 f"Store it with kind='ingredient', polarity='avoid', strength='hard' instead."}
            subjects = mapped
        if kind == "diet" and subjects[0] not in menu.DIET_RULES:
            return {"error": f"unknown diet; supported: {sorted(menu.DIET_RULES)}. Use kind='other' for anything else."}
        if kind == "dish" and not menu.get_item(subjects[0]):
            return {"error": "for kind='dish' the subject must be a menu item id (use search_menu to find it)"}
        results = []
        for s in subjects:
            if op == "remove":
                results.append(memory.remove_preference(uid, kind, s))
            else:
                results.append(memory.set_preference(
                    uid, kind, s, args.get("polarity") or "like", args.get("strength") or "soft",
                    args.get("value"), args.get("note"), ctx.get("source_message_id")))
        ctx.setdefault("memory_events", []).extend(results)
        return {"results": results}

    if name == "remember_combo":
        ids = args.get("item_ids") or []
        if len(ids) != 2 or ids[0] == ids[1] or not all(menu.get_item(i) for i in ids):
            return {"error": "item_ids must be two different valid menu item ids (use search_menu to find them)"}
        stance = "like" if args.get("op") == "set" else "drop"
        res = memory.set_combo_event(uid, ids[0], ids[1], stance, ctx.get("source_message_id"))
        ctx.setdefault("memory_events", []).append(
            {"action": res["action"], "kind": "combo", "subject": " + ".join(_name(i) for i in res["items"])})
        out = {"result": res}
        if stance == "like" and not all(_safe(i, memory.get_active_preferences(uid)) for i in ids):
            out["note"] = "Saved, but it will not be suggested while it conflicts with the guest's dietary constraints."
        return out

    if name in ("quote_order", "record_order"):
        prefs = memory.get_active_preferences(uid)
        lines, total, problems, warnings = _build_order(args, prefs)
        if problems or not lines:
            return {"error": "order not " + ("priced" if name == "quote_order" else "recorded"),
                    "problems": problems or ["no items"]}
        result = {"total_sar": total, "lines": lines, "warnings": warnings}
        if name == "quote_order":
            result["saved"] = False
            return result
        oid = memory.record_order(uid, ctx.get("conversation_id"),
                                  [(l["item_id"], l["quantity"], l["for_others"]) for l in lines], total)
        result.update(order_id=oid, saved=True,
                      items=[{"item_id": l["item_id"], "quantity": l["quantity"]} for l in lines])
        return result

    return {"error": f"unknown tool {name}"}
