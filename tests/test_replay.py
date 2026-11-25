"""Exercise the production JSONL entry point with an offline pagination host."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES = dict(observeTools=True, observeModelRequests=True, injectContext=True,
                    confirmExposure=True, nativeSkillEvolution=False)


class Sidecar:
    def __init__(self, database):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "sase", "serve", "--db", str(database)], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self.number = 0

    def call(self, op, **fields):
        self.number += 1
        req = {"v": 1, "id": self.number, "workspace": "demo", "op": op, **fields}
        self.process.stdin.write(json.dumps(req) + "\n")
        self.process.stdin.flush()
        reply = json.loads(self.process.stdout.readline())
        if reply["id"] != self.number or not reply["ok"]:
            raise AssertionError(reply)
        return reply["result"]

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=5)
        self.process.stdout.close()


def read_page(records, offset):
    """A one-record page size makes both fixture boundaries deterministic."""
    return {"items": records[offset:offset + 1], "has_next": offset + 1 < len(records)}


class ReplayTest(unittest.TestCase):
    def test_goal_feedback_and_frozen_assignment_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "sase.sqlite3"
            core = Sidecar(db)
            try:
                content = (ROOT / "examples/paginate/SKILL.md").read_text()
                version = core.call("import", content=content)["skill_version"]
                core.call("approve", skill_version=version)
                for index, (goal, pages, action, reason) in enumerate([
                    ("all", 2, "inject", "continue_pagination"),
                    ("all", 1, "skip", "last_page"),
                    ("first", 2, "skip", "first_page_only"),
                ]):
                    stream = {"session": "session", "run": f"case-{index}", "branch": "main"}
                    records = ["PRIVATE_FIXTURE_VALUE"] * pages
                    page = read_page(records, 0)
                    core.call("event", **stream, event_id="goal", seq=1, kind="goal", value=goal)
                    core.call("event", **stream, event_id="page", seq=2, kind="page",
                              tool_call_id="read-1", value=page["has_next"])
                    call = dict(**stream, call_id="call-2", episode_id="read", as_of_seq=2,
                                capabilities=CAPABILITIES)
                    decision = core.call("decide", **call)
                    self.assertEqual((action, reason), (decision["assignment"], decision["reason"]))
                    self.assertEqual(goal, decision["state"]["goal"])
                    self.assertEqual(page["has_next"], decision["state"]["has_next"])
                    if action == "inject":
                        frozen = decision
                        original_call = call
                        self.assertIn(decision["decision_id"], decision["patch"])
                        self.assertEqual(version, decision["skill_version"])
                core.process.kill()
                core.process.wait(timeout=5)
                core.close()
                core = Sidecar(db)
                self.assertEqual(frozen, core.call("decide", **original_call))
                self.assertEqual(3, len(core.call("inspect")["decisions"]))
            finally:
                core.close()
            conn = sqlite3.connect(db)
            try:
                self.assertNotIn("PRIVATE_FIXTURE_VALUE", "\n".join(conn.iterdump()))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
