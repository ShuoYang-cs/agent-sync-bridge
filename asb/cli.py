"""asb — Agent Sync Bridge command line."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import AGENT_LABELS, AGENTS, __version__, config, hooks, paths, project, remote, render, router
from .store import (ACKNOWLEDGED, DELIVERED, FAILED, INBOUND, OUTBOX, PENDING, RECEIVED, SENT, BridgeError,
                    Store, is_cross_device, iso)

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
        "branch": m["git_branch"], "from_device": m.get("source_device"), "to_device": m.get("target_device"),
        "transport": m.get("transport"),
    }


def status_text(m: Dict[str, Any], store: Store) -> str:
    """Status in words. For a message sent to another computer, say how far it got."""
    if not is_cross_device(m) or m["target_device"] == store.device:
        text = STATUS_ZH[m["status"]]
        if m.get("transport") == INBOUND:
            text += f"（来自 {store.device_label(m['source_device'])}）"
        return text
    where, agent = store.device_label(m["target_device"]), AGENT_LABELS[m["target_agent"]]
    if m["status"] == PENDING:
        return {OUTBOX: "PENDING · 已写入本机待发队列，尚未上传到同步信箱",
                SENT: f"PENDING · 已上传到同步信箱，{where} 还没有拉取",
                RECEIVED: f"PENDING · {where} 已收到，{agent} 还没有读取"}.get(m["transport"], STATUS_ZH[PENDING])
    if m["status"] == DELIVERED:
        return f"DELIVERED · 已投递进 {where} 上 {agent} 的上下文，等待确认（不代表已理解）"
    if m["status"] == ACKNOWLEDGED:
        return f"ACKNOWLEDGED · {where} 上的 {agent} 已阅读并确认"
    return STATUS_ZH[FAILED]


def _push_receipts(store: Store) -> str:
    """Receipts for the other computer are pushed in the background."""
    if not (store.device and store.pending_push_count()):
        return ""
    try:
        remote.spawn()
    except Exception as e:
        return f"\n回执已写入本机待发队列，后台上传没能启动（{e}），下次同步时补发。"
    return "\n回执正在后台上传到同步信箱。"


def _who(agent: str, device: Optional[str]) -> str:
    return f"{agent}@{device}" if device else agent


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
        arrow = f"{_who(m['source_agent'], m.get('source_device'))}→{_who(m['target_agent'], m.get('target_device'))}"
        mode = "交接" if m["mode"] == "handoff" else "同步"
        line = f"#{m['id']}  {arrow:<13} {mode}  {m['status']:<12} {iso(m['created_at'])[:16]}  {m['title']}"
        if show_project:
            line += f"  [{m['project_id']}]"
        rows.append(line)
    return "\n".join(rows)


# -- commands ---------------------------------------------------------------

def _project_slug(proj: project.Project) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", proj.name.lower()).strip("-") or "my-project"


def cmd_send(a, store: Store) -> int:
    device = a.device if a.device and a.device != store.device else None
    if a.from_agent == a.to and not device:
        raise BridgeError("来源和目标不能是同一个 Agent")
    if device:
        remote.require_config()
    peers = store.devices() if store.device else []
    here = {"id": store.device, "label": store.device_label(store.device),
            "platform": config.platform_name()} if store.device else None
    intent = router.detect(a.user_command, current_agent=a.from_agent, devices=peers, local=here)
    override = False
    if not intent or intent.action != "send":
        if not a.user_confirmed:
            raise BridgeError(
                f"「{a.user_command}」不是明确的同步指令，已拒绝发送（普通聊天不会创建同步消息）。"
                "如果用户确实要求同步，请原样传入用户的同步指令；路由误判时可加 --user-confirmed。")
        override = True
    elif intent.target != a.to or intent.mode != a.mode or intent.device != device:
        if not a.user_confirmed:
            got = f"{intent.target}@{intent.device_word or intent.device}" if intent.device_word else intent.target
            raise BridgeError(
                f"用户指令解析为 → {got} / {intent.mode}，与参数 --to {a.to} --device {device or '本机'} "
                f"--mode {a.mode} 不一致")
        override = True

    proj = project.resolve(a.cwd)
    if device:
        known = {d["id"]: d for d in peers}
        if device not in known:
            raise BridgeError(f"同步信箱里还没有设备 `{device}`。已知设备：{', '.join(known) or '无'}。"
                              "另一台电脑先运行 asb remote setup；已设置过就先运行 asb remote sync")
        if not proj.stable:
            raise BridgeError(
                f"当前项目 ID `{proj.id}` 是本机路径，另一台电脑对应不上。先在两台电脑上把项目关联到同一个 ID："
                f"`asb project link \"{proj.root}\" {_project_slug(proj)}`（另一台电脑用它自己的路径），然后重新发送")

    raw = (open(a.file, encoding="utf-8-sig").read() if a.file and a.file != "-"
           else sys.stdin.buffer.read().decode("utf-8-sig"))
    try:
        payload = json.loads(raw)
    except ValueError as e:
        raise BridgeError(f"同步内容不是合法 JSON: {e}")
    ctx = render.validate(payload, mode=a.mode, target=a.to)
    sess = a.session or env_session(a.from_agent)
    msg = store.create(
        project_id=proj.id, project_root=proj.root, source=a.from_agent, target=a.to, mode=a.mode,
        title=ctx["title"], user_command=a.user_command, body_md="", context=ctx,
        source_session=sess, git_branch=proj.branch, git_head=proj.head, worktree=proj.worktree,
        allow_duplicate=a.allow_duplicate, target_device=device, project_name=proj.name, git_remote=proj.remote,
    )
    if not msg["duplicate"]:
        labels = dict(source_device=store.device_label(store.device), target_device=store.device_label(device)) if device else {}
        body = render.render(ctx, mid=msg["id"], source=a.from_agent, target=a.to, mode=a.mode,
                             project_id=proj.id, user_command=a.user_command,
                             created_at=msg["created_at"], branch=proj.branch, head=proj.head, **labels)
        store.db.execute("UPDATE messages SET body_md=? WHERE id=?", (body, msg["id"]))
        if override:
            store._event(msg["id"], "ROUTER_OVERRIDE", a.from_agent, sess, "sender asserted explicit user request")
        msg = store.get(msg["id"]) | {"duplicate": False}
    t = AGENT_LABELS[a.to]
    extra: Dict[str, Any] = {}
    if device and msg["transport"] == OUTBOX and not a.no_push:
        err = None
        try:
            remote.sync(store, timeout=a.timeout)
        except BridgeError as e:
            err = str(e)
        msg = store.get(msg["id"]) | {"duplicate": msg["duplicate"]}
        extra = {"push_error": err}
    if msg["duplicate"]:
        text = (f"⚠️ 相同内容的消息已存在：#{msg['id']}（{status_text(msg, store)}），未重复写入。"
                f"\n若确需再次发送，加 --allow-duplicate。")
    elif device:
        where = store.device_label(device)
        err = extra.get("push_error")
        head = (f"✅ 已上传到同步信箱：#{msg['id']}「{msg['title']}」→ {where} 上的 {t}" if msg["transport"] != OUTBOX
                else f"⚠️ 已写入本机待发队列：#{msg['id']}「{msg['title']}」→ {where} 上的 {t}，但还没有上传")
        text = (f"{head}\n状态：{status_text(msg, store)}\n项目：{proj.id}\n"
                + (f"上传失败：{err}\n联网后，下一次对话或 `asb remote sync` 会自动补发。\n" if err else "")
                + f"{where} 上的 {t} 在下一次对话时自动拉取并加载；对方确认后状态变为 ACKNOWLEDGED。"
                f"\n查询：asb status {msg['id']}")
    else:
        text = (f"✅ 已写入 {t} 的待接收队列：#{msg['id']}「{msg['title']}」\n"
                f"状态：{STATUS_ZH[msg['status']]}\n"
                f"项目：{proj.id}\n"
                f"{t} 会在下一次对话时通过 hook 自动加载；对方确认后状态变为 ACKNOWLEDGED。"
                f"\n查询：asb status {msg['id']}")
    emit(brief(msg) | {"duplicate": msg["duplicate"]} | extra, a.json, text)
    return 0


def cmd_inbox(a, store: Store) -> int:
    proj = None if a.all_projects else project.resolve(a.cwd)
    pid = proj.id if proj else None
    agent = None if a.agent == "all" else a.agent
    msgs = store.inbox(agent, pid, proj.remote if proj else None)
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
        if a.agent:
            _push_receipts(store)
    emit(brief(m) | {"body_md": m["body_md"], "context": json.loads(m["context_json"])}, a.json, m["body_md"] + tail)
    return 0


def cmd_ack(a, store: Store) -> int:
    m = store.ack(a.id, a.agent, a.summary, session_key(a.agent, a.session))
    text = (f"消息 #{a.id} 早已确认（{iso(m['acked_at'])}），未重复处理。" if m["already"]
            else f"✅ 已确认 #{a.id}「{m['title']}」→ ACKNOWLEDGED\n复述：{m['ack_summary']}" + _push_receipts(store))
    emit(brief(m) | {"already": m["already"]}, a.json, text)
    return 0


def cmd_fail(a, store: Store) -> int:
    m = store.fail(a.id, a.reason, a.agent)
    emit(brief(m), a.json, f"#{a.id} → FAILED：{a.reason}\n重试：asb retry {a.id}" + _push_receipts(store))
    return 0


def cmd_retry(a, store: Store) -> int:
    m = store.retry(a.id)
    where = f"{store.device_label(m['target_device'])} 上 " if m.get("target_device") and m["target_device"] != store.device else ""
    emit(brief(m), a.json, f"#{a.id} → PENDING，已重新进入 {where}{AGENT_LABELS[m['target_agent']]} 的待接收队列"
         + _push_receipts(store))
    return 0


def cmd_status(a, store: Store) -> int:
    m = store.get(a.id)
    ev = store.events(a.id)
    lines = [f"#{m['id']}「{m['title']}」 {_who(m['source_agent'], m.get('source_device'))} → "
             f"{_who(m['target_agent'], m.get('target_device'))}（{m['mode']}）",
             f"状态：{status_text(m, store)}"]
    if m["ack_summary"]:
        lines.append(f"对方复述：{m['ack_summary']}")
    if m["error"]:
        lines.append(f"错误：{m['error']}")
    lines.append("事件：")
    for e in ev:
        who = (e["agent"] or "-") + (f"@{e['device']}" if e.get("device") else "")
        lines.append(f"  {iso(e['ts'])}  {e['event']:<15} {who:<12} {(e['detail'] or '')[:80]}")
    emit(brief(m) | {"status_text": status_text(m, store), "events": [dict(e, ts=iso(e["ts"])) for e in ev]},
         a.json, "\n".join(lines))
    return 0


def cmd_history(a, store: Store) -> int:
    pid = None if a.all_projects else project.resolve(a.cwd).id
    msgs = store.history(pid, None if a.agent == "all" else a.agent, a.limit)
    emit([brief(m) for m in msgs], a.json, f"同步历史（项目：{pid or '全部'}，最近 {a.limit} 条）：\n" + _table(msgs, a.all_projects))
    return 0


def cmd_route(a, store: Store) -> int:
    cfg = config.load()
    here = {"id": cfg["device"], "label": cfg.get("label"), "platform": cfg.get("platform")} if cfg else None
    intent = router.detect(a.text, current_agent=a.agent, devices=store.devices() if cfg else [], local=here)
    emit(intent.to_dict() if intent else None, True, "")
    return 0


def cmd_project(a, store: Store) -> int:
    if a.action == "link":
        before = project.resolve(a.path)
        project.save_link(a.path, a.project_id)
        print(f"已关联 {os.path.realpath(a.path)} → {a.project_id}")
        if not before.stable and before.id != a.project_id:
            n = store.rekey_project(before.id, a.project_id)
            if n:
                print(f"原项目 ID `{before.id}` 下的 {n} 条消息已改到 `{a.project_id}`")
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
    cfg = config.load()
    print(f"asb {__version__}\n数据目录：{paths.home()}\n数据库：{store.path}\n当前项目：{p.id}（{p.source}）\n"
          f"消息统计：{counts or '无'}\nCLI：{paths.cli_command()}\n"
          f"跨电脑同步：{'本机 ' + cfg['device'] + '，信箱 ' + cfg['url'] if cfg else '未配置（asb remote setup）'}")
    return 0


# -- cross-device -------------------------------------------------------------

def _remote_setup(a, store: Store) -> int:
    out = remote.setup(store, a.url, a.device, label=a.label, pair_code=a.pair_code, reuse=a.reuse_device)
    cfg = out["config"]
    lines = [f"✅ 本机已加入同步信箱：{cfg['url']}", f"设备：{cfg['device']}（{cfg['label']}，{cfg['platform']}）"]
    lines.append("信箱里的其他设备：" + ("、".join(out["peers"]) if out["peers"] else "暂无"))
    if out.get("sync_error"):
        lines.append(f"⚠️ 首次同步失败：{out['sync_error']}\n修好网络或权限后运行 asb remote sync")
    if out["generated"]:
        if sys.stdout.isatty():
            lines.append(f"\n配对码：{cfg['pair_code']}\n（相当于密码：只在另一台电脑上输入一次，不要发到聊天里）")
        else:
            lines.append("\n已生成配对码。它相当于密码，所以不在这里显示；请在你自己的终端里运行 `asb remote pair-code` 查看。")
        lines.append(f"\n另一台电脑上：安装 Agent Sync Bridge 后运行\n  asb remote setup {cfg['url']} --device <那台电脑的名字，例如 win> --pair-code <配对码>")
    emit({k: v for k, v in cfg.items() if k != "pair_code"} | {"peers": out["peers"], "sync_error": out.get("sync_error")},
         a.json, "\n".join(lines))
    return 0


def _remote_sync(a, store: Store) -> int:
    if a.background:
        try:
            remote.sync(store, timeout=a.timeout)
        except Exception as e:
            hooks._log(f"background sync failed: {e}")
            return 1
        return 0
    r = remote.sync(store, timeout=a.timeout)
    lines = [f"✅ 同步完成：上传 {len(r['uploaded'])} 条消息、{r['receipts_out']} 条回执；"
             f"收到 {len(r['received'])} 条新消息、{len(r['receipts'])} 条回执。"]
    lines += [f"  收到回执：{x}" for x in r["receipts"]]
    if r["received"]:
        lines.append("  新消息：" + "、".join("#" + x for x in r["received"]) + "（对应 Agent 下一次对话时加载）")
    if r["rejected"]:
        lines.append("⚠️ 已拒收：")
        lines += [f"  {x}" for x in r["rejected"]]
    emit(r, a.json, "\n".join(lines))
    return 0


def _remote_status(a, store: Store) -> int:
    st = remote.status(store)
    if not st["configured"]:
        emit(st, a.json, "跨电脑同步未配置。运行：asb remote setup <Git 仓库地址> --device <本机名>")
        return 0
    peers = "、".join(f"{d['id']}（{d['label']}，{d['platform']}）" for d in st["peers"]) or "暂无（另一台电脑还没加入，或还没同步）"
    lines = [f"本机：{st['device']}（{st['label']}）", f"信箱：{st['url']}（{st['branch']}）", f"其他设备：{peers}",
             f"待上传：{st['pending_push']} 条　发给本机、尚未确认：{st['inbound_open']} 条",
             f"上次成功同步：{st['last_ok'] or '从未'}　上次尝试：{st['last_attempt'] or '从未'}",
             f"对话时自动同步：{'开' if st['hook_sync'] else '关'}"]
    if st["last_error"]:
        lines.append(f"⚠️ 上次同步失败（连续 {st['failures']} 次）：{st['last_error']}")
    if st["last_rejected"]:
        lines.append("⚠️ 上次同步拒收：")
        lines += [f"  {x}" for x in st["last_rejected"]]
    emit(st, a.json, "\n".join(lines))
    return 0


def _remote_devices(a, store: Store) -> int:
    cfg = remote.require_config()
    rows = [{"id": cfg["device"], "label": cfg.get("label"), "platform": cfg.get("platform"), "self": True}]
    rows += [dict(d, last_seen=iso(d["last_seen"]), self=False) for d in store.devices() if d["id"] != cfg["device"]]
    emit(rows, a.json, "\n".join(f"{'*' if r['self'] else ' '} {r['id']:<12} {r['label'] or '':<12} {r['platform'] or ''}"
                                  for r in rows))
    return 0


def _remote_pair_code(a, store: Store) -> int:
    cfg = remote.require_config()
    if not (a.show or sys.stdout.isatty()):
        raise BridgeError("配对码相当于密码，只在终端里显示。请在你自己的终端运行 asb remote pair-code"
                          "（用户明确要求时可加 --show）")
    print(cfg["pair_code"])
    return 0


def _remote_disable(a, store: Store) -> int:
    remote.disable()
    print("已关闭跨电脑同步：删除了本机的信箱克隆和配对码，消息记录保留。远端仓库没有改动。")
    return 0


def cmd_remote(a, store: Store) -> int:
    return {"setup": _remote_setup, "sync": _remote_sync, "status": _remote_status, "devices": _remote_devices,
            "pair-code": _remote_pair_code, "disable": _remote_disable}[a.remote_cmd](a, store)


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
    p.add_argument("--device", help="目标 Agent 所在的电脑（设备 ID，见 asb remote devices）；默认本机")
    p.add_argument("--no-push", action="store_true", help="跨电脑消息只写入本机队列，不立即上传")
    p.add_argument("--timeout", type=float, default=25, help="上传超时秒数")
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

    p = sub.add_parser("remote", help="跨电脑同步（Git 信箱）")
    rs = p.add_subparsers(dest="remote_cmd", required=True)
    q = common(rs.add_parser("setup", help="加入或新建同步信箱"), cwd=False)
    q.add_argument("url", help="Git 仓库地址：GitHub 私有仓库，或局域网另一台电脑上的仓库（SSH）")
    q.add_argument("--device", required=True, help="本机名，小写字母/数字/连字符，例如 mac、win")
    q.add_argument("--label", help="显示名，默认 Mac / Windows / Linux")
    q.add_argument("--pair-code", help="另一台电脑上 asb remote pair-code 显示的配对码")
    q.add_argument("--reuse-device", action="store_true", help="本机重装后沿用原来的设备 ID")
    q = common(rs.add_parser("sync", help="立即同步"), cwd=False)
    q.add_argument("--timeout", type=float, default=60)
    q.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    common(rs.add_parser("status", help="同步状态"), cwd=False)
    common(rs.add_parser("devices", help="信箱里的设备"), cwd=False)
    q = common(rs.add_parser("pair-code", help="显示配对码（只在终端里显示）"), cwd=False)
    q.add_argument("--show", action="store_true", help="非终端环境也显示")
    common(rs.add_parser("disable", help="关闭跨电脑同步（保留消息记录）"), cwd=False)
    p.set_defaults(fn=cmd_remote)

    p = sub.add_parser("hook", help="Agent hook 入口（stdin JSON → additionalContext）")
    p.add_argument("agent", choices=AGENTS)
    p.set_defaults(fn=None)
    return ap


def _utf8_stdio() -> None:
    """Windows consoles default to a legacy code page; agents read UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _utf8_stdio()
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
