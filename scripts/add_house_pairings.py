"""Add curated 'pairs_well_with' lists to data/menu.json. Safe to re-run: never overwrites existing lists.

Run from the project root:  python -m scripts.add_house_pairings
Note: this rewrites the file with standard JSON formatting (one key per line).
"""
import json
from pathlib import Path

MENU = Path(__file__).resolve().parent.parent / "data" / "menu.json"

# Curated by the restaurant, NOT learned from guests. Treated as symmetric at lookup time.
PAIRINGS = {
    "st-hummus": ["mn-mixedgrill", "mn-shawarma"],
    "st-baba": ["mn-mixedgrill", "st-fattoush"],
    "st-muhammara": ["mn-mixedgrill"],
    "st-falafel": ["dr-lemonmint", "st-fattoush"],
    "st-fattoush": ["mn-hammour", "mn-mixedgrill"],
    "st-sambousek": ["dr-lemonmint"],
    "st-gambari": ["dr-lemonmint"],
    "mn-shawarma": ["dr-ayran", "st-fattoush"],
    "mn-kabsa": ["dr-ayran", "st-fattoush"],
    "mn-mixedgrill": ["st-hummus", "st-fattoush", "dr-lemonmint"],
    "mn-hammour": ["st-fattoush", "dr-lemonmint"],
    "mn-harra": ["dr-ayran", "dr-lemonmint"],
    "mn-cauliflower": ["dr-lemonmint"],
    "mn-maqluba": ["dr-ayran", "st-fattoush"],
    "mn-manakish": ["dr-lemonmint"],
    "ds-kunafa": ["dr-gahwa"],
    "ds-umali": ["dr-gahwa"],
    "ds-datepudding": ["dr-gahwa"],
}


def main() -> None:
    data = json.loads(MENU.read_text(encoding="utf-8"))
    ids = {i["id"] for i in data["items"]}
    added = skipped = 0
    for item in data["items"]:
        want = PAIRINGS.get(item["id"])
        if want is None or "pairs_well_with" in item:
            continue
        missing = [w for w in want if w not in ids]
        if missing:
            print(f"skip {item['id']}: unknown item ids {missing}")
            skipped += 1
            continue
        item["pairs_well_with"] = want
        added += 1
    MENU.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"house pairings added to {added} items ({skipped} skipped) -> {MENU}")


if __name__ == "__main__":
    main()
