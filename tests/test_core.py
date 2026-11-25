import concurrent.futures
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sase.core import CAPABILITIES, Core


FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "paginate" / "SKILL.md"
CAPS = dict.fromkeys(CAPABILITIES, True)
SCOPE = dict(workspace="ws", session="session", run="run", branch="main")


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "evidence.sqlite3"
        self.core = Core(self.path)
        self.addCleanup(lambda: self.core.close())
        self.skill = FIXTURE.read_text()

    def handle(self, op, **kw):
        return self.core.handle({"v": 1, "op": op, **SCOPE, **kw})

    def seed(self, suffix=""):
        version = self.handle("import", content=self.skill + suffix)["skill_version"]
        self.handle("approve", skill_version=version)
        return version

    def fact(self, kind, value, seq, **kw):
        data = {"event_id": f"{kind}-{seq}", "seq": seq, "kind": kind, "value": value}
        if kind in ("page", "first_page"):
            data["tool_call_id"] = f"tool-{seq}"
        return self.handle("event", **(data | kw))

    def decide(self, call="call", **kw):
        data = {"call_id": call, "episode_id": "episode", "as_of_seq": 10, "capabilities": CAPS}
        return self.handle("decide", **(data | kw))

    def test_three_goals_and_feedback_combinations(self):
        version = self.seed()
        for branch, goal, more, assignment, reason in [
            ("a", "all", True, "inject", "continue_pagination"),
            ("b", "all", False, "skip", "last_page"),
            ("c", "first", True, "skip", "first_page_only"),
        ]:
            with self.subTest(branch=branch):
                self.fact("goal", goal, 1, branch=branch)
                self.fact("page", more, 2, branch=branch)
                result = self.decide(branch=branch)
                self.assertEqual((result["assignment"], result["reason"]), (assignment, reason))
                state = result["state"]
                self.assertEqual((state["goal"], state["has_next"]), (goal, more))
                self.assertEqual(state["event_ids"], ["goal-1", "page-2"])
                self.assertEqual(result["skill_version"], version)
                self.assertFalse(result["randomized"])
                self.assertIsNone(result["propensity"])
                if assignment == "inject":
                    self.assertIn(result["decision_id"], result["patch"])
                else:
                    self.assertIsNone(result["patch"])

    def test_fixed_arm_is_independent_of_goal_and_continuation(self):
        version = self.seed()
        for run, goal, more in [("more", "all", True), ("last", "all", False),
                                ("first", "first", True)]:
            with self.subTest(run=run):
                self.fact("goal", goal, 1, run=run)
                self.fact("page", more, 2, run=run)
                self.fact("first_page", True, 2, run=run)
                params = dict(as_of_seq=2, run=run, evaluation_policy="fixed")
                result = self.decide(**params)
                self.assertEqual(result["assignment"], "inject")
                self.assertEqual(result["reason"], "fixed_first_page")
                self.assertTrue(result["eligibility"])
                self.assertEqual(result["policy_version"], "eval-fixed-first-page-v1")
                self.assertEqual(result["skill_version"], version)
                self.assertEqual(result["state"]["event_ids"], ["goal-1", "page-2", "first_page-2"])
                self.assertEqual(result["exposure_status"], "unknown")
                self.assertEqual(self.decide(**params), result)
                self.assertEqual(self.decide("other-call", **params)["reason"], "fixed_already_assigned")

    def test_fixed_arm_requires_matched_current_facts_and_approved_skill(self):
        def fixed(call, seq, **kw):
            return self.decide(call, as_of_seq=seq, evaluation_policy="fixed", **kw)

        self.fact("goal", "all", 1)
        self.fact("page", True, 2, tool_call_id="read")
        self.fact("first_page", True, 2, tool_call_id="other")
        self.assertEqual(fixed("wrong-tool", 2)["reason"], "not_first_page")
        self.fact("first_page", True, 3, tool_call_id="read")
        self.assertEqual(fixed("stale-feedback", 3)["reason"], "not_first_page")
        self.fact("page", True, 4, tool_call_id="read")
        self.assertEqual(fixed("stale-position", 4)["reason"], "not_first_page")
        self.fact("first_page", True, 4, tool_call_id="read")
        self.assertEqual(fixed("unapproved", 4)["reason"], "no_approved_skill")
        self.seed()
        self.assertEqual(fixed("no-hook", 4, capabilities=CAPS | {"injectContext": False})["reason"],
                         "unsupported_capabilities")
        with self.assertRaises(ValueError):
            self.decide("bad-arm", evaluation_policy="typo")

    def test_missing_and_invalidation(self):
        self.seed()
        self.fact("page", True, 2)
        self.assertEqual(self.decide()["state"]["goal"], "unknown")
        self.fact("goal", "all", 3)
        result = self.decide("next")
        self.assertEqual((result["reason"], result["state"]["has_next"]), ("unknown_state", "unknown"))
        self.fact("page", True, 4)
        self.fact("page", None, 5)
        self.assertEqual(self.decide("next-2")["reason"], "unknown_state")

    def test_event_replay_sequence_and_frozen_decision(self):
        self.seed()
        self.fact("page", True, 5)
        self.fact("goal", "all", 1)
        self.fact("goal", "all", 1)
        with self.assertRaises(ValueError):
            self.fact("goal", "first", 1)
        first = self.decide(as_of_seq=4)
        self.assertEqual(first["reason"], "unknown_state")
        self.fact("page", False, 2)
        self.assertEqual(self.decide(as_of_seq=4), first)
        self.assertEqual(self.decide("later", as_of_seq=5)["reason"], "continue_pagination")
        self.assertEqual(self.decide("middle", as_of_seq=4)["reason"], "last_page")

    def test_approval_and_crash_recovery(self):
        self.fact("goal", "all", 1)
        self.fact("page", True, 2)
        first = self.handle("import", content=self.skill)["skill_version"]
        self.assertEqual(self.decide()["reason"], "no_approved_skill")
        with self.assertRaises(ValueError):
            self.handle("approve", workspace="another-ws", skill_version=first)
        self.handle("approve", skill_version=first)
        saved = self.decide("retry-me")
        self.core.close()
        self.core = Core(self.path)
        second = self.seed("\nChanged procedure.")
        self.assertNotEqual(first, second)
        self.assertEqual(self.decide("retry-me"), saved)
        self.assertEqual(self.decide("new")["skill_version"], second)
        report = self.handle("inspect", decision_id=saved["decision_id"])["decisions"][0]
        self.assertEqual(report["exposure_status"], "unknown")
        self.assertEqual(report["receipts"], [])

    def test_parallel_tools_and_scope(self):
        self.seed()
        self.fact("goal", "all", 1)
        self.fact("page", True, 2, tool_call_id="A")
        self.fact("page", False, 2, tool_call_id="B", event_id="B-result")
        ambiguous = self.decide()
        self.assertEqual(ambiguous["reason"], "unknown_state")
        self.assertEqual(ambiguous["state"]["event_ids"], ["goal-1", "B-result", "page-2"])
        self.assertEqual(self.decide("A", tool_call_id="A")["reason"], "continue_pagination")
        self.assertEqual(self.decide("B", tool_call_id="B")["reason"], "last_page")
        for field in ("workspace", "session", "run", "branch"):
            self.assertEqual(self.decide(**{field: "other"})["state"]["event_ids"], [])

    def test_receipts_outcomes_capabilities_and_privacy(self):
        self.seed()
        self.fact("goal", "all", 1, raw_prompt="private-canary")
        self.fact("page", True, 2, tool_result="private-canary")
        assigned = self.decide()
        decision_id = assigned["decision_id"]
        delivered = dict(decision_id=decision_id, receipt_id="r0", kind="delivered", location="context")
        self.handle("receipt", **delivered)
        self.assertEqual(self.handle("inspect")["decisions"][0]["exposure_status"], "unknown")
        observed = dict(decision_id=decision_id, receipt_id="r1", kind="observed",
                        location="request_payload", attempt_id="attempt-1", payload="private-canary")
        self.handle("receipt", **observed)
        self.handle("receipt", **observed)
        with self.assertRaises(ValueError):
            self.handle("receipt", **(observed | {"attempt_id": "other"}))
        with self.assertRaises(ValueError):
            self.handle("receipt", **observed, workspace="another-ws")
        for window, value, missing in [("proximal", True, None), ("distal", None, "not_scored")]:
            self.handle("outcome", decision_id=decision_id, outcome_id=window, window=window,
                        value=value, missing_reason=missing)
        record = self.handle("inspect", decision_id=decision_id)["decisions"][0]
        self.assertEqual(record["exposure_status"], "observed")
        self.assertEqual(len(record["receipts"]), 2)
        self.assertEqual(record["proximal_outcome"]["value"], True)
        self.assertEqual(record["distal_outcome"]["missing_reason"], "not_scored")
        self.assertNotIn(b"private-canary", self.path.read_bytes())
        self.assertEqual(self.decide("n", capabilities=CAPS | {"injectContext": False})["reason"],
                         "unsupported_capabilities")
        limited = self.decide("m", capabilities=CAPS | {"confirmExposure": False})
        with self.assertRaises(ValueError):
            self.handle("receipt", **(observed | {"decision_id": limited["decision_id"]}))
        self.handle("receipt", **(observed | {"decision_id": limited["decision_id"], "location": "provider"}))

    def test_concurrent_assignment(self):
        self.seed()
        self.fact("goal", "all", 1)
        self.fact("page", True, 2)

        def decide_from_connection(_):
            core = Core(self.path)
            try:
                return core.handle(dict(SCOPE, v=1, op="decide", episode_id="e", call_id="concurrent",
                                        capabilities=CAPS, as_of_seq=2))
            finally:
                core.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(decide_from_connection, range(8)))
        self.assertEqual(len({row["decision_id"] for row in results}), 1)
        self.assertEqual(len(self.handle("inspect")["decisions"]), 1)

    def test_host_metadata_is_filtered_and_frozen(self):
        host = {"name": "test-adapter", "version": "1.2.3", "raw_settings": "private-canary"}
        saved = self.decide(host=host)
        self.assertEqual(saved["host"], {"name": "test-adapter", "version": "1.2.3"})
        self.assertEqual(self.decide(host=host | {"version": "2.0"})["host"], saved["host"])
        self.assertNotIn(b"private-canary", self.path.read_bytes())
        with self.assertRaises(ValueError):
            self.decide("invalid", host={"name": "missing-version"})

    def test_protocol_and_schema_versions(self):
        with self.assertRaises(ValueError):
            self.core.handle({"v": True, "op": "inspect", "workspace": "ws"})
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA user_version=2")
        with self.assertRaises(ValueError):
            Core(self.path)


if __name__ == "__main__":
    unittest.main()
