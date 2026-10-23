"""Seed a demo guest with order history so pairing memory can be shown without placing many real orders.

Run from the project root:  python -m scripts.seed_demo [username]
Then log in with that username (default: demo-combo).
"""
import sys
from datetime import timedelta

from app import combos, memory, menu


def _order(uid: str, items: list[str], days_ago: int, now) -> None:
    total = sum(menu.get_item(i)["price"] for i in items)
    memory.record_order(uid, None, [(i, 1) for i in items], total,
                        created_at=memory.fmt_ts(now - timedelta(days=days_ago)))


def seed(username: str = "demo-combo") -> dict:
    memory.init_db()
    name = memory.normalize_username(username)
    with memory.db() as c:                       # wipe any previous demo data (cascades to orders, combos...)
        c.execute("DELETE FROM users WHERE username = ?", (name,))
    user = memory.get_or_create_user(name)
    memory.create_conversation(user["id"])       # makes them a recognised "returning" guest
    now = memory.utcnow()
    for d in (3, 10, 17, 24):                    # strong, recent habit
        _order(user["id"], ["mn-kabsa", "dr-ayran"], d, now)
    for d in (5, 19):                            # newer habit (just reached the 2-order threshold)
        _order(user["id"], ["st-hummus", "mn-mixedgrill"], d, now)
    for d in (150, 160, 170):                    # old habit: should have faded away
        _order(user["id"], ["st-falafel", "dr-lemonmint"], d, now)
    return user


if __name__ == "__main__":
    u = seed(sys.argv[1] if len(sys.argv) > 1 else "demo-combo")
    print(f"Seeded guest '{u['username']}'. Active pairings:")
    for c in combos.personal_combos(u["id"]):
        print("  ", " + ".join(menu.get_item(i)["name"] for i in c["items"]), "| score", c["score"], "|", c["source"])
    print("(Falafel + Lemonade, ordered ~5 months ago, should NOT appear: it has decayed.)")
