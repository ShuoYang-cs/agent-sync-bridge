"""Cross-device sync through a Git "mailbox" repository.

Any Git remote works: a private GitHub repository (works across networks) or
a bare repository on another computer reached over SSH (LAN, no third party).

  asb-mailbox.json                      format marker
  devices/<id>/device.json              signed device card (label, platform)
  devices/<id>/messages/<msg-id>.json   messages <id> sent to another computer
  devices/<id>/events/<stem>.json       receipts <id> wrote: RECEIVED, DELIVERED,
                                        ACKNOWLEDGED, FAILED, RETRY

Each device writes only under its own directory and never changes a file after
creating it, so `pull --rebase` cannot conflict and re-reading is idempotent.
Every record is signed with HMAC-SHA256 under a key derived from the pairing
code. The code is entered once on each computer and never written to the
mailbox; records with a bad signature are refused. Records are not encrypted:
whoever can read the repository can read the summaries.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import AGENTS, config, paths, render
from .store import INBOUND, SENT, BridgeError, Store, iso

FORMAT = 1
DEVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MSG_ID_RE = re.compile(r"^\d{4}-[0-9a-f]{6,16}$")
EVENT_TYPES = ("RECEIVED", "DELIVERED", "ACKNOWLEDGED", "FAILED", "RETRY")
STEM_RE = re.compile(r"^\d{4}-[0-9a-f]{6,16}\.(?:" + "|".join(EVENT_TYPES) + r")\.[0-9a-f]{8}$")
MAX_RECORD_BYTES = 256 * 1024
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U
HOOK_MIN_INTERVAL = 5.0   # seconds between hook-triggered syncs
DEFAULT_HOOK_WAIT = 2.5   # how long a prompt waits for the pull
DEFAULT_LABELS = {"macos": "Mac", "windows": "Windows", "linux": "Linux"}
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


class RemoteError(BridgeError):
    pass


# -- pairing code & signatures ------------------------------------------------

def new_pair_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(20))  # 100 bits
    return "-".join(raw[i:i + 5] for i in range(0, 20, 5))


def normalize_code(code: str) -> str:
    s = re.sub(r"[\s\-]", "", (code or "").upper()).replace("O", "0").replace("I", "1").replace("L", "1")
    if len(s) != 20 or any(c not in ALPHABET for c in s):
        raise RemoteError("配对码格式不对：应为 20 位字母数字，例如 7KQ2M-9XH4T-AB3CD-EF5GH")
    return "-".join(s[i:i + 5] for i in range(0, 20, 5))


def derive_key(code: str) -> bytes:
    return hashlib.sha256(b"asb-mailbox-v1:" + normalize_code(code).replace("-", "").encode()).digest()


def _canonical(obj: Dict[str, Any]) -> bytes:
    body = {k: v for k, v in obj.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sign(obj: Dict[str, Any], key: bytes) -> Dict[str, Any]:
    return {**obj, "sig": hmac.new(key, _canonical(obj), hashlib.sha256).hexdigest()}


def verify(obj: Any, key: bytes) -> bool:
    if not isinstance(obj, dict) or not isinstance(obj.get("sig"), str):
        return False
    return hmac.compare_digest(obj["sig"], hmac.new(key, _canonical(obj), hashlib.sha256).hexdigest())


# -- sync state & lock ----------------------------------------------------------

def load_state() -> Dict[str, Any]:
    try:
        return json.loads(paths.sync_state().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_state(**kw: Any) -> Dict[str, Any]:
    st = load_state()
    st.update(kw)
    f = paths.sync_state()
    tmp = f.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, f)
    return st


class SyncLock:
    """Exclusive lock so hooks of both agents and the CLI never sync at once."""

    def __init__(self, timeout: float):
        self.timeout = timeout
        self.f = None

    def _try(self) -> bool:
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def __enter__(self) -> "SyncLock":
        self.f = open(paths.sync_lock(), "a+b")
        deadline = time.monotonic() + self.timeout
        while not self._try():
            if time.monotonic() >= deadline:
                self.f.close()
                raise RemoteError("另一个同步正在进行，稍后再试")
            time.sleep(0.1)
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        finally:
            self.f.close()


def lock_busy() -> bool:
    try:
        with SyncLock(timeout=0):
            return False
    except RemoteError:
        return True


# -- git --------------------------------------------------------------------------

def _git(cwd: Path, *args: str, timeout: float = 30, check: bool = True,
         interactive: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(k, None)
    if not interactive:
        env["GIT_TERMINAL_PROMPT"] = "0"   # never hang a hook on a password prompt
        env["GCM_INTERACTIVE"] = "never"
    kw: Dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = CREATE_NO_WINDOW
    cmd = ["git", "-C", str(cwd), "-c", "core.autocrlf=false", "-c", "core.quotepath=false", *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=max(timeout, 3), env=env,
                           stdin=None if interactive else subprocess.DEVNULL, **kw)
    except FileNotFoundError:
        raise RemoteError("找不到 git，请先安装 Git（Windows：Git for Windows）")
    except subprocess.TimeoutExpired:
        raise RemoteError(f"git {args[0]} 超时（{timeout:.0f} 秒），可能没有联网")
    if check and r.returncode != 0:
        raise RemoteError(f"git {args[0]} 失败：{(r.stderr or r.stdout).strip()[-500:]}")
    return r


class Mailbox:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.dir = paths.mailbox_dir()
        self.device = cfg["device"]
        self.branch = cfg.get("branch") or "main"
        self.key = derive_key(cfg["pair_code"])
        self.ident = ("-c", f"user.name=asb-{self.device}",
                      "-c", f"user.email=asb-{self.device}@agent-sync-bridge.invalid",
                      "-c", "commit.gpgsign=false")

    def git(self, *a: str, **kw: Any) -> subprocess.CompletedProcess:
        return _git(self.dir, *a, **kw)

    def own(self) -> Path:
        return self.dir / "devices" / self.device

    def has_commits(self) -> bool:
        return self.git("rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0

    def commit(self, message: str) -> bool:
        self.git("add", "-A")
        if self.git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        self.git(*self.ident, "commit", "-q", "-m", message)
        return True

    def pull(self, timeout: float) -> None:
        r = self.git("-c", "rebase.autoStash=true", *self.ident, "pull", "--rebase", "-q", "origin", self.branch,
                     timeout=timeout, check=False)
        if r.returncode == 0:
            return
        err = (r.stderr or "") + (r.stdout or "")
        if "couldn't find remote ref" in err:
            return  # nobody has pushed yet
        git_dir = self.dir / ".git"
        if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
            self.git("rebase", "--abort", check=False)
        raise RemoteError("拉取失败：" + err.strip()[-500:])

    def ahead(self) -> bool:
        if not self.has_commits():
            return False
        r = self.git("rev-list", "--count", f"origin/{self.branch}..HEAD", check=False)
        if r.returncode != 0:
            return True  # the remote branch does not exist yet
        return int((r.stdout or "0").strip() or 0) > 0

    def push(self, timeout: float) -> bool:
        """False when the remote moved on (someone pushed first)."""
        r = self.git("push", "-q", "origin", f"HEAD:refs/heads/{self.branch}", timeout=timeout, check=False)
        if r.returncode == 0:
            return True  # push also moves origin/<branch>, so ahead() is accurate
        err = (r.stderr or "") + (r.stdout or "")
        if any(s in err for s in ("rejected", "non-fast-forward", "fetch first", "stale info")):
            return False
        raise RemoteError("推送失败：" + err.strip()[-500:])


# -- records ----------------------------------------------------------------------

def _dump(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def _write_lf(path: Path, text: str) -> None:
    """LF on every platform, so both computers produce byte-identical files."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_record(path: Path, obj: Dict[str, Any]) -> bool:
    data = _dump(obj)
    if path.exists() and path.read_text(encoding="utf-8") == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    _write_lf(tmp, data)
    os.replace(tmp, path)
    return True


def read_record(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            return None, "文件过大"
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None, "缺少文件"
    except (OSError, ValueError) as e:
        return None, f"无法解析：{e}"
    if not isinstance(data, dict):
        return None, "格式不对"
    return data, None


def _short_str(v: Any, limit: int = 300) -> bool:
    return v is None or (isinstance(v, str) and len(v) <= limit)


def check_message(env: Dict[str, Any], dev: str, stem: str) -> Optional[str]:
    if env.get("kind") != "message" or env.get("format") != FORMAT:
        return "不是 format 1 的消息记录"
    if env.get("id") != stem or not MSG_ID_RE.match(stem):
        return "消息 ID 与文件名不符"
    if env.get("source_device") != dev:
        return "发送设备与所在目录不符"
    if not isinstance(env.get("target_device"), str) or not DEVICE_RE.match(env["target_device"]):
        return "target_device 无效"
    if env.get("source_agent") not in AGENTS or env.get("target_agent") not in AGENTS:
        return "Agent 无效"
    if env.get("mode") not in ("sync", "handoff"):
        return "mode 无效"
    for k in ("title", "user_command", "project_id"):
        if not isinstance(env.get(k), str) or not env[k].strip() or len(env[k]) > 2000:
            return f"{k} 无效"
    for k in ("created_at", "expires_at"):
        if not isinstance(env.get(k), (int, float)):
            return f"{k} 无效"
    if not isinstance(env.get("context"), dict):
        return "context 无效"
    for k in ("project_name", "git_remote", "git_branch", "git_head", "source_label", "target_label", "content_hash"):
        if not _short_str(env.get(k)):
            return f"{k} 无效"
    return None


def check_event(ev: Dict[str, Any], dev: str, stem: str) -> Optional[str]:
    if ev.get("kind") != "event" or ev.get("format") != FORMAT:
        return "不是 format 1 的回执记录"
    if ev.get("id") != stem or not STEM_RE.match(stem):
        return "回执 ID 与文件名不符"
    if ev.get("device") != dev:
        return "回执设备与所在目录不符"
    if ev.get("type") not in EVENT_TYPES or not stem.split(".")[1] == ev["type"]:
        return "回执类型无效"
    if ev.get("message_id") != stem.split(".")[0]:
        return "message_id 与文件名不符"
    if not isinstance(ev.get("ts"), (int, float)):
        return "ts 无效"
    if ev.get("agent") is not None and ev["agent"] not in AGENTS:
        return "agent 无效"
    for k in ("detail", "summary"):
        if not _short_str(ev.get(k), 4000):
            return f"{k} 无效"
    if ev.get("expires_at") is not None and not isinstance(ev["expires_at"], (int, float)):
        return "expires_at 无效"
    return None


def message_envelope(store: Store, m: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "message", "format": FORMAT, "id": m["id"],
        "source_device": m["source_device"], "source_label": cfg.get("label") or m["source_device"],
        "target_device": m["target_device"], "target_label": store.device_label(m["target_device"]),
        "source_agent": m["source_agent"], "target_agent": m["target_agent"], "mode": m["mode"],
        "title": m["title"], "user_command": m["user_command"], "context": json.loads(m["context_json"]),
        "project_id": m["project_id"], "project_name": m["project_name"], "git_remote": m["git_remote"],
        "git_branch": m["git_branch"], "git_head": m["git_head"], "content_hash": m["content_hash"],
        "created_at": m["created_at"], "expires_at": m["expires_at"],
    }


def render_envelope(store: Store, env: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """Re-validate untrusted content and render it locally (never trust the sender's Markdown)."""
    ctx = render.validate(env["context"], mode=env["mode"], target=env["target_agent"])
    env = dict(env, context=ctx, title=ctx["title"])
    body = render.render(ctx, mid=env["id"], source=env["source_agent"], target=env["target_agent"],
                         mode=env["mode"], project_id=env["project_id"], user_command=env["user_command"],
                         created_at=env["created_at"], branch=env.get("git_branch"), head=env.get("git_head"),
                         source_device=store.device_label(env["source_device"]),
                         target_device=store.device_label(env["target_device"]))
    return env, body


# -- export / import --------------------------------------------------------------

def _export(store: Store, mb: Mailbox) -> Tuple[List[str], List[str]]:
    own = mb.own()
    sent: List[str] = []
    for m in store.unsent_outbound():
        write_record(own / "messages" / f"{m['id']}.json", sign(message_envelope(store, m, mb.cfg), mb.key))
        store.mark_imported(f"{mb.device}/m/{m['id']}", mb.device, "message", m["id"], "local")
        sent.append(m["id"])
    rids: List[str] = []
    for row in store.unpushed():
        ev = json.loads(row["payload"])
        write_record(own / "events" / f"{ev['id']}.json", sign(ev, mb.key))
        rids.append(row["record_id"])
    return sent, rids


def _import_message(store: Store, mb: Mailbox, env: Dict[str, Any], dev: str) -> str:
    existing = store.find(env["id"])
    if existing:
        same = existing.get("source_device") == env["source_device"]
        return "exists" if same else "id-conflict"
    if dev == mb.device:  # our own message, local database was reset
        env, body = render_envelope(store, env)
        store.insert_remote(env, body, SENT)
        return "restored"
    if env["target_device"] != mb.device:
        return "not-for-me"
    env, body = render_envelope(store, env)
    store.insert_remote(env, body, INBOUND)
    return "received"


def _import(store: Store, mb: Mailbox, result: Dict[str, Any]) -> None:
    root = mb.dir / "devices"
    if not root.is_dir():
        return
    events: List[Tuple[float, str, str, str, Dict[str, Any]]] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        dev = d.name
        if not DEVICE_RE.match(dev):
            continue
        if dev != mb.device:
            card, err = read_record(d / "device.json")
            if err or not verify(card, mb.key) or card.get("id") != dev:
                result["rejected"].append(f"设备 {dev}：设备卡片签名不符（两台电脑的配对码不一致？）")
                continue
            label = re.sub(r"\s+", " ", str(card.get("label") or dev)).strip()[:40] or dev
            plat = card.get("platform") if card.get("platform") in ("macos", "windows", "linux") else None
            store.upsert_device(dev, label, plat)
        for f in sorted((d / "messages").glob("*.json")):
            rid = f"{dev}/m/{f.stem}"
            if store.is_imported(rid):
                continue
            env, err = read_record(f)
            if not err and not verify(env, mb.key):
                err = "签名不符，已拒收"
            if not err:
                err = check_message(env, dev, f.stem)
            if err:
                result["rejected"].append(f"{rid}：{err}")
                continue
            try:
                with store.tx():
                    outcome = _import_message(store, mb, env, dev)
                    store.mark_imported(rid, dev, "message", env["id"], outcome)
            except BridgeError as e:  # content failed validation: permanent
                store.mark_imported(rid, dev, "message", env["id"], f"invalid: {e}")
                result["rejected"].append(f"{rid}：{e}")
                continue
            if outcome == "received":
                result["received"].append(env["id"])
        for f in sorted((d / "events").glob("*.json")):
            rid = f"{dev}/e/{f.stem}"
            if store.is_imported(rid):
                continue
            ev, err = read_record(f)
            if not err and not verify(ev, mb.key):
                err = "签名不符，已拒收"
            if not err:
                err = check_event(ev, dev, f.stem)
            if err:
                result["rejected"].append(f"{rid}：{err}")
                continue
            events.append((float(ev["ts"]), f.stem, dev, rid, ev))
    for _, _, dev, rid, ev in sorted(events, key=lambda x: (x[0], x[1])):
        with store.tx():
            outcome = store.apply_remote_event(ev, dev)
            store.mark_imported(rid, dev, "event", ev["message_id"], outcome)
        if outcome == "applied":
            result["receipts"].append(f"#{ev['message_id']} {ev['type']}（{dev}）")


def require_config() -> Dict[str, Any]:
    cfg = config.load()
    if not cfg:
        raise RemoteError("本机还没有配置跨电脑同步。先运行：asb remote setup <Git 仓库地址> --device <本机名>")
    return cfg


def sync(store: Store, timeout: float = 30) -> Dict[str, Any]:
    """Commit local outbox → pull --rebase → import → push (retried if the remote moved)."""
    cfg = require_config()
    mb = Mailbox(cfg)
    if not (mb.dir / ".git").exists():
        raise RemoteError(f"本机信箱目录 {mb.dir} 不存在，请重新运行 asb remote setup")
    deadline = time.monotonic() + timeout

    def left() -> float:
        return max(5.0, deadline - time.monotonic())

    result: Dict[str, Any] = {"uploaded": [], "receipts_out": 0, "received": [], "receipts": [],
                              "rejected": [], "pushed": False}
    update_state(last_attempt=time.time())
    try:
        with SyncLock(timeout=min(timeout, 20)):
            sent, rids = _export(store, mb)
            mb.commit(f"asb {mb.device}: outbox")
            for _ in range(4):
                mb.pull(left())
                _import(store, mb, result)
                s2, r2 = _export(store, mb)  # RECEIVED receipts written by the import
                sent += s2
                rids += r2
                mb.commit(f"asb {mb.device}: receipts")
                if not mb.ahead():
                    break
                if mb.push(left()):
                    result["pushed"] = True
                    break
            else:
                raise RemoteError("推送多次被拒绝（另一台电脑同时在推送），稍后重试")
            store.mark_sent(list(dict.fromkeys(sent)))
            store.mark_pushed(list(dict.fromkeys(rids)))
            result["uploaded"] = list(dict.fromkeys(sent))
            result["receipts_out"] = len(set(rids))
    except Exception as e:
        st = load_state()
        update_state(last_error=str(e), last_error_at=time.time(), failures=int(st.get("failures") or 0) + 1)
        raise
    update_state(last_ok=time.time(), failures=0, last_error=None,
                 last_rejected=result["rejected"][-20:])
    return result


# -- setup ------------------------------------------------------------------------

def _rmtree(path: Path) -> None:
    def onerror(func, p, _exc):  # Git marks objects read-only on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        shutil.rmtree(path, onerror=onerror)


def setup(store: Store, url: str, device: str, label: Optional[str] = None,
          pair_code: Optional[str] = None, reuse: bool = False, timeout: float = 60) -> Dict[str, Any]:
    if not DEVICE_RE.match(device or ""):
        raise RemoteError("设备 ID 只能用小写字母、数字和连字符（最长 32 位），例如 mac、win")
    cur = config.load()
    if cur and (cur["url"] != url or cur["device"] != device):
        raise RemoteError(f"本机已经配置了信箱 {cur['url']}（设备 {cur['device']}）。"
                          "要换信箱或设备名，先运行 asb remote disable")
    mdir = paths.mailbox_dir()
    if (mdir / ".git").exists():
        origin = _git(mdir, "remote", "get-url", "origin", check=False).stdout.strip()
        if origin != url:
            raise RemoteError(f"{mdir} 已是另一个信箱（{origin}）的克隆；先运行 asb remote disable")
        _git(mdir, "fetch", "-q", "origin", timeout=timeout, interactive=True, check=False)
    else:
        if mdir.exists() and any(mdir.iterdir()):
            raise RemoteError(f"{mdir} 已存在且不是 Git 仓库，请先移走它")
        _git(paths.home(), "clone", "-q", url, str(mdir), timeout=timeout * 2, interactive=True)

    has_commits = _git(mdir, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0
    if has_commits:
        branch = _git(mdir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if not (mdir / "asb-mailbox.json").exists():
            raise RemoteError("这个仓库已有其他内容，不是 Agent Sync 信箱。请新建一个空的私有仓库专门用来同步")
    else:
        branch = (cur or {}).get("branch") or "main"
        _git(mdir, "symbolic-ref", "HEAD", f"refs/heads/{branch}")

    devroot = mdir / "devices"
    peers = sorted(p.name for p in devroot.iterdir() if p.is_dir() and p.name != device) if devroot.is_dir() else []
    own_card = devroot / device / "device.json"
    generated = False
    if pair_code:
        code = normalize_code(pair_code)
    elif cur:
        code = cur["pair_code"]
    elif peers or own_card.exists():
        names = "、".join(peers) or device
        raise RemoteError(f"这个信箱里已经有设备 {names}。请在那台电脑的终端运行 `asb remote pair-code` 查看配对码，"
                          "再加上 --pair-code <配对码> 重新运行")
    else:
        code = new_pair_code()
        generated = True
    key = derive_key(code)
    bad = [p for p in peers if not verify(read_record(devroot / p / "device.json")[0], key)]
    if bad:
        raise RemoteError(f"配对码与信箱里的设备 {'、'.join(bad)} 不一致，请核对后重试")
    if own_card.exists() and not cur and not reuse:
        raise RemoteError(f"设备 ID `{device}` 已被另一台电脑使用。换一个 --device；如果确实是本机重装，加 --reuse-device")

    plat = config.platform_name()
    cfg = {"device": device, "label": label or (cur or {}).get("label") or DEFAULT_LABELS.get(plat, device),
           "platform": plat, "url": url, "branch": branch, "pair_code": code,
           "hook_sync": (cur or {}).get("hook_sync", True), "hook_wait": (cur or {}).get("hook_wait", DEFAULT_HOOK_WAIT),
           "created_at": (cur or {}).get("created_at") or time.time()}
    config.save(cfg)
    store.device = device
    mb = Mailbox(cfg)
    if not (mdir / "asb-mailbox.json").exists():
        write_record(mdir / "asb-mailbox.json", {"kind": "asb-mailbox", "format": FORMAT})
        _write_lf(mdir / ".gitattributes", "* -text\n")
        _write_lf(mdir / "README.md", "# Agent Sync Bridge mailbox\n\n由 `asb remote sync` 自动维护，请不要手动修改。\n"
                  "每台电脑只写 `devices/<设备ID>/` 下的文件；所有记录都用配对码签名。\n")
    write_record(mb.own() / "device.json", sign({"kind": "device", "format": FORMAT, "id": device,
                                                 "label": cfg["label"], "platform": plat,
                                                 "created_at": cfg["created_at"]}, key))
    mb.commit(f"asb {device}: join mailbox")
    out: Dict[str, Any] = {"config": cfg, "generated": generated, "peers": peers}
    try:
        out["sync"] = sync(store, timeout=timeout)
    except RemoteError as e:
        out["sync_error"] = str(e)
    return out


def disable() -> None:
    config.remove()
    _rmtree(paths.mailbox_dir())
    try:
        paths.sync_state().unlink()
    except OSError:
        pass


# -- hook helpers -------------------------------------------------------------------

def backing_off(st: Optional[Dict[str, Any]] = None) -> bool:
    """After failed syncs (usually offline), wait 15 s, 30 s, ... up to 5 min."""
    st = st if st is not None else load_state()
    n = int(st.get("failures") or 0)
    return bool(n) and time.time() - float(st.get("last_attempt") or 0) < min(300, 15 * 2 ** (n - 1))


def hook_sync_due(cfg: Dict[str, Any]) -> bool:
    if cfg.get("hook_sync") is False:
        return False
    st = load_state()
    if time.time() - float(st.get("last_attempt") or 0) < HOOK_MIN_INTERVAL or backing_off(st):
        return False
    return not lock_busy()


def spawn(wait: float = 0.0) -> Optional[int]:
    """Run `asb remote sync` in a detached process; wait up to `wait` seconds.
    Returns its exit code, or None if it is still running in the background."""
    if os.environ.get("ASB_NO_AUTO_SYNC"):  # tests: only explicit `asb remote sync` touches the mailbox
        return None
    update_state(last_attempt=time.time())
    cmd = [sys.executable, "-X", "utf8", "-I", str(paths.ENTRY), "remote", "sync", "--background"]
    if os.environ.get("ASB_SYNC_INLINE"):  # tests: same command, but finish before returning
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, cwd=str(paths.home())).returncode
    kw: Dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              cwd=str(paths.home()), close_fds=True)
    if os.name == "nt":
        kw["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen(cmd, **kw)
    if wait <= 0:
        return None
    try:
        return p.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        return None


def status(store: Store) -> Dict[str, Any]:
    cfg = config.load()
    if not cfg:
        return {"configured": False}
    st = load_state()
    inbound = store.db.execute("SELECT COUNT(*) FROM messages WHERE transport=? AND status IN ('PENDING','DELIVERED')",
                               (INBOUND,)).fetchone()[0]
    return {
        "configured": True, "device": cfg["device"], "label": cfg.get("label"), "platform": cfg.get("platform"),
        "url": cfg["url"], "branch": cfg.get("branch"), "hook_sync": cfg.get("hook_sync", True),
        "peers": [d for d in store.devices() if d["id"] != cfg["device"]],
        "pending_push": store.pending_push_count(), "inbound_open": inbound,
        "last_ok": iso(st.get("last_ok")), "last_attempt": iso(st.get("last_attempt")),
        "last_error": st.get("last_error"), "failures": st.get("failures") or 0,
        "last_rejected": st.get("last_rejected") or [],
    }
