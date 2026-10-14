import unittest

from helpers import REPO  # noqa: F401  (sets sys.path)
from asb.router import detect


class RouterCommands(unittest.TestCase):
    def check(self, text, agent, **expect):
        i = detect(text, current_agent=agent)
        self.assertIsNotNone(i, text)
        for k, v in expect.items():
            self.assertEqual(getattr(i, k), v, f"{text}: {k}")
        return i

    def test_required_commands(self):
        self.check("同步给 Codex", "claude", action="send", target="codex", mode="sync")
        self.check("同步给 Claude", "codex", action="send", target="claude", mode="sync")
        self.check("把刚才关于 Comment Collector 的讨论同步给 Codex", "claude",
                   target="codex", mode="sync", topic="Comment Collector", scope_kind="discussion")
        self.check("把这次代码修改同步给 Claude", "codex", target="claude", scope_kind="code_changes")
        self.check("同步并交接给 Codex", "claude", target="codex", mode="handoff")
        self.check("同步并交接给 Claude", "codex", target="claude", mode="handoff")
        self.check("查看同步历史", "claude", action="history")
        self.check("查看待接收消息", "codex", action="inbox")

    def test_variants(self):
        self.check("只同步刚才关于 Bilibili Comment Collector 的结论。", "claude",
                   target="codex", topic="Bilibili Comment Collector", strict=True, scope_kind="conclusions")
        self.check("交接给 codex", "claude", mode="handoff")
        self.check("请把 WBI 签名的实现同步到 Claude Code", "codex", target="claude")
        self.check("sync this discussion to Codex", "claude", target="codex", mode="sync")
        self.check("hand off to Claude", "codex", target="claude", mode="handoff")
        self.check("check inbox", "claude", action="inbox")
        self.check("把结论交给codex", "claude", target="codex", mode="sync", scope="结论")
        self.check("把这次的测试结果发给 Claude", "codex", target="claude", mode="sync")
        self.check("只同步 A 部分的讨论给 Codex，不要同步 B", "claude", target="codex")

    def test_not_commands(self):
        for text in [
            "Comment Collector 应该用同步请求还是异步请求？",
            "这个函数是同步的吗",
            "数据库同步失败了，帮我看看日志",
            "同步给 Codex 是怎么实现的？",
            "先不要同步给 Codex",
            "不用交接给 Claude，我们继续",
            "先别把这个交给 Codex",
            "这个函数交给 Codex 写的版本有 bug 吗？",
            "Codex 之前写的代码有 bug",
            "帮我写一个 Bilibili 评论采集器",
            "用户说「同步给 Codex」时应该发生什么",
            "",
            "同步给 Codex " + "很长的粘贴文档 " * 100,
        ]:
            self.assertIsNone(detect(text, current_agent="claude"), text)

    def test_sync_to_self_ignored(self):
        self.assertIsNone(detect("同步给 Claude", current_agent="claude"))


if __name__ == "__main__":
    unittest.main()
