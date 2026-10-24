"""Tools exposed to the LLM, plus server-side enforcement of hard constraints.

Design note: the model decides *what* to search for, but allergies and diets stored in
memory are applied here regardless of what the model passes. A forgotten constraint in
the prompt therefore cannot produce an unsafe recommendation.
"""
import copy
import re

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
                "corrections (it replaces the previous fact for the same subject); op='remove' when the guest drops "
                "a preference. Save immediately and tell the guest afterwards: never ask permission first. kinds: "
                "diet (vegetarian|vegan|pescatarian|gluten-free|dairy-free|halal), allergen (an allergy, e.g. nuts, "
                "peanuts, shellfish), ingredient (ANY other 'no X' / 'X-free' restriction such as sugar, onion, "
                "coffee: polarity 'avoid' with strength 'hard' for restrictions, 'soft' for mere dislikes), dish "
                "(subject = menu item id), category, spice (subject 'tolerance', value {'max_level': 0-3}), budget "
                "(subject 'max_item_price' or 'max_total', value {'amount': N}), other (a note the menu cannot "
                "verify, e.g. keto, diabetic, low-carb: stored but NOT enforced). The server also routes 'sugar-free' "
                "or 'no sugar' style subjects to a hard ingredient exclusion for you. Read the returned note and "
                "tell the guest what it means."),
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
                                                      "Only when the guest says so. Default 0."},
                        "without": {"type": "array", "items": {"type": "string"},
                                    "description": "Ingredients to leave out for ALL units on this line. Only those "
                                                   "listed in the item's removable_ingredients. Use a separate line "
                                                   "for units prepared differently."}},
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

_rec = next(t for t in TOOLS if t["function"]["name"] == "record_order")
_rec["function"]["description"] = (
    "Save the order ONLY after the guest has confirmed a quote. Normally call it with NO items: it records exactly "
    "what quote_order last returned in this conversation, including for_others and without. Pass items only to "
    "record a different order. Units the guest will eat are checked against their hard constraints.")
_rec["function"]["parameters"]["required"] = []
_quote = copy.deepcopy(_rec)
_quote["function"]["parameters"]["required"] = ["items"]
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


def _conflict_parts(item: dict, prefs: list[dict], removed=()) -> dict:
    hc = hard_constraints(prefs)
    gone = {r.lower() for r in removed}
    return {
        "allergens": [a for a in hc["allergens"] if a in item["allergens"]],      # removal never clears these
        "diets": [d for d in hc["diets"] if not menu.DIET_RULES[d](item)],         # ...nor these
        "ingredients": [s for s in hc["ingredients"]
                        if any(s in ing.lower() for ing in item["ingredients"] if ing.lower() not in gone)],
    }


def item_conflicts(item: dict, prefs: list[dict], removed=()) -> list[str]:
    """Reasons the guest's hard constraints rule this item out. `removed` = ingredients left out of the order;
    that can satisfy an ingredient exclusion (sugar) but never an allergy or a diet."""
    parts = _conflict_parts(item, prefs, removed)
    return ([f"contains {a}" for a in parts["allergens"]] + [f"not {d}" for d in parts["diets"]]
            + [f"contains {s}" for s in parts["ingredients"]])


def removable_fix(item: dict, prefs: list[dict]) -> list[str] | None:
    """Ingredients to leave out so the item fits the guest's constraints, or None if it cannot be made to fit."""
    parts = _conflict_parts(item, prefs)
    if parts["allergens"] or parts["diets"] or not parts["ingredients"]:
        return None
    removable = {r.lower(): r for r in item.get("removable_ingredients", [])}
    fix = set()
    for s in parts["ingredients"]:
        matching = [ing for ing in item["ingredients"] if s in ing.lower()]
        if not all(m.lower() in removable for m in matching):
            return None
        fix |= {removable[m.lower()] for m in matching}
    return sorted(fix)


_FREE_RE = re.compile(r"^(?:(.+?)[\s-]free|(?:no|without|avoid|free of)\s+(.+))$")


def _singular(word: str) -> str:
    """'onions' -> 'onion' so substring matching against ingredient names works."""
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _route_restriction(kind: str, subject: str, args: dict):
    """Send restrictions to a kind the code can ENFORCE, whatever kind the model picked.
    Returns (kind, subject, polarity, strength, note)."""
    s = subject.strip().lower()
    polarity, strength = args.get("polarity") or "like", args.get("strength") or "soft"
    if kind == "allergen" and not menu.normalize_allergen(s):
        x = _singular(s)
        return ("ingredient", x, "avoid", "hard",
                f"'{s}' is not in the menu's allergen list, so it was saved as a hard ingredient exclusion "
                f"(matched by ingredient name only). Tell the guest to inform the kitchen directly.")
    if kind in ("diet", "other"):
        if s in menu.DIET_RULES:
            return "diet", s, polarity, strength, None
        m = _FREE_RE.match(s)
        if m:
            x = (m.group(1) or m.group(2)).strip()
            if menu.normalize_allergen(x):
                return "allergen", x, "avoid", "hard", f"saved as the allergen '{x}'"
            x = _singular(x)
            return ("ingredient", x, "avoid", "hard",
                    f"saved as a hard ingredient exclusion '{x}' (matched by ingredient name only)")
        return ("other", s, polarity, "soft",
                "Saved as a note only: the menu has no data to enforce this, so dishes cannot be checked against it. "
                "Tell the guest that plainly.")
    return kind, s, polarity, strength, None


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
        removable = {r.lower(): r for r in item.get("removable_ingredients", [])}
        asked = [str(x).strip() for x in (entry.get("without") or []) if str(x).strip()]
        bad = [x for x in asked if x.lower() not in removable]
        if bad:
            problems.append(f"{item['name']} cannot be made without {bad}. "
                            f"Removable: {sorted(removable.values()) or 'nothing'}.")
            continue
        removed = sorted({removable[x.lower()] for x in asked})
        conflicts = item_conflicts(item, prefs, removed)
        if conflicts and qty - others > 0:
            fix = removable_fix(item, prefs)
            hint = (f" It can be ordered without {fix}." if fix and not removed else
                    " If it is only for other people, set for_others to the number of units they will eat.")
            problems.append(f"{item['name']} conflicts with the guest's constraints ({', '.join(conflicts)}).{hint}")
            continue
        if removed and item_conflicts(item, prefs) and not conflicts:
            warnings.append(f"{item['name']} is prepared without {', '.join(removed)} to fit the guest's "
                            f"restriction: ask the kitchen to confirm, in case of cross-contact.")
        elif conflicts:
            warnings.append(f"{item['name']} ({', '.join(conflicts)}) is for other people: keep it separate "
                            f"from the guest's food and tell the kitchen.")
        total += item["price"] * qty
        lines.append({"item_id": item["id"], "name": item["name"], "unit_price": item["price"], "quantity": qty,
                      "for_others": others, "without": removed, "subtotal": item["price"] * qty})
    return lines, total, problems, warnings


def _compact(i: dict) -> dict:
    out = {k: i[k] for k in ("id", "name", "category", "price", "spice_level", "vegetarian", "vegan",
                             "description", "ingredients", "allergens", "tags")}
    out["removable_ingredients"] = i.get("removable_ingredients", [])
    return out


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
        hidden = [i for i in requested if i["id"] not in kept]
        offered, still_hidden = [], []
        for i in hidden:
            fix = removable_fix(i, prefs)
            if fix:      # fits the guest's restriction if an ingredient is left out
                offered.append(dict(_compact(i), must_order_without=fix))
            else:
                still_hidden.append(i)
        items = [_compact(i) for i in final] + offered
        return {"count": len(items), "items": items,
                "hard_constraints_applied": hc,
                "hidden_by_hard_constraints": [i["name"] for i in still_hidden],
                "hidden_reasons": [{"item": i["name"], "because": ", ".join(item_conflicts(i, prefs))}
                                   for i in still_hidden]}

    if name == "get_item":
        item = menu.get_item(args.get("item_id", ""))
        if not item:
            return {"error": "unknown item_id"}
        prefs = memory.get_active_preferences(uid)
        return {"item": _compact(item), "conflicts_with_guest_constraints": item_conflicts(item, prefs),
                "removable_ingredients": item.get("removable_ingredients", []),
                "fixable_by_removing": removable_fix(item, prefs),
                "pairings": pairings_for_item(uid, item["id"], prefs)}

    if name == "update_memory":
        op, kind, subject = args.get("op"), args.get("kind"), (args.get("subject") or "").strip()
        if not subject or kind not in memory.KINDS:
            return {"error": f"kind must be one of {list(memory.KINDS)} and subject is required"}
        original = (kind, subject.lower())
        kind, subject, polarity, strength, note = _route_restriction(kind, subject, args)
        if kind == "dish" and not menu.get_item(subject):
            return {"error": "for kind='dish' the subject must be a menu item id (use search_menu to find it)"}
        subjects = menu.normalize_allergen(subject) if kind == "allergen" else [subject]
        results = []
        for sub in subjects:
            if op == "remove":
                results.append(memory.remove_preference(uid, kind, sub))
            else:
                results.append(memory.set_preference(
                    uid, kind, sub, polarity, strength, args.get("value"), args.get("note"), ctx.get("source_message_id")))
        if op == "remove" and (kind, subject) != original:        # also clear the literal form the model used
            results.append(memory.remove_preference(uid, *original))
        ctx.setdefault("memory_events", []).extend(results)
        out = {"results": results}
        if op != "remove" and kind == "ingredient" and polarity == "avoid" and strength == "hard":
            affected, fixes = [], {}
            for i in menu.all_items():
                matching = [ing for ing in i["ingredients"] if subject in ing.lower()]
                if not matching:
                    continue
                affected.append(i["name"])
                removable = {r.lower(): r for r in i.get("removable_ingredients", [])}
                if all(m.lower() in removable for m in matching):
                    fixes[i["name"]] = [removable[m.lower()] for m in matching]
            out["dishes_affected"] = affected
            if fixes:
                out["can_be_ordered_without"] = fixes
            if not affected:
                note = ((note or "") + " No menu dish lists this ingredient, so nothing is hidden: tell the guest "
                        "the menu data cannot verify it.").strip()
        if note and op != "remove":
            out["note"] = note
        return out

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
        cid = ctx.get("conversation_id")
        entries = args.get("items")
        if name == "record_order" and not entries:           # confirm exactly what was last quoted
            entries = memory.get_pending_quote(cid) if cid else None
            if not entries:
                return {"error": "order not recorded",
                        "problems": ["nothing has been quoted in this conversation yet: call quote_order first"]}
        lines, total, problems, warnings = _build_order({"items": entries}, prefs)
        if problems or not lines:
            if name == "quote_order" and cid:
                memory.clear_pending_quote(cid)               # never leave a stale quote that could be confirmed
            return {"error": "order not " + ("priced" if name == "quote_order" else "recorded"),
                    "problems": problems or ["no items"]}
        result = {"total_sar": total, "lines": lines, "warnings": warnings}
        if name == "quote_order":
            if cid:
                memory.save_pending_quote(cid, entries)
            result["saved"] = False
            result["next"] = "When the guest confirms, call record_order with NO items to record exactly this quote."
            return result
        oid = memory.record_order(uid, cid, [(l["item_id"], l["quantity"], l["for_others"], l["without"])
                                              for l in lines], total)
        if cid:
            memory.clear_pending_quote(cid)
        result.update(order_id=oid, saved=True,
                      items=[{"item_id": l["item_id"], "quantity": l["quantity"]} for l in lines])
        return result

    return {"error": f"unknown tool {name}"}
