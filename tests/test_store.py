import time
import unittest

from helpers import TempHome
from asb.store import ACKNOWLEDGED, DELIVERED, FAILED, PENDING, BridgeError, Store


def make(store, project="p1", target="codex", ctx=None, **kw):
    return store.create(project_id=project, project_root="/x", source="claude" if target == "codex" else "codex",
                        target=target, mode="sync", title="t", user_command="同步给 Codex", body_md="b",
                        context=ctx or {"title": "t", "summary": "s", "items": [{"type": "PROPOSAL", "text": "x"}]},
                        **kw)


class StateMachine(TempHome):
    def setUp(self):
        super().setUp()
        self.s = Store()

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def test_full_lifecycle(self):  # Test 7
        m = make(self.s)
        self.assertEqual(m["status"], PENDING)
        with self.assertRaises(BridgeError):
            self.s.ack(m["id"], "codex", "我已理解评论采集方案")  # cannot ack unread
        self.assertTrue(self.s.deliver(m["id"], "codex", "codex:s1", "hook"))
        self.assertEqual(self.s.get(m["id"])["status"], DELIVERED)
        with self.assertRaises(BridgeError):
            self.s.ack(m["id"], "claude", "我已理解评论采集方案")  # wrong agent
        with self.assertRaises(BridgeError):
            self.s.ack(m["id"], "codex", "ok")  # no real restatement
        a = self.s.ack(m["id"], "codex", "我已理解评论采集方案")
        self.assertEqual(a["status"], ACKNOWLEDGED)
        self.assertFalse(a["already"])
        self.assertTrue(self.s.ack(m["id"], "codex", "我已理解评论采集方案")["already"])  # idempotent
        events = [e["event"] for e in self.s.events(m["id"])]
        self.assertEqual(events, ["CREATED", "DELIVERED", "ACKNOWLEDGED"])

    def test_fail_and_retry(self):  # Test 7 + Test 9
        m = make(self.s)
        self.s.deliver(m["id"], "codex", "codex:s1", "hook")
        f = self.s.fail(m["id"], "Codex 不可用")
        self.assertEqual(f["status"], FAILED)
        self.assertEqual(self.s.inbox("codex", "p1"), [])
        with self.assertRaises(BridgeError):
            self.s.deliver(m["id"], "codex", "codex:s2", "hook")
        r = self.s.retry(m["id"])
        self.assertEqual(r["status"], PENDING)
        self.assertEqual([x["id"] for x in self.s.inbox("codex", "p1")], [m["id"]])
        self.assertTrue(self.s.deliver(m["id"], "codex", "codex:s1", "hook"))  # receipts reset by retry

    def test_expiry_marks_failed(self):
        m = make(self.s, ttl_days=0)
        time.sleep(0.01)
        self.assertEqual(self.s.inbox("codex", "p1"), [])
        self.assertEqual(self.s.get(m["id"])["status"], FAILED)
        self.assertIn("expired", self.s.get(m["id"])["error"])

    def test_dedupe(self):  # Test 5
        m1 = make(self.s)
        m2 = make(self.s)
        self.assertFalse(m1["duplicate"])
        self.assertTrue(m2["duplicate"])
        self.assertEqual(m1["id"], m2["id"])
        self.assertEqual(len(self.s.history("p1")), 1)
        m3 = make(self.s, allow_duplicate=True)
        self.assertNotEqual(m3["id"], m1["id"])
        # duplicate delivery to the same session is a no-op
        self.assertTrue(self.s.deliver(m1["id"], "codex", "codex:s1", "hook"))
        self.assertFalse(self.s.deliver(m1["id"], "codex", "codex:s1", "hook"))
        self.assertEqual(self.s.get(m1["id"])["attempts"], 1)

    def test_failed_message_can_be_resent(self):
        m1 = make(self.s)
        self.s.fail(m1["id"], "x")
        self.assertFalse(make(self.s)["duplicate"])

    def test_project_isolation(self):  # Test 6
        a = make(self.s, project="anime")
        make(self.s, project="other", ctx={"title": "o", "summary": "s", "items": [{"type": "PROPOSAL", "text": "y"}]})
        self.assertEqual([m["id"] for m in self.s.inbox("codex", "anime")], [a["id"]])
        self.assertEqual(len(self.s.inbox("codex", None)), 2)
        self.assertEqual(self.s.inbox("claude", "anime"), [])

    def test_persists_across_restart(self):  # Test 8
        m = make(self.s)
        self.s.close()
        self.s = Store()
        self.assertEqual([x["id"] for x in self.s.inbox("codex", "p1")], [m["id"]])

    def test_purge(self):
        m = make(self.s)
        self.s.deliver(m["id"], "codex", None, "cli")
        self.s.ack(m["id"], "codex", "我已理解评论采集方案")
        self.assertEqual(self.s.purge(-1), 1)
        self.assertEqual(self.s.history(None), [])


if __name__ == "__main__":
    unittest.main()
