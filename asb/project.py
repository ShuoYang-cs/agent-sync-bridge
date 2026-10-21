"""Resolve which project a working directory belongs to.

Order: explicit link in projects.json (longest path prefix wins) -> git origin
remote (shared by every worktree/branch of the repo) -> git common dir -> realpath.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from . import paths


STABLE_SOURCES = ("link", "git-remote", "env")  # same id on every computer


@dataclass
class Project:
    id: str
    root: str
    source: str  # link | git-remote | git-dir | path | env
    branch: Optional[str] = None
    head: Optional[str] = None
    worktree: Optional[str] = None
    remote: Optional[str] = None  # normalized origin URL, used to match across computers

    @property
    def name(self) -> str:
        return os.path.basename((self.worktree or self.root).rstrip("/\\")) or self.root

    @property
    def stable(self) -> bool:
        return self.source in STABLE_SOURCES


def _git(cwd: str, *args: str) -> Optional[str]:
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3,
            stdin=subprocess.DEVNULL, **kw,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def normalize_remote(url: str) -> str:
    u = url.strip()
    u = re.sub(r"^[a-z+]+://", "", u)
    u = re.sub(r"^[^@/]+@", "", u)  # strip user@
    u = u.replace(":", "/", 1) if re.match(r"^[^/]+:[^/]", u) else u
    u = re.sub(r"\.git$", "", u).rstrip("/")
    return u.lower()


def _norm(p: str) -> str:
    """Case-insensitive on Windows, where C:\\Dev and c:\\dev are the same folder."""
    return os.path.normcase(os.path.realpath(p))


def load_links() -> Dict[str, str]:
    f = paths.projects_file()
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {_norm(k): v for k, v in data.get("links", {}).items()}


def save_link(path: str, project_id: str) -> None:
    f = paths.projects_file()
    data = {"links": {}}
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            pass
    data.setdefault("links", {})[os.path.realpath(path)] = project_id
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def remove_link(path: str) -> bool:
    f = paths.projects_file()
    if not f.exists():
        return False
    data = json.loads(f.read_text(encoding="utf-8"))
    links = data.get("links", {})
    keys = [k for k in links if _norm(k) == _norm(path)]
    removed = bool(keys)
    for k in keys:
        del links[k]
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return removed


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def resolve(cwd: Optional[str] = None) -> Project:
    override = os.environ.get("ASB_PROJECT")
    cwd = os.path.realpath(cwd or os.getcwd())
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    head = _git(cwd, "rev-parse", "--short", "HEAD")
    worktree = _git(cwd, "rev-parse", "--show-toplevel")
    if worktree:
        worktree = os.path.realpath(worktree)
    origin = _git(cwd, "config", "--get", "remote.origin.url") if worktree else None
    remote = normalize_remote(origin) if origin else None
    extra = dict(branch=branch, head=head, worktree=worktree, remote=remote)
    if override:
        return Project(override, cwd, "env", **extra)

    links = load_links()
    key = os.path.normcase(cwd)
    best = max((r for r in links if _under(key, r)), key=len, default=None)
    if best:
        root = next((p for p in (worktree, cwd) if p and os.path.normcase(p) == best), best)
        return Project(links[best], root, "link", **extra)

    if worktree:
        if remote:
            return Project(remote, worktree, "git-remote", **extra)
        common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common:
            root = str(Path(common).parent)
            return Project("path:" + root, root, "git-dir", **extra)

    return Project("path:" + cwd, cwd, "path", **extra)
