"""SQLite message queue with an explicit, audited state machine.

PENDING      saved in the local queue, target agent has not seen it
DELIVERED    content was put into the target agent's context (hook injection
             or the target ran `asb show`); recorded per session
ACKNOWLEDGED the target agent ran `asb ack` with its own restatement
FAILED       rejected by the target, expired, or delivery error (retryable)

Cross-device messages (source_device != target_device) also carry a
`transport` state on top of the status:
OUTBOX    sender: written locally, not yet pushed to the mailbox
SENT      sender: pushed to the mailbox, the other computer has not pulled it
RECEIVED  sender: the other computer imported it into its own queue
INBOUND   receiver: imported from the mailbox
Every status change of a cross-device message is also written to `outbox`
so the other computer receives it as a receipt (see remote.py).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import config, paths

PENDING, DELIVERED, ACKNOWLEDGED, FAILED = "PENDING", "DELIVERED", "ACKNOWLEDGED", "FAILED"
STATUSES = (PENDING, DELIVERED, ACKNOWLEDGED, FAILED)
OPEN = (PENDING, DELIVERED)
OUTBOX, SENT, RECEIVED, INBOUND = "OUTBOX", "SENT", "RECEIVED", "INBOUND"

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

-- receipts this device still has to push to the mailbox
CREATE TABLE IF NOT EXISTS outbox (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id   TEXT NOT NULL UNIQUE,
    message_id  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    pushed_at   REAL
);

-- mailbox records already processed (makes import idempotent)
CREATE TABLE IF NOT EXISTS imported (
    record_id   TEXT PRIMARY KEY,
    device      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    message_id  TEXT,
    result      TEXT,
    ts          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id          TEXT PRIMARY KEY,
    label       TEXT,
    platform    TEXT,
    last_seen   REAL
);

-- one-line hints already shown in a session (hook)
CREATE TABLE IF NOT EXISTS notices (
    message_id  TEXT NOT NULL,
    session_key TEXT NOT NULL,
    ts          REAL NOT NULL,
    PRIMARY KEY (message_id, session_key)
);
"""

MIGRATIONS = {
    "messages": (("source_device", "TEXT"), ("target_device", "TEXT"), ("transport", "TEXT"),
                 ("project_name", "TEXT"), ("git_remote", "TEXT")),
    "events": (("device", "TEXT"),),
}


class BridgeError(Exception):
    pass


def iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(now: float) -> str:
    return datetime.fromtimestamp(now).strftime("%m%d") + "-" + secrets.token_hex(4)


def content_hash(project_id: str, source: str, target: str, mode: str, context: Dict[str, Any],
                 target_device: Optional[str] = None) -> str:
    canon = {k: v for k, v in context.items() if k not in ("source_session",)}
    key = [project_id, source, target, mode, canon] + ([target_device] if target_device else [])
    raw = json.dumps(key, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_cross_device(m: Dict[str, Any]) -> bool:
    return bool(m.get("source_device") and m.get("target_device") and m["source_device"] != m["target_device"])


class Store:
    def __init__(self, path: Optional[str] = None, device: Optional[str] = None):
        self.path = str(path or paths.db_path())
        self.device = device if device is not None else config.local_device()
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols:
                if name not in have:
                    try:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                    except sqlite3.OperationalError as e:  # another process migrated first
                        if "duplicate column" not in str(e):
                            raise

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    # -- helpers -----------------------------------------------------------
    def _event(self, mid: str, event: str, agent: Optional[str] = None,
               session: Optional[str] = None, detail: Optional[str] = None,
               device: Optional[str] = None, ts: Optional[float] = None) -> None:
        self.db.execute(
            "INSERT INTO events(message_id, ts, event, agent, session_id, detail, device) VALUES (?,?,?,?,?,?,?)",
            (mid, ts or time.time(), event, agent, session, detail, device),
        )

    def _local_target(self) -> str:
        """SQL condition: message is addressed to an agent on this computer."""
        return "(target_device IS NULL OR target_device = ?)" if self.device else "target_device IS NULL"

    def _local_args(self) -> List[Any]:
        return [self.device] if self.device else []

    def _check_here(self, msg: Dict[str, Any]) -> None:
        if msg.get("target_device") and msg["target_device"] != self.device:
            raise BridgeError(f"消息 {msg['id']} 发往另一台电脑（{msg['target_device']}），不能在本机投递或确认")

    def _publish(self, msg: Dict[str, Any], kind: str, agent: Optional[str] = None,
                 detail: Optional[str] = None, summary: Optional[str] = None,
                 expires_at: Optional[float] = None) -> None:
        """Queue a receipt for the other computer. Call inside a transaction."""
        if not self.device or not is_cross_device(msg) or msg.get("transport") == OUTBOX:
            return  # same-machine message, or never uploaded: nothing to tell
        now = time.time()
        stem = f"{msg['id']}.{kind}.{secrets.token_hex(4)}"
        payload = {"kind": "event", "format": 1, "id": stem, "message_id": msg["id"], "type": kind,
                   "device": self.device, "agent": agent, "ts": now}
        if detail:
            payload["detail"] = detail
        if summary:
            payload["summary"] = summary
        if expires_at:
            payload["expires_at"] = expires_at
        rid = f"{self.device}/e/{stem}"
        self.db.execute("INSERT INTO outbox(record_id, message_id, payload, created_at) VALUES (?,?,?,?)",
                        (rid, msg["id"], json.dumps(payload, ensure_ascii=False), now))
        self.mark_imported(rid, self.device, "event", msg["id"], "local")

    def get(self, mid: str) -> Dict[str, Any]:
        row = self.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not row:
            raise BridgeError(f"消息不存在: {mid}")
        return dict(row)

    def find(self, mid: str) -> Optional[Dict[str, Any]]:
        row = self.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        return dict(row) if row else None

    def events(self, mid: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM events WHERE message_id=? ORDER BY ts, id", (mid,))]

    # -- write side --------------------------------------------------------
    def create(self, *, project_id: str, project_root: str, source: str, target: str,
               mode: str, title: str, user_command: str, body_md: str,
               context: Dict[str, Any], source_session: Optional[str] = None,
               git_branch: Optional[str] = None, git_head: Optional[str] = None,
               worktree: Optional[str] = None, allow_duplicate: bool = False,
               ttl_days: float = DEFAULT_TTL_DAYS, target_device: Optional[str] = None,
               project_name: Optional[str] = None, git_remote: Optional[str] = None) -> Dict[str, Any]:
        remote = bool(target_device and target_device != self.device)
        if remote and not self.device:
            raise BridgeError("本机还没有配置跨电脑同步（asb remote setup）")
        h = content_hash(project_id, source, target, mode, context, target_device if remote else None)
        with self.tx():
            dup = None if allow_duplicate else self.db.execute(
                "SELECT id FROM messages WHERE project_id=? AND content_hash=? "
                "AND status != ? ORDER BY created_at DESC LIMIT 1",
                (project_id, h, FAILED),
            ).fetchone()
            if not dup:
                now = time.time()
                mid = new_id(now)
                while self.find(mid):
                    mid = new_id(now)
                self.db.execute(
                    "INSERT INTO messages(id, project_id, project_root, source_agent, target_agent, mode, title, "
                    "user_command, body_md, context_json, content_hash, status, source_session, git_branch, git_head, "
                    "worktree, created_at, updated_at, expires_at, source_device, target_device, transport, "
                    "project_name, git_remote) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, project_id, project_root, source, target, mode, title, user_command, body_md,
                     json.dumps(context, ensure_ascii=False), h, PENDING, source_session, git_branch, git_head,
                     worktree, now, now, now + ttl_days * 86400,
                     self.device if remote else None, target_device if remote else None,
                     OUTBOX if remote else None, project_name, git_remote),
                )
                self._event(mid, "CREATED", source, source_session, user_command,
                            device=self.device if remote else None)
        msg = self.get(dup["id"] if dup else mid)
        msg["duplicate"] = bool(dup)
        return msg

    def deliver(self, mid: str, agent: str, session: Optional[str], channel: str) -> bool:
        """Record that content entered `agent`'s context. Returns False if this
        session already received it (so hooks never inject the same message twice)."""
        msg = self.get(mid)
        if msg["target_agent"] != agent:
            raise BridgeError(f"消息 {mid} 的接收方是 {msg['target_agent']}，不是 {agent}")
        self._check_here(msg)
        if msg["status"] not in OPEN:
            raise BridgeError(f"消息 {mid} 当前状态为 {msg['status']}，不能投递")
        key = session or f"{channel}:{agent}"
        now = time.time()
        with self.tx():
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
                self._event(mid, "DELIVERED", agent, session, channel,
                            device=self.device if is_cross_device(msg) else None)
                if msg["status"] == PENDING:
                    self._publish(msg, "DELIVERED", agent, channel)
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
        self._check_here(msg)
        if msg["status"] == ACKNOWLEDGED:
            msg["already"] = True
            return msg
        if msg["status"] == PENDING:
            raise BridgeError(f"消息 {mid} 尚未投递给 {agent}；请先运行 `asb show {mid} --agent {agent}` 阅读全文再确认")
        if msg["status"] != DELIVERED:
            raise BridgeError(f"消息 {mid} 当前状态为 {msg['status']}，不能确认")
        now = time.time()
        with self.tx():
            self.db.execute(
                "UPDATE messages SET status=?, acked_at=?, ack_summary=?, ack_session=?, updated_at=? "
                "WHERE id=? AND status=?",
                (ACKNOWLEDGED, now, summary, session, now, mid, DELIVERED),
            )
            self._event(mid, "ACKNOWLEDGED", agent, session, summary, device=self.device if is_cross_device(msg) else None)
            self._publish(msg, "ACKNOWLEDGED", agent, summary=summary)
        msg = self.get(mid)
        msg["already"] = False
        return msg

    def fail(self, mid: str, reason: str, agent: Optional[str] = None) -> Dict[str, Any]:
        msg = self.get(mid)
        if msg["status"] not in OPEN:
            raise BridgeError(f"消息 {mid} 当前状态为 {msg['status']}，不能标记失败")
        now = time.time()
        with self.tx():
            self.db.execute("UPDATE messages SET status=?, error=?, updated_at=? WHERE id=?",
                            (FAILED, reason, now, mid))
            self._event(mid, "FAILED", agent, None, reason, device=self.device if is_cross_device(msg) else None)
            self._publish(msg, "FAILED", agent, reason)
        return self.get(mid)

    def retry(self, mid: str, ttl_days: float = DEFAULT_TTL_DAYS) -> Dict[str, Any]:
        msg = self.get(mid)
        if msg["status"] != FAILED:
            raise BridgeError(f"只有 FAILED 消息可以重试，{mid} 当前为 {msg['status']}")
        now = time.time()
        expires = now + ttl_days * 86400
        with self.tx():
            self.db.execute(
                "UPDATE messages SET status=?, error=NULL, updated_at=?, expires_at=? WHERE id=?",
                (PENDING, now, expires, mid))
            self.db.execute("DELETE FROM deliveries WHERE message_id=?", (mid,))
            self._event(mid, "RETRY", None, None, msg.get("error"), device=self.device if is_cross_device(msg) else None)
            self._publish(msg, "RETRY", detail=msg.get("error"), expires_at=expires)
        return self.get(mid)

    def expire(self) -> int:
        """Only messages addressed to this computer expire here; a message sent
        to another computer keeps showing that computer's real state."""
        now = time.time()
        rows = self.db.execute(
            "SELECT * FROM messages WHERE status IN (?,?) AND expires_at IS NOT NULL AND expires_at < ? AND "
            + self._local_target(), [PENDING, DELIVERED, now] + self._local_args()).fetchall()
        for r in rows:
            with self.tx():
                self.db.execute("UPDATE messages SET status=?, error=?, updated_at=? WHERE id=? AND status IN (?,?)",
                                (FAILED, "expired: 超过保留期仍未确认", now, r["id"], PENDING, DELIVERED))
                self._event(r["id"], "FAILED", None, None, "expired",
                            device=self.device if is_cross_device(dict(r)) else None)
                self._publish(dict(r), "FAILED", detail="expired: 超过保留期仍未确认")
        return len(rows)

    def purge(self, older_than_days: float) -> int:
        cutoff = time.time() - older_than_days * 86400
        ids = [r["id"] for r in self.db.execute(
            "SELECT id FROM messages WHERE status IN (?,?) AND updated_at < ?",
            (ACKNOWLEDGED, FAILED, cutoff))]
        for mid in ids:
            for t in ("events", "deliveries", "notices"):
                self.db.execute(f"DELETE FROM {t} WHERE message_id=?", (mid,))
            self.db.execute("DELETE FROM messages WHERE id=?", (mid,))
        return len(ids)

    def rekey_project(self, old: str, new: str) -> int:
        if old == new:
            return 0
        return self.db.execute("UPDATE messages SET project_id=? WHERE project_id=?", (new, old)).rowcount

    # -- cross-device ------------------------------------------------------
    def insert_remote(self, env: Dict[str, Any], body_md: str, transport: str) -> Dict[str, Any]:
        """Insert a message read from the mailbox (inbound, or our own after a reinstall)."""
        now = time.time()
        self.db.execute(
            "INSERT INTO messages(id, project_id, project_root, source_agent, target_agent, mode, title, "
            "user_command, body_md, context_json, content_hash, status, git_branch, git_head, created_at, "
            "updated_at, expires_at, source_device, target_device, transport, project_name, git_remote) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (env["id"], env["project_id"], None, env["source_agent"], env["target_agent"], env["mode"],
             env["title"], env["user_command"], body_md, json.dumps(env["context"], ensure_ascii=False),
             env.get("content_hash") or "", PENDING, env.get("git_branch"), env.get("git_head"),
             env["created_at"], now, env.get("expires_at"), env["source_device"], env["target_device"],
             transport, env.get("project_name"), env.get("git_remote")),
        )
        self._event(env["id"], "CREATED", env["source_agent"], None, env["user_command"],
                    device=env["source_device"], ts=env["created_at"])
        msg = self.get(env["id"])
        if transport == INBOUND:
            self._event(env["id"], "RECEIVED", None, None, "已从同步信箱导入", device=self.device)
            self._publish(msg, "RECEIVED")
        return msg

    def apply_remote_event(self, ev: Dict[str, Any], from_device: str) -> str:
        """Merge a receipt from the other computer. Rules are idempotent and
        order-tolerant: ACKNOWLEDGED wins over FAILED; RETRY only reopens FAILED."""
        m = self.find(ev["message_id"])
        if not m:
            return "orphan"
        if not is_cross_device(m) or from_device not in (m["source_device"], m["target_device"]):
            return "foreign"
        kind = ev["type"]
        if kind in ("RECEIVED", "DELIVERED", "ACKNOWLEDGED") and from_device != m["target_device"]:
            return "not-allowed"
        ts = float(ev.get("ts") or time.time())
        now = time.time()
        mid, st = m["id"], m["status"]
        applied = True
        outbound = m["source_device"] == self.device
        if kind == "RECEIVED":
            applied = False
        elif kind == "DELIVERED":
            if st == PENDING:
                self.db.execute("UPDATE messages SET status=?, delivered_at=COALESCE(delivered_at, ?), updated_at=? "
                                "WHERE id=?", (DELIVERED, ts, now, mid))
            else:
                applied = False
        elif kind == "ACKNOWLEDGED":
            if st != ACKNOWLEDGED:
                self.db.execute("UPDATE messages SET status=?, acked_at=?, ack_summary=?, ack_session=?, error=NULL, "
                                "updated_at=? WHERE id=?",
                                (ACKNOWLEDGED, ts, ev.get("summary"), f"{from_device}:{ev.get('agent') or ''}", now, mid))
            else:
                applied = False
        elif kind == "FAILED":
            if st in OPEN:
                self.db.execute("UPDATE messages SET status=?, error=?, updated_at=? WHERE id=?",
                                (FAILED, ev.get("detail") or f"{from_device} 标记失败", now, mid))
            else:
                applied = False
        elif kind == "RETRY":
            if st == FAILED:
                self.db.execute("UPDATE messages SET status=?, error=NULL, expires_at=?, updated_at=? WHERE id=?",
                                (PENDING, ev.get("expires_at") or now + DEFAULT_TTL_DAYS * 86400, now, mid))
                self.db.execute("DELETE FROM deliveries WHERE message_id=?", (mid,))
            else:
                applied = False
        else:
            return "unknown"
        if outbound and kind in ("RECEIVED", "DELIVERED", "ACKNOWLEDGED") and m["transport"] in (OUTBOX, SENT):
            self.db.execute("UPDATE messages SET transport=? WHERE id=?", (RECEIVED, mid))
        detail = ev.get("summary") or ev.get("detail") or ""
        if not applied and kind != "RECEIVED":
            detail = (detail + " " if detail else "") + f"(已忽略：本机状态为 {st})"
        self._event(mid, kind, ev.get("agent"), None, detail, device=from_device, ts=ts)
        return "applied" if applied else "noop"

    def unsent_outbound(self) -> List[Dict[str, Any]]:
        if not self.device:
            return []
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM messages WHERE source_device=? AND transport=? AND status != ? ORDER BY created_at",
            (self.device, OUTBOX, FAILED))]

    def mark_sent(self, ids: List[str]) -> None:
        for mid in ids:
            cur = self.db.execute("UPDATE messages SET transport=? WHERE id=? AND transport=?", (SENT, mid, OUTBOX))
            if cur.rowcount:
                self._event(mid, "UPLOADED", None, None, "已推送到同步信箱", device=self.device)

    def unpushed(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM outbox WHERE pushed_at IS NULL ORDER BY seq")]

    def mark_pushed(self, record_ids: List[str]) -> None:
        now = time.time()
        for rid in record_ids:
            self.db.execute("UPDATE outbox SET pushed_at=? WHERE record_id=?", (now, rid))

    def pending_push_count(self) -> int:
        n = self.db.execute("SELECT COUNT(*) FROM outbox WHERE pushed_at IS NULL").fetchone()[0]
        return n + len(self.unsent_outbound())

    def is_imported(self, rid: str) -> bool:
        return self.db.execute("SELECT 1 FROM imported WHERE record_id=?", (rid,)).fetchone() is not None

    def mark_imported(self, rid: str, device: str, kind: str, mid: Optional[str], result: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO imported(record_id, device, kind, message_id, result, ts) "
                        "VALUES (?,?,?,?,?,?)", (rid, device, kind, mid, result, time.time()))

    def upsert_device(self, did: str, label: Optional[str], platform: Optional[str]) -> None:
        self.db.execute("INSERT INTO devices(id, label, platform, last_seen) VALUES (?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET label=excluded.label, platform=excluded.platform, "
                        "last_seen=excluded.last_seen", (did, label, platform, time.time()))

    def devices(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM devices ORDER BY id")]

    def device_label(self, did: Optional[str]) -> Optional[str]:
        if not did:
            return None
        if did == self.device:
            cfg = config.load()
            return (cfg or {}).get("label") or did
        row = self.db.execute("SELECT label FROM devices WHERE id=?", (did,)).fetchone()
        return (row["label"] if row and row["label"] else did)

    def notice_once(self, mid: str, session: Optional[str]) -> bool:
        if not session:
            return True
        cur = self.db.execute("INSERT OR IGNORE INTO notices(message_id, session_key, ts) VALUES (?,?,?)",
                              (mid, session, time.time()))
        return cur.rowcount == 1

    # -- read side ---------------------------------------------------------
    def inbox(self, agent: Optional[str], project_id: Optional[str],
              git_remote: Optional[str] = None) -> List[Dict[str, Any]]:
        self.expire()
        q = "SELECT * FROM messages WHERE status IN (?,?) AND " + self._local_target()
        args: List[Any] = [PENDING, DELIVERED] + self._local_args()
        if agent:
            q += " AND target_agent=?"
            args.append(agent)
        if project_id:
            if git_remote:
                q += " AND (project_id=? OR git_remote=?)"
                args += [project_id, git_remote]
            else:
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
