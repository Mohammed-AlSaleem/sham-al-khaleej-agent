"""Live check of pairing behaviour against the REAL model (needs DEEPSEEK_API_KEY in .env).

Run from the project root:  python -m scripts.live_combo_check [repeats]     (default 3)
Uses a temporary database, so your real data is untouched. The model is non-deterministic,
so each check is repeated and reported as passes/runs.
"""
import os
import sys
import tempfile

from app import agent, config, memory
from scripts.seed_demo import seed

config.DB_PATH = os.path.join(tempfile.mkdtemp(), "live_combo.db")
memory.init_db()
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3
AYRAN = ("ayran", "عيران", "أيران")


def say(username: str, text: str) -> str:
    user = memory.get_or_create_user(username)
    cid = memory.create_conversation(user["id"])      # a fresh conversation every time
    return agent.run_turn(user, cid, text)["reply"]


def mentions_ayran(reply: str) -> bool:
    return any(w in reply.lower() for w in AYRAN)


def run(label: str, fn) -> None:
    ok = sum(bool(fn(i)) for i in range(N))
    print(f"{'PASS' if ok == N else 'FAIL' if ok == 0 else 'PART'}  {ok}/{N}  {label}")


def habit_surfaced(i):
    seed(f"live-habit-{i}")
    return mentions_ayran(say(f"live-habit-{i}", "I'll have the lamb kabsa please."))


def constraint_wins(i):
    u = seed(f"live-dairy-{i}")
    memory.set_preference(u["id"], "diet", "dairy-free", "like")
    return not mentions_ayran(say(f"live-dairy-{i}", "I'll have the lamb kabsa please."))


def stated_saved(i):
    say(f"live-stated-{i}", "I love having hummus with the mixed grill.")
    uid = memory.get_or_create_user(f"live-stated-{i}")["id"]
    return memory.get_combo_state(uid).get(("mn-mixedgrill", "st-hummus"), {}).get("stance") == "like"


def drop_respected(i):
    u = seed(f"live-drop-{i}")
    say(f"live-drop-{i}", "I'm tired of Ayran with the kabsa, please stop suggesting it.")
    dropped = memory.get_combo_state(u["id"]).get(("dr-ayran", "mn-kabsa"), {}).get("stance") == "drop"
    return dropped and not mentions_ayran(say(f"live-drop-{i}", "I'll have the lamb kabsa please."))


run("learned habit is suggested (Kabsa -> Ayran)", habit_surfaced)
run("dairy-free guest is NOT offered Ayran", constraint_wins)
run("stated pairing is saved (hummus + mixed grill)", stated_saved)
run("dropped pairing is saved and no longer suggested", drop_respected)
