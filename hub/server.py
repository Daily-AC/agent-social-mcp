"""
Agent Social Hub — 轻量 HTTP 消息中转服务
"""

import time
import sqlite3
import threading
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

# ── Config ──────────────────────────────────────────────

DB_PATH = "messages.db"
API_KEY: Optional[str] = None  # Set via env or startup arg
MESSAGE_RETENTION_DAYS = 7
CLEANUP_INTERVAL = 3600  # 1 hour
MAX_MESSAGE_LENGTH = 4096  # 防止超大 payload
RATE_LIMIT_PER_MIN = 60  # 每 agent 每分钟最多 60 条

# API Key → Agent Name 绑定（防止身份伪造）
KEY_AGENT_MAP: dict[str, str] = {}  # 启动时从环境变量加载

# ── Database ────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                name TEXT PRIMARY KEY,
                description TEXT DEFAULT '',
                capabilities TEXT DEFAULT '[]',
                registered_at REAL NOT NULL,
                last_seen REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                receiver TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_receiver ON messages(receiver, timestamp)")


def cleanup_old_messages():
    """Delete messages older than retention period."""
    cutoff = time.time() - MESSAGE_RETENTION_DAYS * 86400
    with get_db() as conn:
        conn.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))


# ── Background cleanup ──────────────────────────────────

def _cleanup_loop():
    while True:
        try:
            cleanup_old_messages()
        except Exception:
            pass
        time.sleep(CLEANUP_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    t = threading.Thread(target=_cleanup_loop, daemon=True)
    t.start()
    yield


# ── App ─────────────────────────────────────────────────

app = FastAPI(title="Agent Social Hub", version="1.0.0", lifespan=lifespan)


# ── Auth ────────────────────────────────────────────────

def check_auth(x_api_key: Optional[str] = Header(None)) -> Optional[str]:
    """验证 API Key，返回绑定的 agent name（如果有）"""
    if API_KEY and x_api_key != API_KEY:
        if x_api_key not in KEY_AGENT_MAP:
            raise HTTPException(status_code=401, detail="Invalid API key")
    return KEY_AGENT_MAP.get(x_api_key or "")


# ── Models ──────────────────────────────────────────────

class AgentRegister(BaseModel):
    name: str
    description: str = ""
    capabilities: list[str] = []


class Message(BaseModel):
    sender: str
    receiver: str
    content: str
    timestamp: Optional[float] = None


class Broadcast(BaseModel):
    sender: str
    content: str
    timestamp: Optional[float] = None


# ── Routes ──────────────────────────────────────────────

@app.post("/api/agents/register")
def register_agent(agent: AgentRegister, x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    import json
    now = time.time()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO agents (name, description, capabilities, registered_at, last_seen)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 description=excluded.description,
                 capabilities=excluded.capabilities,
                 last_seen=excluded.last_seen""",
            (agent.name, agent.description, json.dumps(agent.capabilities), now, now),
        )
    return {"ok": True, "agent": agent.name}


@app.get("/api/agents")
def list_agents(x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    import json
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM agents ORDER BY last_seen DESC").fetchall()
    return [
        {
            "name": r["name"],
            "description": r["description"],
            "capabilities": json.loads(r["capabilities"]),
            "registered_at": r["registered_at"],
            "last_seen": r["last_seen"],
        }
        for r in rows
    ]


@app.get("/api/agents/{name}")
def get_agent(name: str, x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    import json
    with get_db() as conn:
        r = conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return {
        "name": r["name"],
        "description": r["description"],
        "capabilities": json.loads(r["capabilities"]),
        "registered_at": r["registered_at"],
        "last_seen": r["last_seen"],
    }


@app.post("/api/messages")
def send_message(msg: Message, x_api_key: Optional[str] = Header(None)):
    bound_name = check_auth(x_api_key)
    # 安全：如果 Key 绑定了 agent，强制覆写 sender（防伪造身份）
    if bound_name:
        msg.sender = bound_name
    # 安全：消息长度限制
    if len(msg.content) > MAX_MESSAGE_LENGTH:
        raise HTTPException(status_code=413, detail=f"Message too long (max {MAX_MESSAGE_LENGTH} chars)")
    ts = msg.timestamp or time.time()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO messages (sender, receiver, content, timestamp) VALUES (?, ?, ?, ?)",
            (msg.sender, msg.receiver, msg.content, ts),
        )
        # Update sender last_seen
        conn.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (ts, msg.sender))
    return {"ok": True, "timestamp": ts}


@app.get("/api/messages")
def get_messages(
    to: str,
    since: Optional[float] = Query(None),
    limit: int = Query(50, le=200),
    x_api_key: Optional[str] = Header(None),
):
    check_auth(x_api_key)
    with get_db() as conn:
        if since:
            rows = conn.execute(
                "SELECT * FROM messages WHERE receiver = ? AND timestamp > ? ORDER BY timestamp DESC LIMIT ?",
                (to, since, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE receiver = ? ORDER BY timestamp DESC LIMIT ?",
                (to, limit),
            ).fetchall()
    return [
        {
            "id": r["id"],
            "sender": r["sender"],
            "receiver": r["receiver"],
            "content": r["content"],
            "timestamp": r["timestamp"],
        }
        for r in rows
    ]


@app.post("/api/broadcast")
def broadcast(msg: Broadcast, x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    ts = msg.timestamp or time.time()
    with get_db() as conn:
        agents = conn.execute("SELECT name FROM agents WHERE name != ?", (msg.sender,)).fetchall()
        for a in agents:
            conn.execute(
                "INSERT INTO messages (sender, receiver, content, timestamp) VALUES (?, ?, ?, ?)",
                (msg.sender, a["name"], msg.content, ts),
            )
        conn.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (ts, msg.sender))
    return {"ok": True, "sent_to": [a["name"] for a in agents], "timestamp": ts}


# ── Main ────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import os
    import uvicorn

    port = int(os.environ.get("PORT", 9850))
    API_KEY = os.environ.get("API_KEY", None)

    if len(sys.argv) > 1:
        port = int(sys.argv[1])

    uvicorn.run(app, host="0.0.0.0", port=port)
