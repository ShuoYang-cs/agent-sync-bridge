"""Command Router: deterministic detection of explicit sync commands.

Only short, imperative commands count. Questions about syncing, negated
requests, quoted mentions and long pasted documents never produce a send
intent, so ordinary chat cannot create sync messages.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Optional

from . import other_agent

MAX_COMMAND_CHARS = 500

AGENT = r"(?P<agent>claude(?:\s*code)?|codex|克劳德)"
TO = r"\s*(?:给|到|至|to)\s*"

_HANDOFF = [
    re.compile(r"同步\s*(?:并|和|且|然后)?\s*交接" + TO + AGENT, re.I),
    re.compile(r"(?<!同步)交接" + TO + AGENT, re.I),
    re.compile(r"\b(?:sync\s+and\s+)?hand\s*-?\s*off\b(?P<scope>.*?)\bto\s+" + AGENT, re.I),
]
_SYNC = [
    re.compile(r"把(?P<scope>.+?)同步" + TO + AGENT, re.I),
    re.compile(r"把(?P<scope>.+?)(?:交|发|传)给\s*" + AGENT, re.I),
    re.compile(r"同步(?P<scope>[^，,。.!！?？]{0,60}?)" + TO + AGENT, re.I),
    re.compile(r"\bsync\b(?P<scope>.*?)\bto\s+" + AGENT, re.I),
]
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

    def to_dict(self):
        return asdict(self)


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


def detect(text: str, current_agent: Optional[str] = None) -> Optional[Intent]:
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

    for mode, patterns in (("handoff", _HANDOFF), ("sync", _SYNC)):
        for p in patterns:
            m = p.search(raw)
            if not m:
                continue
            target = _agent(m.group("agent"))
            if current_agent and target == current_agent:
                return None  # "同步给 Claude" said to Claude is not a cross-agent send
            scope = _clean_scope(m.groupdict().get("scope"))
            only = bool(re.search(r"只|仅|\bonly\b", raw, re.I))
            return Intent("send", target=target, mode=mode, scope=scope, topic=_topic(scope),
                          scope_kind=_scope_kind(scope, raw), strict=only, command=raw)

    m = _SYNC_NO_TARGET.search(raw)
    if m and current_agent:
        scope = _clean_scope(m.group("scope"))
        return Intent("send", target=other_agent(current_agent), mode="sync", scope=scope,
                      topic=_topic(scope), scope_kind=_scope_kind(scope, raw),
                      strict=bool(m.group("only")), command=raw)
    return None
