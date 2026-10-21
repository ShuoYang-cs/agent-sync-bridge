"""Context Packager contract: validate the sender's structured payload and
render the Markdown the receiving agent reads."""
from __future__ import annotations

from typing import Any, Dict, List

from . import AGENT_LABELS
from .store import BridgeError, iso

ITEM_TYPES = (
    "CONFIRMED_DECISION",
    "PROPOSAL",
    "IMPLEMENTED",
    "VERIFIED",
    "UNVERIFIED",
    "PENDING_TASK",
)

ITEM_HEADINGS = {
    "CONFIRMED_DECISION": "已确认决定 CONFIRMED_DECISION（用户已拍板）",
    "PROPOSAL": "建议 / 方案 PROPOSAL（尚未确认）",
    "IMPLEMENTED": "已实现 IMPLEMENTED（代码已改，未必已验证）",
    "VERIFIED": "已验证 VERIFIED（有测试或实际运行证据）",
    "UNVERIFIED": "未验证 UNVERIFIED（假设 / 推测，接收方需自行核实）",
    "PENDING_TASK": "待办 PENDING_TASK",
}

ALLOWED_KEYS = {
    "title", "summary", "scope", "items", "files_changed", "tests", "open_questions",
    "next_steps_for_receiver", "handoff", "references",
}


def _str_list(v: Any, field: str) -> List[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise BridgeError(f"`{field}` 必须是字符串数组")
    return [x.strip() for x in v if x.strip()]


def validate(payload: Dict[str, Any], *, mode: str, target: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise BridgeError("同步内容必须是 JSON 对象")
    unknown = set(payload) - ALLOWED_KEYS
    if unknown:
        raise BridgeError(f"未知字段: {sorted(unknown)}；允许: {sorted(ALLOWED_KEYS)}")
    for f in ("title", "summary"):
        if not isinstance(payload.get(f), str) or not payload[f].strip():
            raise BridgeError(f"缺少必填字段 `{f}`")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise BridgeError("`items` 至少需要一条分类条目")
    clean_items = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not isinstance(it.get("text"), str) or not it["text"].strip():
            raise BridgeError(f"items[{i}] 需要 `type` 和非空 `text`")
        t = str(it.get("type", "")).upper()
        if t not in ITEM_TYPES:
            raise BridgeError(f"items[{i}].type={it.get('type')!r} 无效；必须是 {', '.join(ITEM_TYPES)}")
        ev = (it.get("evidence") or "").strip() if isinstance(it.get("evidence"), str) else ""
        if t == "VERIFIED" and not ev:
            raise BridgeError(f"items[{i}] 标记为 VERIFIED 但没有 `evidence`（测试命令/运行结果）；无证据请改为 IMPLEMENTED 或 UNVERIFIED")
        clean_items.append({"type": t, "text": it["text"].strip(), **({"evidence": ev} if ev else {})})

    out: Dict[str, Any] = {
        "title": payload["title"].strip()[:120],
        "summary": payload["summary"].strip(),
        "items": clean_items,
    }
    scope = payload.get("scope")
    if scope is not None:
        if not isinstance(scope, dict):
            raise BridgeError("`scope` 必须是对象 {topic, included, excluded}")
        out["scope"] = {
            "topic": str(scope.get("topic") or "").strip(),
            "included": str(scope.get("included") or "").strip(),
            "excluded": _str_list(scope.get("excluded"), "scope.excluded"),
        }

    files = []
    for f in payload.get("files_changed") or []:
        if isinstance(f, str):
            files.append({"path": f})
        elif isinstance(f, dict) and isinstance(f.get("path"), str):
            files.append({"path": f["path"], **({"change": str(f["change"])} if f.get("change") else {})})
        else:
            raise BridgeError("`files_changed` 元素必须是路径字符串或 {path, change}")
    if files:
        out["files_changed"] = files

    tests = []
    for t in payload.get("tests") or []:
        if not isinstance(t, dict) or not t.get("command") or not t.get("result"):
            raise BridgeError("`tests` 元素必须是 {command, result, passed?}")
        tests.append({"command": str(t["command"]), "result": str(t["result"]),
                      **({"passed": bool(t["passed"])} if "passed" in t else {})})
    if tests:
        out["tests"] = tests

    for f in ("open_questions", "next_steps_for_receiver", "references"):
        v = _str_list(payload.get(f), f)
        if v:
            out[f] = v

    handoff = payload.get("handoff")
    if mode == "handoff":
        if not isinstance(handoff, dict):
            raise BridgeError("交接模式必须提供 `handoff` {owner, goal, acceptance_criteria}")
        owner = str(handoff.get("owner") or "").lower()
        if owner != target:
            raise BridgeError(f"handoff.owner 必须是接收方 `{target}`")
        if not str(handoff.get("goal") or "").strip():
            raise BridgeError("handoff.goal 不能为空")
        ac = _str_list(handoff.get("acceptance_criteria"), "handoff.acceptance_criteria")
        if not ac:
            raise BridgeError("handoff.acceptance_criteria 至少一条")
        out["handoff"] = {"owner": owner, "goal": str(handoff["goal"]).strip(), "acceptance_criteria": ac,
                          "constraints": _str_list(handoff.get("constraints"), "handoff.constraints")}
    elif handoff:
        raise BridgeError("普通同步不能包含 `handoff`；需要交接请使用“同步并交接”(--mode handoff)")
    return out


def render(ctx: Dict[str, Any], *, mid: str, source: str, target: str, mode: str,
           project_id: str, user_command: str, created_at: float,
           branch: str = None, head: str = None,
           source_device: str = None, target_device: str = None) -> str:
    """`source_device` / `target_device` are device labels, set only for cross-device messages."""
    kind = "同步并交接" if mode == "handoff" else "同步（仅上下文）"
    src = AGENT_LABELS[source] + (f"（{source_device}）" if source_device else "")
    dst = AGENT_LABELS[target] + (f"（{target_device}）" if target_device else "")
    lines = [
        f"# 📨 Agent Sync #{mid}：{ctx['title']}",
        "",
        f"- 方向：{src} → {dst}　类型：{kind}",
        f"- 项目：`{project_id}`" + (f"　分支：`{branch}`" if branch else "") + (f"　HEAD：`{head}`" if head else ""),
        f"- 时间：{iso(created_at)}",
        f"- 用户指令：「{user_command}」",
    ]
    sc = ctx.get("scope")
    if sc:
        if sc.get("topic"):
            lines.append(f"- 范围：{sc['topic']}" + (f"（{sc['included']}）" if sc.get("included") else ""))
        if sc.get("excluded"):
            lines.append("- 明确排除：" + "；".join(sc["excluded"]))
    lines += ["", "## 摘要", ctx["summary"]]

    for t in ITEM_TYPES:
        group = [it for it in ctx["items"] if it["type"] == t]
        if not group:
            continue
        lines += ["", f"## {ITEM_HEADINGS[t]}"]
        for it in group:
            lines.append(f"- {it['text']}" + (f"\n  - 证据：{it['evidence']}" if it.get("evidence") else ""))

    if ctx.get("files_changed"):
        lines += ["", "## 修改的文件"]
        lines += [f"- `{f['path']}`" + (f" — {f['change']}" if f.get("change") else "") for f in ctx["files_changed"]]
    if ctx.get("tests"):
        lines += ["", "## 测试结果"]
        for t in ctx["tests"]:
            mark = "" if "passed" not in t else ("✅ " if t["passed"] else "❌ ")
            lines.append(f"- {mark}`{t['command']}` → {t['result']}")
    if ctx.get("open_questions"):
        lines += ["", "## 未解决问题"] + [f"- {q}" for q in ctx["open_questions"]]
    if ctx.get("next_steps_for_receiver"):
        lines += ["", f"## {AGENT_LABELS[target]} 需要知道 / 下一步"] + [f"- {q}" for q in ctx["next_steps_for_receiver"]]
    if ctx.get("handoff"):
        h = ctx["handoff"]
        lines += ["", "## 交接", f"- 下一阶段负责人：{AGENT_LABELS[h['owner']]}", f"- 任务目标：{h['goal']}", "- 验收条件："]
        lines += [f"  - [ ] {a}" for a in h["acceptance_criteria"]]
        if h.get("constraints"):
            lines += ["- 约束："] + [f"  - {c}" for c in h["constraints"]]
    if ctx.get("references"):
        lines += ["", "## 参考"] + [f"- {r}" for r in ctx["references"]]
    return "\n".join(lines) + "\n"
