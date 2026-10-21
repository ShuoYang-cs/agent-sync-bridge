import json
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


class RemoteMerge(TempHome):
    """Receipt merge rules on the sender side, without Git."""

    def setUp(self):
        super().setUp()
        self.s = Store(device="mac")
        self.m = self.s.create(project_id="p", project_root="/x", source="codex", target="claude", mode="sync",
                               title="t", user_command="同步给 Windows 上的 Claude", body_md="b",
                               context={"title": "t", "summary": "s", "items": [{"type": "PROPOSAL", "text": "x"}]},
                               target_device="win")
        self.s.mark_sent([self.m["id"]])

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def ev(self, kind, **kw):
        return {"message_id": self.m["id"], "type": kind, "ts": time.time(), "agent": "claude", **kw}

    def test_outbound_is_not_local(self):
        self.assertEqual(self.s.inbox("claude", "p"), [])
        with self.assertRaises(BridgeError):
            self.s.deliver(self.m["id"], "claude", "claude:s", "hook")
        self.s.db.execute("UPDATE messages SET expires_at=0 WHERE id=?", (self.m["id"],))
        self.assertEqual(self.s.expire(), 0)  # the other computer decides when it expires

    def test_only_the_receiver_can_confirm(self):
        self.assertEqual(self.s.apply_remote_event(self.ev("ACKNOWLEDGED", summary="伪造"), "mac"), "not-allowed")
        self.assertEqual(self.s.apply_remote_event(self.ev("DELIVERED"), "evil"), "foreign")
        self.assertEqual(self.s.apply_remote_event(dict(self.ev("DELIVERED"), message_id="0101-deadbeef"), "win"), "orphan")
        self.assertEqual(self.s.get(self.m["id"])["status"], PENDING)

    def test_merge_rules(self):
        self.assertEqual(self.s.apply_remote_event(self.ev("RECEIVED"), "win"), "noop")
        self.assertEqual(self.s.get(self.m["id"])["transport"], "RECEIVED")
        self.assertEqual(self.s.apply_remote_event(self.ev("DELIVERED"), "win"), "applied")
        self.assertEqual(self.s.apply_remote_event(self.ev("DELIVERED"), "win"), "noop")
        self.s.fail(self.m["id"], "发错了")
        self.assertEqual(self.s.apply_remote_event(self.ev("RETRY"), "win"), "applied")  # either side may reopen
        self.assertEqual(self.s.get(self.m["id"])["status"], PENDING)
        self.assertEqual(self.s.apply_remote_event(self.ev("ACKNOWLEDGED", summary="已读"), "win"), "applied")
        self.assertEqual(self.s.apply_remote_event(self.ev("FAILED", detail="expired"), "win"), "noop")
        self.assertEqual(self.s.apply_remote_event(self.ev("RETRY"), "win"), "noop")
        m = self.s.get(self.m["id"])
        self.assertEqual((m["status"], m["ack_summary"]), (ACKNOWLEDGED, "已读"))

    def test_local_changes_become_receipts(self):
        self.s.fail(self.m["id"], "发错了")
        self.s.retry(self.m["id"])
        kinds = [json.loads(r["payload"])["type"] for r in self.s.unpushed()]
        self.assertEqual(kinds, ["FAILED", "RETRY"])


if __name__ == "__main__":
    unittest.main()
