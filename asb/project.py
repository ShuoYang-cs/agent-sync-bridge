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


@dataclass
class Project:
    id: str
    root: str
    source: str  # link | git-remote | git-dir | path
    branch: Optional[str] = None
    head: Optional[str] = None
    worktree: Optional[str] = None


def _git(cwd: str, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=3,
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


def load_links() -> Dict[str, str]:
    f = paths.projects_file()
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {os.path.realpath(k): v for k, v in data.get("links", {}).items()}


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
    removed = data.get("links", {}).pop(os.path.realpath(path), None) is not None
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
    extra = dict(branch=branch, head=head, worktree=worktree)
    if override:
        return Project(override, cwd, "env", **extra)

    links = load_links()
    best = max((r for r in links if _under(cwd, r)), key=len, default=None)
    if best:
        return Project(links[best], best, "link", **extra)

    if worktree:
        remote = _git(cwd, "config", "--get", "remote.origin.url")
        if remote:
            return Project(normalize_remote(remote), worktree, "git-remote", **extra)
        common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common:
            root = str(Path(common).parent)
            return Project("path:" + root, root, "git-dir", **extra)

    return Project("path:" + cwd, cwd, "path", **extra)
