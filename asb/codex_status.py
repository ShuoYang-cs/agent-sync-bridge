"""Read-only check of the bridge's Codex hook via the public app-server
`hooks/list` request. Trusting the hook is left to the user (Codex `/hooks`
or the VS Code extension's hook settings); this module never writes config.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from . import __version__

MARK = "hook codex"


def find_codex() -> Optional[str]:
    if os.environ.get("ASB_CODEX"):
        return os.environ["ASB_CODEX"]
    exe = "codex.exe" if os.name == "nt" else "codex"
    cands = sorted(glob.glob(os.path.expanduser(
        f"~/.vscode/extensions/openai.chatgpt-*/bin/*/{exe}")), key=os.path.getmtime)
    if cands:
        return cands[-1]
    return shutil.which("codex")


def _rpc(codex: str, requests: List[Dict[str, Any]], timeout: float = 30) -> Dict[int, Any]:
    p = subprocess.Popen([codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
    results: Dict[int, Any] = {}
    try:
        def send(obj):
            p.stdin.write(json.dumps(obj) + "\n")
            p.stdin.flush()

        def wait(rid):
            while True:
                line = p.stdout.readline()
                if not line:
                    raise RuntimeError("codex app-server exited")
                msg = json.loads(line)
                if msg.get("id") == rid and "method" not in msg:
                    if "error" in msg:
                        raise RuntimeError(str(msg["error"]))
                    return msg.get("result")

        send({"id": 0, "method": "initialize",
              "params": {"clientInfo": {"name": "agent-sync-bridge", "version": __version__}}})
        wait(0)
        send({"method": "initialized"})
        for i, r in enumerate(requests, 1):
            send({"id": i, **r})
            results[i] = wait(i)
    finally:
        try:
            p.stdin.close()
            p.wait(timeout=5)
        except Exception:
            p.kill()
    return results


def bridge_hooks(cwd: Optional[str] = None) -> List[Dict[str, Any]]:
    codex = find_codex()
    if not codex:
        raise RuntimeError("未找到 codex 可执行文件")
    res = _rpc(codex, [{"method": "hooks/list", "params": {"cwds": [cwd or os.path.expanduser("~")]}}])[1]
    entries = res.get("data") or res.get("entries") or [] if isinstance(res, dict) else []
    return [h for e in entries for h in e.get("hooks", []) if MARK in (h.get("command") or "")]
