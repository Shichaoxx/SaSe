"""End-to-end calibration of trial ordering, the three arms and stored evidence."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from sase.evaluate import schedule
from sase.evaluation import ARMS, cases


class PairedRunTests(unittest.TestCase):
    def test_schedule_is_reproducible_and_each_task_is_matched(self):
        tasks = cases(10, 2)
        first = schedule(tasks, 10)
        self.assertEqual(first, schedule(tasks, 10))
        self.assertNotEqual(first, schedule(tasks, 11))
        for i in range(0, len(first), 3):
            group = first[i:i + 3]
            self.assertEqual(set(ARMS), {t["arm"] for t in group})
            self.assertEqual(1, len({t["task_id"] for t in group}))

    def test_fixture_scores_and_exposure(self):
        with tempfile.TemporaryDirectory() as directory:
            batch = Path(directory) / "batch"
            candidate = Path("examples/candidates/cursor-stop/paginate/SKILL.md")
            run = subprocess.run([sys.executable, "-m", "sase.evaluate", "--fixture",
                                  "--skill", str(candidate), "--output", str(batch)], capture_output=True, text=True,
                                 timeout=120)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            summary = json.loads((batch / "summary.json").read_text())
            self.assertFalse(summary["failures"])
            manifest = json.loads((batch / "manifest.json").read_text())
            obs = json.loads((batch / "observations.json").read_text())
            self.assertEqual(9, len(obs))
            snapshot = (batch / "SKILL.md").read_bytes()
            self.assertEqual(candidate.read_bytes(), snapshot)
            version = "paginate@" + sha256(snapshot).hexdigest()
            self.assertEqual(manifest["skill_version"], version)
            self.assertEqual(manifest["max_model_calls"], 6)
            self.assertEqual(manifest["order"], [{k: o[k] for k in ("task_id", "arm")} for o in obs])
            tasks = {t["task_id"]: t for t in manifest["tasks"]}
            for trial in obs:
                expected = trial["arm"] == "fixed" or (
                    trial["arm"] == "gated" and tasks[trial["task_id"]]["task_type"] == "all-more")
                self.assertLessEqual(trial["model_calls"], manifest["max_model_calls"])
                self.assertTrue(all(d["skill_version"] == version for d in trial["decisions"]))
                injected = [d for d in trial["decisions"] if d["assignment"] == "inject"]
                self.assertEqual(int(expected), len(injected))
                self.assertTrue(all(d["exposure_status"] == "observed" for d in injected))
                if trial["arm"] == "native":
                    self.assertEqual([], trial["decisions"])
