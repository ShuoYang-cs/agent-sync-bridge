"""Agent Sync Bridge: user-controlled context sync between Claude Code and Codex."""

__version__ = "0.1.0"

AGENTS = ("claude", "codex")

AGENT_LABELS = {"claude": "Claude Code", "codex": "Codex"}


def other_agent(agent: str) -> str:
    return "codex" if agent == "claude" else "claude"
