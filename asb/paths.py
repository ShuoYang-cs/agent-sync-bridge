from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENTRY = REPO / "bin" / "asb.py"


def home() -> Path:
    """Runtime data dir. Local only; never inside a project repo."""
    p = Path(os.environ.get("ASB_HOME") or Path.home() / ".agent-sync-bridge")
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return home() / "bridge.db"


def projects_file() -> Path:
    return home() / "projects.json"


def log_file() -> Path:
    return home() / "hook.log"


def remote_file() -> Path:
    """Cross-device settings incl. the pairing code. Never synced."""
    return home() / "remote.json"


def mailbox_dir() -> Path:
    """Local clone of the Git mailbox repository."""
    return home() / "mailbox"


def sync_lock() -> Path:
    return home() / "sync.lock"


def sync_state() -> Path:
    return home() / "sync-state.json"


def _quote(p: str) -> str:
    return f'"{p}"' if " " in p else p


def windows_python() -> str:
    """How Windows hooks and agents start Python. `py` is on PATH in Git Bash,
    PowerShell and cmd alike, so the command works whichever shell runs it."""
    if shutil.which("py"):
        return "py -3"
    return _quote(Path(sys.executable).as_posix())


def cli_command() -> str:
    """The command agents and hooks use to run asb on this machine."""
    if os.environ.get("ASB_CLI"):
        return os.environ["ASB_CLI"]
    if os.name == "nt":
        return f"{windows_python()} -X utf8 -I {_quote(ENTRY.as_posix())}"
    found = shutil.which("asb")
    return found or str(REPO / "bin" / "asb")
