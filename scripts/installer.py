"""Install / uninstall Agent Sync Bridge integrations (user level, reversible).

Changes (all recorded in ~/.agent-sync-bridge/install-manifest.json, originals
backed up to ~/.agent-sync-bridge/backups/<timestamp>/):
  ~/.local/bin/asb                       symlink to this repo's bin/asb
                                         (Windows: small asb / asb.cmd launcher files)
  ~/.claude/skills/agent-sync/SKILL.md   Claude Code skill
  ~/.agents/skills/agent-sync/SKILL.md   Codex skill
  ~/.claude/settings.json                + hooks.UserPromptSubmit entry (asb hook claude)
  ~/.codex/hooks.json                    + hooks.UserPromptSubmit entry (asb hook codex)
  ~/.agent-sync-bridge/projects.json     optional project links (--link PATH=ID)
Never touches project repos, CLAUDE.md, AGENTS.md, or login credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from asb import paths as asb_paths  # noqa: E402

IS_WINDOWS = os.name == "nt"
HOME = Path.home()
DATA = Path(os.environ.get("ASB_HOME") or HOME / ".agent-sync-bridge")
MANIFEST = DATA / "install-manifest.json"
MARKER = ".asb-managed"

BIN_LINK = HOME / ".local/bin/asb"
WIN_SHIMS = {HOME / ".local/bin/asb.cmd": "@{cli} %*\r\n", HOME / ".local/bin/asb": "#!/bin/sh\nexec {cli} \"$@\"\n"}
SHIM_MARK = "agent-sync-bridge"
CLAUDE_SETTINGS = HOME / ".claude/settings.json"
CODEX_HOOKS = HOME / ".codex/hooks.json"
SKILL_DIRS = {"claude": HOME / ".claude/skills/agent-sync", "codex": HOME / ".agents/skills/agent-sync"}
LABELS = {"claude": "Claude Code", "codex": "Codex"}
COMPONENTS = ("cli", "claude", "codex", "links")
HOOK_TARGETS = {
    "claude": (CLAUDE_SETTINGS, {"timeout": 10}, ""),
    "codex": (CODEX_HOOKS, {"timeout": 10, "statusMessage": "Agent Sync：检查待接收消息",
                            "additionalContextLimit": 8000}, "（需在 Codex 中信任一次）"),
}


def cli() -> str:
    """How skills and hooks call asb. On Windows `py -3 -X utf8 -I <repo>/bin/asb.py`
    works the same in Git Bash (Claude Code), PowerShell (Codex) and cmd."""
    return asb_paths.cli_command() if IS_WINDOWS else str(BIN_LINK)


def hook_cmd(agent: str) -> str:
    return f"{cli()} hook {agent}"


def is_bridge_hook(h: dict) -> bool:
    c = h.get("command") or ""
    return any(m in c for m in ("asb hook ", "asb.py hook ", 'asb.py" hook ', "agent-sync-bridge"))


def load_json(p: Path) -> dict:
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else {}


def write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".asb-tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def backup(paths, stamp: str) -> Path:
    d = DATA / "backups" / stamp
    d.mkdir(parents=True, exist_ok=True)
    for p in paths:
        if p.exists():
            name = re.sub(r"[\\/:]+", "__", str(p).replace(str(HOME), "HOME")).strip("_")
            shutil.copy2(p, d / name)
    return d


def add_hook(settings: dict, agent: str, extra: dict) -> bool:
    groups = settings.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    for g in groups:
        for h in g.get("hooks", []):
            if is_bridge_hook(h):
                if h.get("command") == hook_cmd(agent) and all(h.get(k) == v for k, v in extra.items()):
                    return False
                h.update({"type": "command", "command": hook_cmd(agent), **extra})
                return True
    groups.append({"hooks": [{"type": "command", "command": hook_cmd(agent), **extra}]})
    return True


def remove_hooks(settings: dict) -> int:
    n = 0
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        kept = []
        for g in hooks[event]:
            inner = [h for h in g.get("hooks", []) if not is_bridge_hook(h)]
            n += len(g.get("hooks", [])) - len(inner)
            if inner:
                kept.append({**g, "hooks": inner})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if "hooks" in settings and not settings["hooks"]:
        del settings["hooks"]
    return n


# The Claude Code plugin ships a pre-rendered skill that calls the CLI inside the
# plugin directory; Claude Code substitutes ${CLAUDE_PLUGIN_ROOT} when it loads.
PLUGIN_SKILL = REPO / "skills/agent-sync/SKILL.md"
PLUGIN_ASB = "${CLAUDE_PLUGIN_ROOT}/bin/asb"


def render_skill(agent: str, asb: str = None, platform: str = None) -> str:
    other = "codex" if agent == "claude" else "claude"
    platform = platform or ("windows" if IS_WINDOWS else "posix")
    t = (REPO / "templates/SKILL.template.md").read_text(encoding="utf-8")
    # keep the <!-- if:<platform> --> blocks for this platform, drop the others
    t = re.sub(r"<!-- if:(\w+) -->\n(.*?)<!-- endif -->\n",
               lambda m: m.group(2) if m.group(1) == platform else "", t, flags=re.S)
    payload = (REPO / "templates/payload-example.json").read_text(encoding="utf-8").rstrip("\n")
    for k, v in {"{{PAYLOAD}}": payload, "{{AGENT}}": agent, "{{OTHER}}": other, "{{AGENT_LABEL}}": LABELS[agent],
                 "{{OTHER_LABEL}}": LABELS[other], "{{ASB}}": asb or cli()}.items():
        t = t.replace(k, v)
    return t


def render_plugin(a) -> int:
    PLUGIN_SKILL.parent.mkdir(parents=True, exist_ok=True)
    PLUGIN_SKILL.write_text(render_skill("claude", PLUGIN_ASB, "posix"), encoding="utf-8")
    print(f"✓ {PLUGIN_SKILL.relative_to(REPO)}")
    return 0


def install(a) -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    only = set(a.only.split(",")) if a.only else set(COMPONENTS)
    unknown = only - set(COMPONENTS)
    if unknown:
        print(f"未知组件: {sorted(unknown)}；可选: {','.join(COMPONENTS)}")
        return 2
    DATA.mkdir(parents=True, exist_ok=True)
    targets = [CLAUDE_SETTINGS, CODEX_HOOKS] + [d / "SKILL.md" for d in SKILL_DIRS.values()]
    bdir = backup(targets, stamp)
    manifest = load_json(MANIFEST) or {"installs": []}
    log = {"at": stamp, "backup_dir": str(bdir), "repo": str(REPO), "components": sorted(only), "steps": []}

    if "cli" in only and IS_WINDOWS:
        for shim, body in WIN_SHIMS.items():
            if shim.exists() and SHIM_MARK not in shim.read_text(encoding="utf-8", errors="replace"):
                print(f"✗ {shim} 已存在且不是本工具写的，跳过")
                continue
            shim.parent.mkdir(parents=True, exist_ok=True)
            shim.write_bytes(body.format(cli=cli()).encode("utf-8"))
            log["steps"].append(f"shim {shim}")
            print(f"✓ CLI       {shim}")
    elif "cli" in only:
        BIN_LINK.parent.mkdir(parents=True, exist_ok=True)
        target = REPO / "bin/asb"
        if BIN_LINK.exists() and not BIN_LINK.is_symlink():
            print(f"✗ {BIN_LINK} 已存在且不是符号链接，跳过（请手动处理）")
            return 1
        if BIN_LINK.is_symlink():
            if Path(os.readlink(BIN_LINK)) != target:
                print(f"! {BIN_LINK} 原指向 {os.readlink(BIN_LINK)}，已替换")
            BIN_LINK.unlink()
        BIN_LINK.symlink_to(target)
        log["steps"].append(f"symlink {BIN_LINK} -> {target}")
        print(f"✓ CLI       {BIN_LINK} → {target}")

    for agent in ("claude", "codex"):
        if agent not in only:
            continue
        d = SKILL_DIRS[agent]
        if d.exists() and not (d / MARKER).exists():
            print(f"✗ {d} 已存在且不是本工具安装的，跳过 {agent} skill")
        else:
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(render_skill(agent), encoding="utf-8")
            (d / MARKER).write_text(str(REPO) + "\n", encoding="utf-8")
            log["steps"].append(f"skill {d}")
            print(f"✓ Skill     {d}/SKILL.md")
        path, extra, note = HOOK_TARGETS[agent]
        cfg = load_json(path)
        if add_hook(cfg, agent, extra):
            write_json(path, cfg)
            log["steps"].append(f"hook {path}")
        print(f"✓ Hook      {path} · UserPromptSubmit → {hook_cmd(agent)}{note}")

    if "links" in only and a.link:
        links_file = DATA / "projects.json"
        data = load_json(links_file) or {"links": {}}
        for spec in a.link:
            path, _, pid = spec.rpartition("=")
            data.setdefault("links", {})[os.path.realpath(os.path.expanduser(path))] = pid
            print(f"✓ Project   {path} → {pid}")
        write_json(links_file, data)
        log["steps"].append(f"links {a.link}")

    manifest["installs"].append(log)
    write_json(MANIFEST, manifest)
    print(f"\n备份：{bdir}\n清单：{MANIFEST}")
    return 0


def uninstall(a) -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bdir = backup([CLAUDE_SETTINGS, CODEX_HOOKS], "uninstall-" + stamp)
    for path in (CLAUDE_SETTINGS, CODEX_HOOKS):
        s = load_json(path)
        n = remove_hooks(s)
        if n:
            if path == CODEX_HOOKS and not s:
                path.unlink()
                print(f"✓ 删除 {path}（只包含本工具的 hook）")
            else:
                write_json(path, s)
                print(f"✓ 从 {path} 移除 {n} 个 hook")
    for d in SKILL_DIRS.values():
        if (d / MARKER).exists():
            shutil.rmtree(d)
            print(f"✓ 删除 skill {d}")
            parent = d.parent
            if parent == HOME / ".agents/skills" and not any(parent.iterdir()):
                parent.rmdir()
                if not any(parent.parent.iterdir()):
                    parent.parent.rmdir()
    if BIN_LINK.is_symlink() and Path(os.readlink(BIN_LINK)) == REPO / "bin/asb":
        BIN_LINK.unlink()
        print(f"✓ 删除 {BIN_LINK}")
    for shim in WIN_SHIMS:
        if shim.is_file() and not shim.is_symlink() and SHIM_MARK in shim.read_text(encoding="utf-8", errors="replace"):
            shim.unlink()
            print(f"✓ 删除 {shim}")
    print("ℹ Codex 的 hooks.state 信任记录（~/.codex/config.toml）只保存 hash，hook 删除后不会再运行，可在 Codex /hooks 中清理。")
    if a.purge_data:
        shutil.rmtree(DATA, ignore_errors=True)
        print(f"✓ 删除数据目录 {DATA}")
    else:
        print(f"ℹ 保留消息数据 {DATA}（加 --purge-data 一并删除）；卸载前备份：{bdir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install")
    p.add_argument("--link", action="append", default=[], help="PATH=PROJECT_ID")
    p.add_argument("--only", help="逗号分隔的组件: cli,claude,codex,links（默认全部）")
    p = sub.add_parser("uninstall")
    p.add_argument("--purge-data", action="store_true")
    sub.add_parser("render-plugin", help="regenerate skills/agent-sync/SKILL.md for the Claude Code plugin")
    a = ap.parse_args()
    return {"install": install, "uninstall": uninstall, "render-plugin": render_plugin}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
