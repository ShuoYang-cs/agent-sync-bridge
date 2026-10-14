"""SQLite message queue with an explicit, audited state machine.

PENDING      saved in the local queue, target agent has not seen it
DELIVERED    content was put into the target agent's context (hook injection
             or the target ran `asb show`); recorded per session
ACKNOWLEDGED the target agent ran `asb ack` with its own restatement
FAILED       rejected by the target, expired, or delivery error (retryable)
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import paths

PENDING, DELIVERED, ACKNOWLEDGED, FAILED = "PENDING", "DELIVERED", "ACKNOWLEDGED", "FAILED"
STATUSES = (PENDING, DELIVERED, ACKNOWLEDGED, FAILED)
OPEN = (PENDING, DELIVERED)

DEFAULT_TTL_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    project_root  TEXT,
    source_agent  TEXT NOT NULL,
    target_agent  TEXT NOT NULL,
    mode          TEXT NOT NULL,            -- sync | handoff
    title         TEXT NOT NULL,
    user_command  TEXT NOT NULL,
    body_md       TEXT NOT NULL,
    context_json  TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    status        TEXT NOT NULL,
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    source_session TEXT,
    git_branch    TEXT,
    git_head      TEXT,
    worktree      TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    delivered_at  REAL,
    acked_at      REAL,
    ack_summary   TEXT,
    ack_session   TEXT,
    expires_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_inbox ON messages(target_agent, project_id, status);
CREATE INDEX IF NOT EXISTS idx_hash ON messages(project_id, content_hash);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  TEXT NOT NULL,
    ts          REAL NOT NULL,
    event       TEXT NOT NULL,
    agent       TEXT,
    session_id  TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events ON events(message_id);

CREATE TABLE IF NOT EXISTS deliveries (
    message_id  TEXT NOT NULL,
    session_key TEXT NOT NULL,
    channel     TEXT NOT NULL,              -- hook | cli
    ts          REAL NOT NULL,
    PRIMARY KEY (message_id, session_key)
);
"""


class BridgeError(Exception):
    pass


def iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(now: float) -> str:
    return datetime.fromtimestamp(now).strftime("%m%d") + "-" + secrets.token_hex(3)


def content_hash(project_id: str, source: str, target: str, mode: str, context: Dict[str, Any]) -> str:
    canon = {k: v for k, v in context.items() if k not in ("source_session",)}
    raw = json.dumps([project_id, source, target, mode, canon], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = str(path or paths.db_path())
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- helpers -----------------------------------------------------------
    def _event(self, mid: str, event: str, agent: Optional[str] = None,
               session: Optional[str] = None, detail: Optional[str] = None) -> None:
        self.db.execute(
            "INSERT INTO events(message_id, ts, event, agent, session_id, detail) VALUES (?,?,?,?,?,?)",
            (mid, time.time(), event, agent, session, detail),
        )

    def get(self, mid: str) -> Dict[str, Any]:
        row = self.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not row:
            raise BridgeError(f"消息不存在: {mid}")
        return dict(row)

    def events(self, mid: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM events WHERE message_id=? ORDER BY id", (mid,))]

    # -- write side --------------------------------------------------------
    def create(self, *, project_id: str, project_root: str, source: str, target: str,
               mode: str, title: str, user_command: str, body_md: str,
               context: Dict[str, Any], source_session: Optional[str] = None,
               git_branch: Optional[str] = None, git_head: Optional[str] = None,
               worktree: Optional[str] = None, allow_duplicate: bool = False,
               ttl_days: float = DEFAULT_TTL_DAYS) -> Dict[str, Any]:
        h = content_hash(project_id, source, target, mode, context)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if not allow_duplicate:
                dup = self.db.execute(
                    "SELECT id, status FROM messages WHERE project_id=? AND content_hash=? "
                    "AND status != ? ORDER BY created_at DESC LIMIT 1",
                    (project_id, h, FAILED),
                ).fetchone()
                if dup:
                    self.db.execute("COMMIT")
                    msg = self.get(dup["id"])
                    msg["duplicate"] = True
                    return msg
            now = time.time()
            mid = new_id(now)
            self.db.execute(
                "INSERT INTO messages(id, project_id, project_root, source_agent, target_agent, mode, title, "
                "user_command, body_md, context_json, content_hash, status, source_session, git_branch, git_head, "
                "worktree, created_at, updated_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, project_id, project_root, source, target, mode, title, user_command, body_md,
                 json.dumps(context, ensure_ascii=False), h, PENDING, source_session, git_branch, git_head,
                 worktree, now, now, now + ttl_days * 86400),
            )
            self._event(mid, "CREATED", source, source_session, user_command)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        msg = self.get(mid)
        msg["duplicate"] = False
        return msg

    def deliver(self, mid: str, agent: str, session: Optional[str], channel: str) -> bool:
        """Record that content entered `agent`'s context. Returns False if this
        session already received it (so hooks never inject the same message twice)."""
        msg = self.get(mid)
        if msg["target_agent"] != agent:
            raise BridgeError(f"消息 {mid} 的接收方是 {msg['target_agent']}，不是 {agent}")
        if msg["status"] not in OPEN:
            raise BridgeError(f"消息 {mid} 当前状态为 {msg['status']}，不能投递")
        key = session or f"{channel}:{agent}"
        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO deliveries(message_id, session_key, channel, ts) VALUES (?,?,?,?)",
                (mid, key, channel, now),
            )
            first_for_session = cur.rowcount == 1
            if first_for_session:
                self.db.execute(
                    "UPDATE messages SET status=?, delivered_at=COALESCE(delivered_at, ?), updated_at=?, "
                    "attempts=attempts+1 WHERE id=? AND status IN (?,?)",
                    (DELIVERED, now, now, mid, PENDING, DELIVERED),
                )
                self._event(mid, "DELIVERED", agent, session, channel)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return first_for_session

    def was_delivered_to(self, mid: str, session: Optional[str]) -> bool:
        if not session:
            return False
        return self.db.execute(
            "SELECT 1 FROM deliveries WHERE message_id=? AND session_key=?", (mid, session)
        ).fetchone() is not None

    def ack(self, mid: str, agent: str, summary: str, session: Optional[str] = None) -> Dict[str, Any]:
        summary = (summary or "").strip()
        if len(summary) < 8:
            raise BridgeError("确认必须附带对消息内容的复述摘要（--summary，至少 8 个字符）")
        msg = self.get(mid)
        if msg["target_agent"] != agent:
            raise BridgeError(f"只有接收方 {msg['target_agent']} 可以确认消息 {mid}")
        if msg["status"] == ACKNOWLEDGED:
            msg["already"] = True
            return msg
        if msg["status"] == PENDING:
            raise BridgeError(f"消息 {mid} 尚未投递给 {agent}；请先运行 `asb show {mid} --agent {agent}` 阅读全文再确认")
        if msg["status"] != DELIVERED:
            raise BridgeError(f"消息 {mid} 当前状态为 {msg['status']}，不能确认")
        now = time.time()
        self.db.execute(
            "UPDATE messages SET status=?, acked_at=?, ack_summary=?, ack_session=?, updated_at=? "
            "WHERE id=? AND status=?",
            (ACKNOWLEDGED, now, summary, session, now, mid, DELIVERED),
        )
        self._event(mid, "ACKNOWLEDGED", agent, session, summary)
        msg = self.get(mid)
        msg["already"] = False
        return msg

    def fail(self, mid: str, reason: str, agent: Optional[str] = None) -> Dict[str, Any]:
        msg = self.get(mid)
        if msg["status"] not in OPEN:
            raise BridgeError(f"消息 {mid} 当前状态为 {msg['status']}，不能标记失败")
        now = time.time()
        self.db.execute("UPDATE messages SET status=?, error=?, updated_at=? WHERE id=?",
                        (FAILED, reason, now, mid))
        self._event(mid, "FAILED", agent, None, reason)
        return self.get(mid)

    def retry(self, mid: str, ttl_days: float = DEFAULT_TTL_DAYS) -> Dict[str, Any]:
        msg = self.get(mid)
        if msg["status"] != FAILED:
            raise BridgeError(f"只有 FAILED 消息可以重试，{mid} 当前为 {msg['status']}")
        now = time.time()
        self.db.execute(
            "UPDATE messages SET status=?, error=NULL, updated_at=?, expires_at=? WHERE id=?",
            (PENDING, now, now + ttl_days * 86400, mid))
        self.db.execute("DELETE FROM deliveries WHERE message_id=?", (mid,))
        self._event(mid, "RETRY", None, None, msg.get("error"))
        return self.get(mid)

    def expire(self) -> int:
        now = time.time()
        rows = self.db.execute(
            "SELECT id FROM messages WHERE status IN (?,?) AND expires_at IS NOT NULL AND expires_at < ?",
            (PENDING, DELIVERED, now)).fetchall()
        for r in rows:
            self.db.execute("UPDATE messages SET status=?, error=?, updated_at=? WHERE id=?",
                            (FAILED, "expired: 超过保留期仍未确认", now, r["id"]))
            self._event(r["id"], "FAILED", None, None, "expired")
        return len(rows)

    def purge(self, older_than_days: float) -> int:
        cutoff = time.time() - older_than_days * 86400
        ids = [r["id"] for r in self.db.execute(
            "SELECT id FROM messages WHERE status IN (?,?) AND updated_at < ?",
            (ACKNOWLEDGED, FAILED, cutoff))]
        for mid in ids:
            for t in ("events", "deliveries"):
                self.db.execute(f"DELETE FROM {t} WHERE message_id=?", (mid,))
            self.db.execute("DELETE FROM messages WHERE id=?", (mid,))
        return len(ids)

    # -- read side ---------------------------------------------------------
    def inbox(self, agent: Optional[str], project_id: Optional[str]) -> List[Dict[str, Any]]:
        self.expire()
        q = "SELECT * FROM messages WHERE status IN (?,?)"
        args: List[Any] = [PENDING, DELIVERED]
        if agent:
            q += " AND target_agent=?"
            args.append(agent)
        if project_id:
            q += " AND project_id=?"
            args.append(project_id)
        q += " ORDER BY created_at"
        return [dict(r) for r in self.db.execute(q, args)]

    def history(self, project_id: Optional[str], agent: Optional[str] = None,
                limit: int = 20) -> List[Dict[str, Any]]:
        self.expire()
        q = "SELECT * FROM messages WHERE 1=1"
        args: List[Any] = []
        if project_id:
            q += " AND project_id=?"
            args.append(project_id)
        if agent:
            q += " AND (source_agent=? OR target_agent=?)"
            args += [agent, agent]
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(q, args)]
