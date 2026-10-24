"""Add curated 'removable_ingredients' to data/menu.json: what the kitchen can leave out on request.

Run from the project root:  python -m scripts.add_customizations
Safe to re-run: never overwrites an item that already has a list. EDIT THE LISTS to match your restaurant:
the assistant only offers modifications that appear here, so it never promises something the kitchen cannot do.
"""
import json
from pathlib import Path

MENU = Path(__file__).resolve().parent.parent / "data" / "menu.json"

REMOVABLE = {
    "dr-lemonmint": ["sugar"],
    "dr-ayran": ["mint", "salt"],
    "dr-gahwa": ["dates"],
    "st-hummus": ["garlic", "pita bread"],
    "st-baba": ["garlic", "pomegranate"],
    "st-muhammara": ["chili", "breadcrumbs"],
    "st-falafel": ["pickled turnip", "tahini"],
    "st-fattoush": ["radish", "cucumber", "pita chips", "pomegranate molasses"],
    "st-sambousek": ["mint"],
    "st-gambari": ["chili", "coriander", "garlic"],
    "mn-shawarma": ["garlic sauce", "pickles", "potato fries", "flatbread"],
    "mn-kabsa": ["raisins", "almonds"],
    "mn-mixedgrill": ["onion", "tomato"],
    "mn-hammour": ["chili", "coriander"],
    "mn-harra": ["coriander"],
    "mn-cauliflower": ["pine nuts", "pomegranate"],
    "mn-maqluba": ["potato", "cinnamon"],
    "mn-manakish": ["sesame seeds"],
    "ds-kunafa": ["pistachios", "orange blossom water"],
    "ds-umali": ["almonds", "raisins", "coconut"],
}


def main() -> None:
    data = json.loads(MENU.read_text(encoding="utf-8"))
    added = skipped = 0
    for item in data["items"]:
        want = REMOVABLE.get(item["id"])
        if want is None or "removable_ingredients" in item:
            continue
        have = {i.lower() for i in item["ingredients"]}
        missing = [w for w in want if w.lower() not in have]
        if missing:
            print(f"skip {item['id']}: not in its ingredient list: {missing}")
            skipped += 1
            continue
        item["removable_ingredients"] = want
        added += 1
    MENU.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"removable_ingredients added to {added} items ({skipped} skipped) -> {MENU}")


if __name__ == "__main__":
    main()
