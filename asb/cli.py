"""asb — Agent Sync Bridge command line."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import AGENT_LABELS, AGENTS, __version__, hooks, paths, project, render, router
from .store import ACKNOWLEDGED, DELIVERED, FAILED, PENDING, BridgeError, Store, iso

STATUS_ZH = {
    PENDING: "PENDING · 已写入队列，对方尚未读取",
    DELIVERED: "DELIVERED · 已投递进对方上下文，等待对方确认",
    ACKNOWLEDGED: "ACKNOWLEDGED · 对方已阅读并确认",
    FAILED: "FAILED · 失败（可 retry）",
}

SESSION_ENV = {"claude": ("CLAUDE_CODE_SESSION_ID",), "codex": ("CODEX_THREAD_ID", "CODEX_SESSION_ID")}


def env_session(agent: str) -> Optional[str]:
    for k in SESSION_ENV.get(agent, ()):
        if os.environ.get(k):
            return os.environ[k]
    return None


def session_key(agent: str, explicit: Optional[str]) -> Optional[str]:
    sid = explicit or env_session(agent)
    return f"{agent}:{sid}" if sid else None


def brief(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": m["id"], "from": m["source_agent"], "to": m["target_agent"], "mode": m["mode"],
        "title": m["title"], "status": m["status"], "project": m["project_id"],
        "created_at": iso(m["created_at"]), "delivered_at": iso(m["delivered_at"]),
        "acked_at": iso(m["acked_at"]), "ack_summary": m["ack_summary"], "error": m["error"],
        "branch": m["git_branch"],
    }


def emit(obj: Any, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(text)


def _table(msgs: List[Dict[str, Any]], show_project: bool = False) -> str:
    if not msgs:
        return "（无）"
    rows = []
    for m in msgs:
        arrow = f"{m['source_agent']}→{m['target_agent']}"
        mode = "交接" if m["mode"] == "handoff" else "同步"
        line = f"#{m['id']}  {arrow:<13} {mode}  {m['status']:<12} {iso(m['created_at'])[:16]}  {m['title']}"
        if show_project:
            line += f"  [{m['project_id']}]"
        rows.append(line)
    return "\n".join(rows)


# -- commands ---------------------------------------------------------------

def cmd_send(a, store: Store) -> int:
    if a.from_agent == a.to:
        raise BridgeError("来源和目标不能是同一个 Agent")
    intent = router.detect(a.user_command, current_agent=a.from_agent)
    override = False
    if not intent or intent.action != "send":
        if not a.user_confirmed:
            raise BridgeError(
                f"「{a.user_command}」不是明确的同步指令，已拒绝发送（普通聊天不会创建同步消息）。"
                "如果用户确实要求同步，请原样传入用户的同步指令；路由误判时可加 --user-confirmed。")
        override = True
    elif intent.target != a.to or intent.mode != a.mode:
        if not a.user_confirmed:
            raise BridgeError(
                f"用户指令解析为 → {intent.target} / {intent.mode}，与参数 --to {a.to} --mode {a.mode} 不一致")
        override = True

    raw = open(a.file, encoding="utf-8").read() if a.file and a.file != "-" else sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError as e:
        raise BridgeError(f"同步内容不是合法 JSON: {e}")
    ctx = render.validate(payload, mode=a.mode, target=a.to)
    proj = project.resolve(a.cwd)
    sess = a.session or env_session(a.from_agent)
    msg = store.create(
        project_id=proj.id, project_root=proj.root, source=a.from_agent, target=a.to, mode=a.mode,
        title=ctx["title"], user_command=a.user_command, body_md="", context=ctx,
        source_session=sess, git_branch=proj.branch, git_head=proj.head, worktree=proj.worktree,
        allow_duplicate=a.allow_duplicate,
    )
    if not msg["duplicate"]:
        body = render.render(ctx, mid=msg["id"], source=a.from_agent, target=a.to, mode=a.mode,
                             project_id=proj.id, user_command=a.user_command,
                             created_at=msg["created_at"], branch=proj.branch, head=proj.head)
        store.db.execute("UPDATE messages SET body_md=? WHERE id=?", (body, msg["id"]))
        if override:
            store._event(msg["id"], "ROUTER_OVERRIDE", a.from_agent, sess, "sender asserted explicit user request")
        msg = store.get(msg["id"]) | {"duplicate": False}
    t = AGENT_LABELS[a.to]
    if msg["duplicate"]:
        text = (f"⚠️ 相同内容的消息已存在：#{msg['id']}（{msg['status']}），未重复写入。"
                f"\n若确需再次发送，加 --allow-duplicate。")
    else:
        text = (f"✅ 已写入 {t} 的待接收队列：#{msg['id']}「{msg['title']}」\n"
                f"状态：{STATUS_ZH[msg['status']]}\n"
                f"项目：{proj.id}\n"
                f"{t} 会在下一次对话时通过 hook 自动加载；对方确认后状态变为 ACKNOWLEDGED。"
                f"\n查询：asb status {msg['id']}")
    emit(brief(msg) | {"duplicate": msg["duplicate"]}, a.json, text)
    return 0


def cmd_inbox(a, store: Store) -> int:
    pid = None if a.all_projects else project.resolve(a.cwd).id
    agent = None if a.agent == "all" else a.agent
    msgs = store.inbox(agent, pid)
    who = AGENT_LABELS.get(a.agent, "所有 Agent")
    text = f"{who} 的待接收消息（项目：{pid or '全部'}）：\n" + _table(msgs, a.all_projects)
    if msgs and agent:
        text += f"\n\n阅读：asb show <ID> --agent {agent}　确认：asb ack <ID> --agent {agent} --summary \"...\""
    emit([brief(m) for m in msgs], a.json, text)
    return 0


def cmd_show(a, store: Store) -> int:
    m = store.get(a.id)
    if a.agent:
        if a.agent != m["target_agent"]:
            raise BridgeError(f"消息 #{a.id} 的接收方是 {m['target_agent']}")
        if m["status"] in (PENDING, DELIVERED):
            store.deliver(a.id, a.agent, session_key(a.agent, a.session), "cli")
            m = store.get(a.id)
    tail = ""
    if a.agent and m["status"] == DELIVERED:
        tail = (f"\n---\n状态：{m['status']}。读完后请确认："
                f"asb ack {a.id} --agent {a.agent} --summary \"<一句话复述>\"")
    emit(brief(m) | {"body_md": m["body_md"], "context": json.loads(m["context_json"])}, a.json, m["body_md"] + tail)
    return 0


def cmd_ack(a, store: Store) -> int:
    m = store.ack(a.id, a.agent, a.summary, session_key(a.agent, a.session))
    text = (f"消息 #{a.id} 早已确认（{iso(m['acked_at'])}），未重复处理。" if m["already"]
            else f"✅ 已确认 #{a.id}「{m['title']}」→ ACKNOWLEDGED\n复述：{m['ack_summary']}")
    emit(brief(m) | {"already": m["already"]}, a.json, text)
    return 0


def cmd_fail(a, store: Store) -> int:
    m = store.fail(a.id, a.reason, a.agent)
    emit(brief(m), a.json, f"#{a.id} → FAILED：{a.reason}\n重试：asb retry {a.id}")
    return 0


def cmd_retry(a, store: Store) -> int:
    m = store.retry(a.id)
    emit(brief(m), a.json, f"#{a.id} → PENDING，已重新进入 {AGENT_LABELS[m['target_agent']]} 的待接收队列")
    return 0


def cmd_status(a, store: Store) -> int:
    m = store.get(a.id)
    ev = store.events(a.id)
    lines = [f"#{m['id']}「{m['title']}」 {m['source_agent']} → {m['target_agent']}（{m['mode']}）",
             f"状态：{STATUS_ZH[m['status']]}"]
    if m["ack_summary"]:
        lines.append(f"对方复述：{m['ack_summary']}")
    if m["error"]:
        lines.append(f"错误：{m['error']}")
    lines.append("事件：")
    for e in ev:
        lines.append(f"  {iso(e['ts'])}  {e['event']:<15} {e['agent'] or '-':<7} {(e['detail'] or '')[:80]}")
    emit(brief(m) | {"events": [dict(e, ts=iso(e["ts"])) for e in ev]}, a.json, "\n".join(lines))
    return 0


def cmd_history(a, store: Store) -> int:
    pid = None if a.all_projects else project.resolve(a.cwd).id
    msgs = store.history(pid, None if a.agent == "all" else a.agent, a.limit)
    emit([brief(m) for m in msgs], a.json, f"同步历史（项目：{pid or '全部'}，最近 {a.limit} 条）：\n" + _table(msgs, a.all_projects))
    return 0


def cmd_route(a, store: Store) -> int:
    intent = router.detect(a.text, current_agent=a.agent)
    emit(intent.to_dict() if intent else None, True, "")
    return 0


def cmd_project(a, store: Store) -> int:
    if a.action == "link":
        project.save_link(a.path, a.project_id)
        print(f"已关联 {os.path.realpath(a.path)} → {a.project_id}")
    elif a.action == "unlink":
        print("已移除" if project.remove_link(a.path) else "没有该关联")
    else:
        p = project.resolve(a.cwd)
        emit(p.__dict__, a.json, f"项目：{p.id}\n来源：{p.source}\n根目录：{p.root}\n分支：{p.branch}  HEAD：{p.head}")
    return 0


def cmd_purge(a, store: Store) -> int:
    n = store.purge(a.older_than_days)
    print(f"已删除 {n} 条超过 {a.older_than_days} 天的 ACKNOWLEDGED/FAILED 消息")
    return 0


def cmd_doctor(a, store: Store) -> int:
    counts = {r[0]: r[1] for r in store.db.execute("SELECT status, COUNT(*) FROM messages GROUP BY status")}
    p = project.resolve(a.cwd)
    print(f"asb {__version__}\n数据目录：{paths.home()}\n数据库：{store.path}\n当前项目：{p.id}（{p.source}）\n"
          f"消息统计：{counts or '无'}\nCLI：{hooks.asb_cmd()}")
    return 0


def cmd_codex_hook(a, store: Store) -> int:
    from . import codex_status
    try:
        hooks_found = codex_status.bridge_hooks(a.cwd)
    except Exception as e:
        raise BridgeError(f"无法查询 Codex hooks：{e}")
    if not hooks_found:
        emit([], a.json, "Codex 中未发现 Agent Sync Bridge hook（请运行 install.sh）")
        return 1
    trusted = all(h.get("trustStatus") in ("trusted", "managed") and h.get("enabled") for h in hooks_found)
    lines = [f"{h.get('eventName')}  trust={h.get('trustStatus')}  enabled={h.get('enabled')}  {h.get('sourcePath')}"
             for h in hooks_found]
    if not trusted:
        lines.append("\n⚠️ Codex hook 尚未被信任，不会运行。请在 Codex 中信任一次：\n"
                     "  VS Code Codex 面板 → 设置 → Hooks → 找到 asb hook codex → Trust\n"
                     "  或终端运行 codex，输入 /hooks 审核并信任。\n"
                     "未信任前 Codex 仍可通过 agent-sync skill 手动接收（说「查看待接收消息」）。")
    emit(hooks_found, a.json, "\n".join(lines))
    return 0 if trusted else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="asb", description="Agent Sync Bridge — Claude Code ↔ Codex")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, cwd=True):
        p.add_argument("--json", action="store_true")
        if cwd:
            p.add_argument("--cwd", default=None, help="项目目录（默认当前目录）")
        return p

    p = common(sub.add_parser("send", help="发送同步消息（JSON 从 stdin 或 --file 读取）"))
    p.add_argument("--from", dest="from_agent", required=True, choices=AGENTS)
    p.add_argument("--to", required=True, choices=AGENTS)
    p.add_argument("--mode", default="sync", choices=("sync", "handoff"))
    p.add_argument("--user-command", required=True, help="用户触发同步的原话")
    p.add_argument("--user-confirmed", action="store_true", help="路由未识别但用户确实要求同步")
    p.add_argument("--file")
    p.add_argument("--session")
    p.add_argument("--allow-duplicate", action="store_true")
    p.set_defaults(fn=cmd_send)

    p = common(sub.add_parser("inbox", help="待接收消息"))
    p.add_argument("--agent", default="all", choices=AGENTS + ("all",))
    p.add_argument("--all-projects", action="store_true")
    p.set_defaults(fn=cmd_inbox)

    p = common(sub.add_parser("show", help="阅读消息（带 --agent 时记录投递）"), cwd=False)
    p.add_argument("id")
    p.add_argument("--agent", choices=AGENTS)
    p.add_argument("--session")
    p.set_defaults(fn=cmd_show)

    p = common(sub.add_parser("ack", help="接收方确认"), cwd=False)
    p.add_argument("id")
    p.add_argument("--agent", required=True, choices=AGENTS)
    p.add_argument("--summary", required=True, help="接收方对消息的一句话复述")
    p.add_argument("--session")
    p.set_defaults(fn=cmd_ack)

    p = common(sub.add_parser("fail", help="标记失败"), cwd=False)
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p.add_argument("--agent", choices=AGENTS)
    p.set_defaults(fn=cmd_fail)

    p = common(sub.add_parser("retry", help="重试失败消息"), cwd=False)
    p.add_argument("id")
    p.set_defaults(fn=cmd_retry)

    p = common(sub.add_parser("status", help="消息状态与事件"), cwd=False)
    p.add_argument("id")
    p.set_defaults(fn=cmd_status)

    p = common(sub.add_parser("history", help="同步历史"))
    p.add_argument("--agent", default="all", choices=AGENTS + ("all",))
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--all-projects", action="store_true")
    p.set_defaults(fn=cmd_history)

    p = sub.add_parser("route", help="调试：解析一句话的同步意图")
    p.add_argument("text")
    p.add_argument("--agent", choices=AGENTS)
    p.set_defaults(fn=cmd_route)

    p = common(sub.add_parser("project", help="查看/关联项目"))
    p.add_argument("action", nargs="?", default="show", choices=("show", "link", "unlink"))
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("project_id", nargs="?")
    p.set_defaults(fn=cmd_project)

    p = sub.add_parser("purge", help="清理旧消息（保留策略）")
    p.add_argument("--older-than-days", type=float, default=90)
    p.set_defaults(fn=cmd_purge)

    p = common(sub.add_parser("doctor", help="环境检查"))
    p.set_defaults(fn=cmd_doctor)

    p = common(sub.add_parser("codex-hook", help="只读：检查 Codex hook 是否已被信任"))
    p.set_defaults(fn=cmd_codex_hook)

    p = sub.add_parser("hook", help="Agent hook 入口（stdin JSON → additionalContext）")
    p.add_argument("agent", choices=AGENTS)
    p.set_defaults(fn=None)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "hook":
        return hooks.main(a.agent)
    if a.cmd == "project" and a.action == "link" and not a.project_id:
        print("用法：asb project link <path> <project-id>", file=sys.stderr)
        return 2
    store = Store()
    try:
        return a.fn(a, store)
    except BridgeError as e:
        if getattr(a, "json", False):
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
        else:
            print(f"❌ {e}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
