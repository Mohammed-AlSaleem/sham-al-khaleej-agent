import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, memory, menu
from .tools import combos_for_display

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    memory.init_db()
    yield


app = FastAPI(title="Sham Al-Khaleej Ordering Assistant", lifespan=lifespan)


class LoginReq(BaseModel):
    username: str


class ChatReq(BaseModel):
    username: str
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None   # omit to start a new conversation


def _user(username: str) -> dict:
    try:
        return memory.get_or_create_user(username)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _public_pref(p: dict) -> dict:
    return {k: p[k] for k in ("kind", "subject", "polarity", "strength", "value", "note", "status", "created_at")}


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/login")
def login(req: LoginReq):
    user = _user(req.username)
    prefs = memory.get_active_preferences(user["id"])
    return {"username": user["username"], "is_returning": user["is_returning"],
            "memory": [_public_pref(p) for p in prefs],
            "combos": combos_for_display(user["id"], prefs)}


@app.post("/api/chat")
def chat(req: ChatReq):
    user = _user(req.username)
    cid = req.conversation_id
    if cid and not memory.conversation_belongs_to(cid, user["id"]):
        raise HTTPException(404, "Conversation not found for this user")
    cid = cid or memory.create_conversation(user["id"])
    try:
        out = agent.run_turn(user, cid, req.message)
    except Exception:
        logging.exception("agent failure")
        raise HTTPException(502, "The assistant is temporarily unavailable. Please try again.")
    return {"conversation_id": cid, **out}


@app.get("/api/memory")
def get_memory(username: str, include_history: bool = False):
    user = _user(username)
    rows = memory.get_preference_history(user["id"]) if include_history else memory.get_active_preferences(user["id"])
    return {"username": user["username"], "memory": [_public_pref(p) for p in rows],
            "combos": combos_for_display(user["id"], memory.get_active_preferences(user["id"]))}


@app.get("/api/menu")
def get_menu():
    return menu.load_menu()


static_dir = Path(__file__).resolve().parent.parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
