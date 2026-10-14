import json
import unittest

from helpers import TempHome, payload


class Send(TempHome):
    def test_send_and_receive_claude_to_codex(self):  # Test 1 (mechanism)
        m = self.send("把刚才关于 Comment Collector 的讨论同步给 Codex")
        self.assertEqual(m["status"], "PENDING")
        self.assertEqual(m["to"], "codex")
        inbox = self.asb_json("inbox", "--agent", "codex")
        self.assertEqual([x["id"] for x in inbox], [m["id"]])
        shown = self.asb_json("show", m["id"], "--agent", "codex", "--session", "t1")
        self.assertEqual(shown["status"], "DELIVERED")
        self.assertIn("已确认决定", shown["body_md"])
        self.assertIn("明确排除：弹幕", shown["body_md"])
        acked = self.asb_json("ack", m["id"], "--agent", "codex", "--summary", "评论按 BV 分目录，cursor 分页待定")
        self.assertEqual(acked["status"], "ACKNOWLEDGED")

    def test_codex_to_claude(self):  # Test 2 (mechanism)
        body = payload(title="WBI 签名实现", items=[
            {"type": "IMPLEMENTED", "text": "新增 bilibili_wbi.py"},
            {"type": "VERIFIED", "text": "单测通过", "evidence": "unittest → OK"}],
            files_changed=[{"path": "backend/app/adapters/bilibili_wbi.py", "change": "新增"}],
            tests=[{"command": "python -m unittest", "result": "OK", "passed": True}])
        m = self.send("把这次代码修改同步给 Claude", src="codex", dst="claude", body=body)
        ctx = self.hook("claude", "继续", session="c1")
        self.assertIn(m["id"], ctx)
        self.assertIn("bilibili_wbi.py", ctx)
        self.assertIn("✅", ctx)
        self.assertEqual(self.asb_json("status", m["id"])["status"], "DELIVERED")

    def test_rejects_non_command(self):  # Test 4
        r = self.asb("send", "--from", "claude", "--to", "codex", "--user-command",
                     "Comment Collector 应该用同步还是异步请求？", stdin=json.dumps(payload()), check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("不是明确的同步指令", r.stderr)
        self.assertEqual(self.asb_json("history"), [])

    def test_rejects_mode_mismatch(self):
        r = self.asb("send", "--from", "claude", "--to", "codex", "--mode", "sync", "--user-command",
                     "同步并交接给 Codex", stdin=json.dumps(payload()), check=False)
        self.assertEqual(r.returncode, 1)

    def test_validation(self):
        bad = payload(items=[{"type": "VERIFIED", "text": "测试通过"}])
        r = self.asb("send", "--from", "claude", "--to", "codex", "--user-command", "同步给 Codex",
                     stdin=json.dumps(bad), check=False)
        self.assertIn("evidence", r.stderr)
        r = self.asb("send", "--from", "claude", "--to", "codex", "--user-command", "同步给 Codex",
                     stdin=json.dumps(payload(items=[{"type": "FACT", "text": "x"}])), check=False)
        self.assertIn("无效", r.stderr)
        r = self.asb("send", "--from", "claude", "--to", "codex", "--mode", "handoff",
                     "--user-command", "同步并交接给 Codex", stdin=json.dumps(payload()), check=False)
        self.assertIn("handoff", r.stderr)

    def test_handoff(self):
        body = payload(handoff={"owner": "codex", "goal": "实现 checkpoint",
                                "acceptance_criteria": ["中断后重跑不重复写入"]})
        m = self.send("同步并交接给 Codex", mode="handoff", body=body)
        shown = self.asb_json("show", m["id"])
        self.assertIn("下一阶段负责人：Codex", shown["body_md"])
        self.assertIn("- [ ] 中断后重跑不重复写入", shown["body_md"])
        self.assertEqual(shown["status"], "PENDING")  # show without --agent is a peek

    def test_duplicate_send(self):  # Test 5
        a = self.send()
        b = self.send()
        self.assertFalse(a["duplicate"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.asb_json("history")), 1)


class Hooks(TempHome):
    def test_plain_chat_injects_nothing(self):  # Test 4
        self.assertEqual(self.hook("claude", "帮我解释一下 WBI 签名"), "")
        self.assertEqual(self.asb_json("history"), [])

    def test_router_hint_does_not_create_message(self):
        ctx = self.hook("claude", "只同步刚才关于 Bilibili Comment Collector 的结论")
        self.assertIn("Claude Code → Codex", ctx)
        self.assertIn("主题：Bilibili Comment Collector", ctx)
        self.assertIn("用户要求只同步该范围", ctx)
        self.assertEqual(self.asb_json("history"), [])

    def test_unscoped_hint_only_without_scope(self):
        self.assertIn("未指定范围", self.hook("claude", "同步给 Codex"))
        whole = self.hook("claude", "把今天这整个会话的内容同步给 Codex")
        self.assertIn("范围：今天这整个会话的内容", whole)
        self.assertNotIn("未指定范围", whole)

    def test_delivery_once_per_session(self):
        m = self.send()
        first = self.hook("codex", "你好", session="x")
        self.assertIn(m["id"], first)
        self.assertIn("ack", first)
        again = self.hook("codex", "继续", session="x")
        self.assertNotIn("## 摘要", again)  # body not re-injected
        self.assertIn("仍待你确认", again)
        other_session = self.hook("codex", "新会话", session="y")
        self.assertIn("## 摘要", other_session)  # a fresh session lacks the context
        st = self.asb_json("status", m["id"])
        self.assertEqual([e["event"] for e in st["events"]], ["CREATED", "DELIVERED", "DELIVERED"])
        self.asb("ack", m["id"], "--agent", "codex", "--summary", "已了解评论采集架构")
        self.assertEqual(self.hook("codex", "再来", session="z"), "")

    def test_hook_project_isolation(self):  # Test 6
        self.send(cwd=self.proj_a)
        self.assertEqual(self.hook("codex", "hi", cwd=self.proj_b), "")
        self.assertIn("Agent Sync", self.hook("codex", "hi", cwd=self.proj_a))

    def test_linked_folders_share_project(self):
        inner = self.proj_a / "repo"
        inner.mkdir()
        self.asb("project", "link", str(self.proj_a), "anime")
        self.assertEqual(self.asb_json("project", cwd=inner)["id"], "anime")
        m = self.send(cwd=inner)
        self.assertIn(m["id"], self.hook("codex", "hi", cwd=self.proj_a))

    def test_sender_does_not_receive_own_message(self):
        self.send()
        self.assertEqual(self.hook("claude", "hi"), "")

    def test_hook_survives_bad_input(self):
        r = self.asb("hook", "codex", stdin="not json")
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
