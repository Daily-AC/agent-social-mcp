"""
Agent Social Hub — 轻量 HTTP 消息中转服务
"""

import time
import sqlite3
import threading
from contextlib import asynccontextmanager
from typing import Optional

import httpx
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
                webhook_url TEXT DEFAULT '',
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
        # ── Group tables ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                creator TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS group_members (
                group_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                role TEXT DEFAULT 'member',
                joined_at REAL NOT NULL,
                PRIMARY KEY (group_id, agent_name)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS group_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gmsg_group ON group_messages(group_id, timestamp)")


def fire_webhook(receiver: str, sender: str, content: str, group_id: str = "", group_name: str = ""):
    """异步通知收件人有新消息（非阻塞）"""
    def _do():
        try:
            with get_db() as conn:
                row = conn.execute("SELECT webhook_url FROM agents WHERE name = ?", (receiver,)).fetchone()
            if not row or not row["webhook_url"]:
                return
            payload = {"sender": sender, "receiver": receiver, "content": content, "timestamp": time.time()}
            if group_id:
                payload["group_id"] = group_id
                payload["group_name"] = group_name
            httpx.post(
                row["webhook_url"],
                json=payload,
                timeout=5,
                proxy=None,
            )
        except Exception:
            pass  # webhook 失败不影响消息存储
    threading.Thread(target=_do, daemon=True).start()


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
    webhook_url: str = ""  # 收到消息时回调此 URL


class Message(BaseModel):
    sender: str
    receiver: str
    content: str
    timestamp: Optional[float] = None


class Broadcast(BaseModel):
    sender: str
    content: str
    timestamp: Optional[float] = None


class GroupCreate(BaseModel):
    name: str
    description: str = ""
    creator: str


class GroupInvite(BaseModel):
    agent_name: str


class GroupMessage(BaseModel):
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
            """INSERT INTO agents (name, description, capabilities, webhook_url, registered_at, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 description=excluded.description,
                 capabilities=excluded.capabilities,
                 webhook_url=excluded.webhook_url,
                 last_seen=excluded.last_seen""",
            (agent.name, agent.description, json.dumps(agent.capabilities), agent.webhook_url, now, now),
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
            "webhook_url": bool(r["webhook_url"]),  # 只暴露是否有 webhook，不暴露 URL
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
    # 异步通知收件人
    fire_webhook(msg.receiver, msg.sender, msg.content)
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
    # 异步通知每个收件人
    for a in agents:
        fire_webhook(a["name"], msg.sender, msg.content)
    return {"ok": True, "sent_to": [a["name"] for a in agents], "timestamp": ts}


# ── Group Routes ────────────────────────────────────────

@app.post("/api/groups")
def create_group(group: GroupCreate, x_api_key: Optional[str] = Header(None)):
    bound_name = check_auth(x_api_key)
    if bound_name:
        group.creator = bound_name
    import uuid
    group_id = uuid.uuid4().hex[:8]
    now = time.time()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO groups (id, name, description, creator, created_at) VALUES (?, ?, ?, ?, ?)",
            (group_id, group.name, group.description, group.creator, now),
        )
        conn.execute(
            "INSERT INTO group_members (group_id, agent_name, role, joined_at) VALUES (?, ?, 'owner', ?)",
            (group_id, group.creator, now),
        )
    return {"ok": True, "group_id": group_id, "name": group.name}


@app.get("/api/groups")
def list_groups(member: Optional[str] = Query(None), x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    with get_db() as conn:
        if member:
            rows = conn.execute("""
                SELECT g.*, gm.role FROM groups g
                JOIN group_members gm ON g.id = gm.group_id
                WHERE gm.agent_name = ?
                ORDER BY g.created_at DESC
            """, (member,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM groups ORDER BY created_at DESC").fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "description": r["description"],
            "creator": r["creator"],
            "created_at": r["created_at"],
            **({"role": r["role"]} if member else {}),
        }
        for r in rows
    ]


@app.get("/api/groups/{group_id}")
def get_group(group_id: str, x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    with get_db() as conn:
        g = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
        if not g:
            raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")
        members = conn.execute(
            "SELECT agent_name, role, joined_at FROM group_members WHERE group_id = ?", (group_id,)
        ).fetchall()
    return {
        "id": g["id"],
        "name": g["name"],
        "description": g["description"],
        "creator": g["creator"],
        "created_at": g["created_at"],
        "members": [{"name": m["agent_name"], "role": m["role"], "joined_at": m["joined_at"]} for m in members],
    }


@app.post("/api/groups/{group_id}/members")
def add_group_member(group_id: str, invite: GroupInvite, x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    now = time.time()
    with get_db() as conn:
        g = conn.execute("SELECT id FROM groups WHERE id = ?", (group_id,)).fetchone()
        if not g:
            raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")
        try:
            conn.execute(
                "INSERT INTO group_members (group_id, agent_name, role, joined_at) VALUES (?, ?, 'member', ?)",
                (group_id, invite.agent_name, now),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"'{invite.agent_name}' already in group")
    return {"ok": True, "group_id": group_id, "agent": invite.agent_name}


@app.delete("/api/groups/{group_id}/members/{agent_name}")
def remove_group_member(group_id: str, agent_name: str, x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    with get_db() as conn:
        conn.execute(
            "DELETE FROM group_members WHERE group_id = ? AND agent_name = ?",
            (group_id, agent_name),
        )
    return {"ok": True}


@app.post("/api/groups/{group_id}/messages")
def send_group_message(group_id: str, msg: GroupMessage, x_api_key: Optional[str] = Header(None)):
    bound_name = check_auth(x_api_key)
    if bound_name:
        msg.sender = bound_name
    if len(msg.content) > MAX_MESSAGE_LENGTH:
        raise HTTPException(status_code=413, detail=f"Message too long (max {MAX_MESSAGE_LENGTH} chars)")
    ts = msg.timestamp or time.time()
    with get_db() as conn:
        g = conn.execute("SELECT id, name FROM groups WHERE id = ?", (group_id,)).fetchone()
        if not g:
            raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")
        # 检查发送者是否是群成员
        member = conn.execute(
            "SELECT 1 FROM group_members WHERE group_id = ? AND agent_name = ?",
            (group_id, msg.sender),
        ).fetchone()
        if not member:
            raise HTTPException(status_code=403, detail=f"'{msg.sender}' is not a member of this group")
        conn.execute(
            "INSERT INTO group_messages (group_id, sender, content, timestamp) VALUES (?, ?, ?, ?)",
            (group_id, msg.sender, msg.content, ts),
        )
        conn.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (ts, msg.sender))
        # 获取所有成员（除发送者）
        recipients = conn.execute(
            "SELECT agent_name FROM group_members WHERE group_id = ? AND agent_name != ?",
            (group_id, msg.sender),
        ).fetchall()
    # 异步通知每个群成员
    group_name = g["name"]
    for r in recipients:
        fire_webhook(r["agent_name"], msg.sender, msg.content, group_id=group_id, group_name=group_name)
    return {"ok": True, "group_id": group_id, "sent_to": [r["agent_name"] for r in recipients], "timestamp": ts}


@app.get("/api/groups/{group_id}/messages")
def get_group_messages(
    group_id: str,
    since: Optional[float] = Query(None),
    limit: int = Query(50, le=200),
    x_api_key: Optional[str] = Header(None),
):
    check_auth(x_api_key)
    with get_db() as conn:
        if since:
            rows = conn.execute(
                "SELECT * FROM group_messages WHERE group_id = ? AND timestamp > ? ORDER BY timestamp DESC LIMIT ?",
                (group_id, since, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM group_messages WHERE group_id = ? ORDER BY timestamp DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()
    return [
        {
            "id": r["id"],
            "group_id": r["group_id"],
            "sender": r["sender"],
            "content": r["content"],
            "timestamp": r["timestamp"],
        }
        for r in rows
    ]


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
