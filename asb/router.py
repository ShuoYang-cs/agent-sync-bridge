"""Command Router: deterministic detection of explicit sync commands.

Only short, imperative commands count. Questions about syncing, negated
requests, quoted mentions and long pasted documents never produce a send
intent, so ordinary chat cannot create sync messages.

A target may name a computer: "同步给 Windows 上的 Claude", "sync to codex on
mac", "同步给另一台电脑". Device words only count inside the target phrase, so
"把 Windows 版的测试结果同步给 Codex" still goes to the local Codex.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import other_agent

MAX_COMMAND_CHARS = 500

AGENT = r"(?P<agent>claude(?:\s*code)?|codex|克劳德)"
TO = r"\s*(?:给|到|至|to)\s*"

# (pattern, kind, platform). kind "platform" picks the device with that
# platform; "other" picks the only other computer.
GENERIC_DEVICES: Tuple[Tuple[str, str, Optional[str]], ...] = (
    (r"windows|win(?![a-z])|pc(?![a-z])", "platform", "windows"),
    (r"mac\s*mini|macbook(?:\s*(?:pro|air))?|imac|mac(?![a-z])|苹果电脑", "platform", "macos"),
    (r"linux|ubuntu", "platform", "linux"),
    (r"另一台(?:电脑|机器|设备)?|另外一台(?:电脑|机器|设备)?|那台(?:电脑|机器)|其他电脑|别的电脑|"
     r"other\s+(?:computer|machine|device|pc)", "other", None),
)
DEV_SUFFIX = r"(?:'s|’s)?(?:\s*(?:电脑|机器|设备|端|那边))?(?:\s*(?:上面|上|里))?\s*(?:的)?\s*"
END = r"\s*(?:[，,。.!！~]|$)"


@dataclass
class Intent:
    action: str                     # send | inbox | history
    target: Optional[str] = None
    mode: Optional[str] = None      # sync | handoff
    scope: Optional[str] = None     # raw scope phrase from the user
    topic: Optional[str] = None     # "关于 X 的" -> X
    scope_kind: Optional[str] = None  # discussion | code_changes | conclusions | unspecified
    strict: bool = False            # "只同步" -> exclude everything else
    command: str = ""
    device: Optional[str] = None    # target computer id; None = this computer
    device_word: Optional[str] = None   # how the user named the computer
    device_status: str = "local"    # local | remote | unresolved | ambiguous
    agent_defaulted: bool = False   # "同步给 Windows" without naming an agent

    def to_dict(self):
        return asdict(self)


@lru_cache(maxsize=16)
def _patterns(extra: Tuple[str, ...]):
    words = [re.escape(w) for w in sorted(extra, key=len, reverse=True)] + [p for p, _, _ in GENERIC_DEVICES]
    dev = "(?:" + "|".join(words) + ")"
    target = (rf"(?:(?:the\s+)?(?P<dev>{dev}){DEV_SUFFIX})?{AGENT}"
              rf"(?:\s*(?:on|@|in)\s*(?:the\s+)?(?P<dev2>{dev})|\s*[（(]\s*(?P<dev3>{dev})\s*[)）])?")
    devonly = rf"(?:the\s+)?(?P<dev>{dev})(?:\s*(?:电脑|机器|设备|端|那边|computer|machine|side))?(?:\s*(?:上面|上|里))?{END}"
    handoff = [
        re.compile(r"同步\s*(?:并|和|且|然后)?\s*交接" + TO + target, re.I),
        re.compile(r"(?<!同步)交接" + TO + target, re.I),
        re.compile(r"\b(?:sync\s+and\s+)?hand\s*-?\s*off\b(?P<scope>.*?)\bto\s+" + target, re.I),
    ]
    sync = [
        re.compile(r"把(?P<scope>.+?)同步" + TO + target, re.I),
        re.compile(r"把(?P<scope>.+?)(?:交|发|传)给\s*" + target, re.I),
        re.compile(r"同步(?P<scope>[^，,。.!！?？]{0,60}?)" + TO + target, re.I),
        re.compile(r"\bsync\b(?P<scope>.*?)\bto\s+" + target, re.I),
    ]
    handoff_dev = [
        re.compile(r"同步\s*(?:并|和|且|然后)?\s*交接" + TO + devonly, re.I),
        re.compile(r"(?<!同步)交接" + TO + devonly, re.I),
        re.compile(r"\b(?:sync\s+and\s+)?hand\s*-?\s*off\b(?P<scope>.*?)\bto\s+" + devonly, re.I),
    ]
    sync_dev = [
        re.compile(r"把(?P<scope>.+?)同步" + TO + devonly, re.I),
        re.compile(r"同步(?P<scope>[^，,。.!！?？]{0,60}?)" + TO + devonly, re.I),
        re.compile(r"\bsync\b(?P<scope>.*?)\bto\s+" + devonly, re.I),
    ]
    return (("handoff", handoff), ("sync", sync)), (("handoff", handoff_dev), ("sync", sync_dev))


# "只同步刚才关于 X 的结论" — no target named; target = the other agent.
_SYNC_NO_TARGET = re.compile(
    r"^(?:请|麻烦)?\s*(?P<only>只|仅|只把|仅把)?\s*同步(?P<scope>.+?)(?:的)?(?:讨论|结论|内容|修改|改动|上下文|决定|进展)",
)
_INBOX = re.compile(
    r"(查看|看看|检查|读取|接收)?\s*(待接收|未接收|待处理的同步|收件箱)|接收同步(消息)?|"
    r"\b(check\s+(sync\s+)?inbox|pending\s+(sync\s+)?messages|show\s+inbox)\b", re.I)
_HISTORY = re.compile(r"同步(历史|记录|状态)|\bsync\s+(history|status|log)\b", re.I)

_QUESTION = re.compile(r"怎么|如何|为什么|为何|原理|是什么|是不是|会不会|有没有|\bhow\b|\bwhy\b|\bwhat\b", re.I)
_NEGATION = re.compile(r"(不要|不用|别|先别|无需|不需要|暂时不|don'?t|do\s+not|no\s+need)\s*(把|再)?\s*\S{0,30}?(同步|交接|交给|发给|传给|sync|hand)", re.I)
_KEYWORD = re.compile(r"同步|交接|交给|发给|传给|\bsync\b|hand\s*-?\s*off", re.I)
_QUOTED = re.compile(r"[「“\"'`](?:[^」”\"'`]{0,80})(同步|交接|sync|hand\s*off)(?:[^」”\"'`]{0,80})[」”\"'`]", re.I)


def _agent(name: str) -> str:
    return "codex" if name.lower().startswith("codex") else "claude"


def _clean_scope(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.strip(" \t，,。：:的")
    s = re.sub(r"^(只|仅|一下|下)\s*", "", s)
    return s or None


def _topic(scope: Optional[str]) -> Optional[str]:
    if not scope:
        return None
    m = re.search(r"关于\s*(?P<t>.+?)\s*(?:的|$)", scope)
    if m:
        return m.group("t").strip() or None
    m = re.search(r"\babout\s+(?P<t>.+)$", scope, re.I)
    return m.group("t").strip() if m else None


def _scope_kind(scope: Optional[str], text: str) -> str:
    probe = (scope or "") + " " + text
    if re.search(r"代码修改|改动|修改|实现|code\s*change|changes|diff", probe, re.I):
        return "code_changes"
    if re.search(r"结论|决定|conclusion|decision", probe, re.I):
        return "conclusions"
    if re.search(r"讨论|对话|discussion|conversation", probe, re.I):
        return "discussion"
    return "unspecified"


def resolve_device(word: str, devices: Sequence[Dict[str, Any]],
                   local: Optional[Dict[str, Any]]) -> Tuple[Optional[str], str]:
    """Map what the user said to a device id. Returns (remote id or None, status)."""
    w = re.sub(r"\s+", " ", word.strip()).lower()
    here = local["id"] if local else None
    for d in list(devices) + ([local] if local else []):
        if w in {d["id"].lower(), (d.get("label") or "").lower()}:
            return (None, "local") if d["id"] == here else (d["id"], "remote")
    for pat, kind, plat in GENERIC_DEVICES:
        if not re.fullmatch(pat, w, re.I):
            continue
        peers = [d["id"] for d in devices if d["id"] != here and (kind == "other" or d.get("platform") == plat)]
        if len(peers) == 1:
            return peers[0], "remote"
        if len(peers) > 1:
            return None, "ambiguous"
        if kind == "platform" and local and local.get("platform") == plat:
            return None, "local"
        return None, "unresolved"
    return None, "unresolved"


def detect(text: str, current_agent: Optional[str] = None,
           devices: Optional[Sequence[Dict[str, Any]]] = None,
           local: Optional[Dict[str, Any]] = None) -> Optional[Intent]:
    raw = (text or "").strip()
    if not raw or len(raw) > MAX_COMMAND_CHARS:
        return None
    if _QUOTED.search(raw):
        return None
    neg = _NEGATION.search(raw)
    first_kw = _KEYWORD.search(raw)
    if neg and first_kw and neg.start() <= first_kw.start():
        return None  # "不要同步给 Codex"; but "只同步 A，不要同步 B" is still a command
    question = bool(_QUESTION.search(raw))

    if _HISTORY.search(raw) and not question:
        return Intent("history", command=raw)
    if _INBOX.search(raw) and not question and not re.search(r"同步\s*(并\s*交接)?\s*(给|到)", raw):
        return Intent("inbox", command=raw)
    if question:
        return None

    devices = list(devices or [])
    names = tuple(sorted({n for d in devices + ([local] if local else [])
                          for n in (d.get("id"), d.get("label")) if n}))
    with_agent, device_only = _patterns(names)
    only = bool(re.search(r"只|仅|\bonly\b", raw, re.I))

    def build(m: "re.Match[str]", mode: str, target: Optional[str]) -> Optional[Intent]:
        g = m.groupdict()
        word = g.get("dev") or g.get("dev2") or g.get("dev3")
        device, status = resolve_device(word, devices, local) if word else (None, "local")
        defaulted = target is None
        target = target or current_agent
        if not target:
            return None
        if status == "local" and current_agent and target == current_agent:
            return None  # "同步给 Claude" said to Claude is not a cross-agent send
        scope = _clean_scope(g.get("scope"))
        return Intent("send", target=target, mode=mode, scope=scope, topic=_topic(scope),
                      scope_kind=_scope_kind(scope, raw), strict=only, command=raw,
                      device=device, device_word=word, device_status=status, agent_defaulted=defaulted)

    for mode, patterns in with_agent:
        for p in patterns:
            m = p.search(raw)
            if m:
                return build(m, mode, _agent(m.group("agent")))
    for mode, patterns in device_only:
        for p in patterns:
            m = p.search(raw)
            if m:
                return build(m, mode, None)

    m = _SYNC_NO_TARGET.search(raw)
    if m and current_agent:
        scope = _clean_scope(m.group("scope"))
        return Intent("send", target=other_agent(current_agent), mode="sync", scope=scope,
                      topic=_topic(scope), scope_kind=_scope_kind(scope, raw),
                      strict=bool(m.group("only")), command=raw)
    return None
