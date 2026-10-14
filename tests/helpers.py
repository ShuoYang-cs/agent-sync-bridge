import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

ASB = str(REPO / "bin" / "asb")


def payload(**over):
    p = {
        "title": "Comment Collector 架构",
        "summary": "确定评论采集分页与重试方案。",
        "scope": {"topic": "Bilibili Comment Collector", "included": "分页与重试", "excluded": ["弹幕"]},
        "items": [
            {"type": "CONFIRMED_DECISION", "text": "评论按 BV 号分目录存储"},
            {"type": "PROPOSAL", "text": "cursor 分页"},
            {"type": "UNVERIFIED", "text": "412 来自频控"},
        ],
    }
    p.update(over)
    return p


class TempHome(unittest.TestCase):
    """Each test gets an isolated ASB_HOME (and a fake project dir)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        os.environ["ASB_HOME"] = str(self.home / "asb")
        os.environ.pop("ASB_PROJECT", None)
        self.proj_a = self.home / "projA"
        self.proj_b = self.home / "projB"
        self.proj_a.mkdir()
        self.proj_b.mkdir()

    def tearDown(self):
        os.environ.pop("ASB_HOME", None)
        self._tmp.cleanup()

    def asb(self, *args, stdin=None, cwd=None, env=None, check=True):
        e = dict(os.environ, **(env or {}))
        e.pop("CLAUDE_CODE_SESSION_ID", None)
        r = subprocess.run([ASB, *args], input=stdin, capture_output=True, text=True,
                           cwd=str(cwd or self.proj_a), env=e)
        if check and r.returncode != 0:
            raise AssertionError(f"asb {args} failed ({r.returncode}): {r.stdout}\n{r.stderr}")
        return r

    def asb_json(self, *args, **kw):
        r = self.asb(*args, "--json", **kw)
        return json.loads(r.stdout)

    def send(self, cmd="同步给 Codex", src="claude", dst="codex", mode="sync", body=None, cwd=None, extra=()):
        return self.asb_json("send", "--from", src, "--to", dst, "--mode", mode, "--user-command", cmd,
                             *extra, stdin=json.dumps(body or payload(), ensure_ascii=False), cwd=cwd)

    def hook(self, agent, prompt, session="s1", cwd=None):
        data = {"prompt": prompt, "cwd": str(cwd or self.proj_a), "session_id": session,
                "hook_event_name": "UserPromptSubmit"}
        r = self.asb("hook", agent, stdin=json.dumps(data, ensure_ascii=False), cwd=cwd)
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout.strip() else ""
