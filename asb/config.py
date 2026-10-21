"""Cross-device settings in ~/.agent-sync-bridge/remote.json (local only).

{"device": "mac", "label": "MacBook", "platform": "macos", "url": "<git remote>",
 "branch": "main", "pair_code": "XXXXX-XXXXX-XXXXX-XXXXX", "hook_sync": true, "hook_wait": 2.5}
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional

from . import paths


def platform_name() -> str:
    if os.environ.get("ASB_PLATFORM"):  # tests simulate the other computer
        return os.environ["ASB_PLATFORM"]
    if os.name == "nt":
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def load() -> Optional[Dict[str, Any]]:
    f = paths.remote_file()
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("device") and data.get("url") else None


def save(cfg: Dict[str, Any]) -> None:
    f = paths.remote_file()
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)  # holds the pairing code
    os.replace(tmp, f)


def remove() -> bool:
    f = paths.remote_file()
    if f.exists():
        f.unlink()
        return True
    return False


def local_device() -> Optional[str]:
    c = load()
    return c["device"] if c else None
