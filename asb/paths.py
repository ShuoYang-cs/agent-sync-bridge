from __future__ import annotations

import os
from pathlib import Path


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
