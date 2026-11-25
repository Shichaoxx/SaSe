import copy
import unittest

from sase.evaluation import cases, report, score, summarize


def observed(task, arm="native", **updates):
    needed = task["pages"][:1] if task["goal"] == "first" else task["pages"]
    return {"task_id": task["task_id"], "arm": arm, "error": None,
            "final_ids": [value for page in needed for value in page],
            "tool_pages": list(range(1, len(needed) + 1)),
            "trace_ref": f"{arm}-{task['task_id']}", **updates}


class TaskScoringTest(unittest.TestCase):
    def setUp(self):
        self.all, self.first, self.single = cases(7, 1)

    def test_case_manifest_is_stable_and_matched(self):
        self.assertEqual(cases(5, 2), cases(5, 2))
        self.assertNotEqual(cases(6, 2), cases(5, 2))
        self.assertEqual(len(cases(5, 2)), 6)
        for task in cases(7, 12):
            self.assertEqual(len(task["pages"]), 1 if task["task_type"] == "all-last" else 2)
        self.assertEqual(self.all["pages"], self.first["pages"])
        self.assertEqual(self.single["pages"], self.all["pages"][:1])
        for task in (self.all, self.first, self.single):
            self.assertTrue(task["prompt"].startswith(f"sase.goal={task['goal']}\n"))
            ids = [r for p in task["pages"] for r in p]
            self.assertEqual(len(set(ids)), len(ids))
            self.assertFalse(any(x in task["prompt"] for x in ids))
            self.assertTrue(score(task, observed(task))["success"])
        with self.assertRaises(ValueError):
            cases(0, 0)

    def test_scoring_is_arm_blind_and_inspects_answer(self):
        obs = observed(self.all)
        obs["final_ids"].reverse()
        obs["arm"] = "gated"
        obs["decisions"] = [{"exposure_status": "unknown"}]
        self.assertEqual(score(self.all, obs), score(self.all, observed(self.all)))
        was = obs["final_ids"].pop()
        duplicate = obs["final_ids"][0]
        obs["final_ids"].extend([duplicate, "other"])
        metrics = score(self.all, obs)
        self.assertFalse(metrics["success"])
        self.assertEqual(metrics["missing_ids"], [was])
        self.assertEqual(metrics["duplicate_ids"], [duplicate])
        self.assertEqual(metrics["unexpected_ids"], ["other"])

    def test_answer_cannot_hide_invalid_or_missing_reads(self):
        obs = observed(self.first, tool_pages=[1, 1, 2, -1, True, None])
        metrics = score(self.first, obs)
        self.assertTrue(metrics["exact_ids"])
        self.assertEqual(metrics["tool_calls"], 6)
        self.assertEqual(metrics["extra_calls"], 5)
        self.assertEqual(metrics["beyond_scope_reads"], 1)
        self.assertEqual(metrics["out_of_range_reads"], 3)
        self.assertFalse(metrics["success"])
        repeat = score(self.single, observed(self.single, tool_pages=[1, 1]))
        self.assertTrue(repeat["success"])
        self.assertEqual(repeat["extra_calls"], 1)
        self.assertEqual(score(self.all, observed(self.all, tool_pages=[]))["missing_pages"],
                         list(range(1, len(self.all["pages"]) + 1)))

    def test_missing_is_distinct_from_malformed_reply(self):
        self.assertFalse(score(self.all, observed(self.all, final_ids=None))["success"])
        bad = score(self.all, observed(self.all, final_ids=[3]))
        self.assertIn("invalid_final_answer", bad["reasons"])
        for changes in ({"error": "timeout"}, {"tool_pages": None}, {"tool_pages": "2"}):
            self.assertIsNone(score(self.all, observed(self.all, **changes))["success"])
        unknown = score(self.all, {"error": "not_run"})
        self.assertIsNone(unknown["exact_ids"])
        self.assertIsNone(unknown["tool_calls"])


class ReportTest(unittest.TestCase):
    def test_divisors_pairing_token_delta_and_exposure_ref(self):
        tasks = cases(1, 1)
        evidence = [{"decision_id": "dec01", "assignment": "inject", "reason": "continue_pagination",
                     "exposure_status": "observed", "patch": "unneeded raw payload"}]
        rows = [
            observed(tasks[0], "native", total_tokens=100, final_ids=[]),
            observed(tasks[0], "fixed", total_tokens=120),
            observed(tasks[0], "gated", total_tokens=105),
            observed(tasks[1], "native", total_tokens=300),
            observed(tasks[1], "fixed", error="timeout", total_tokens=100),
            observed(tasks[1], "gated", tool_pages=[1, 2], total_tokens=None, decisions=evidence),
            observed(tasks[2], "native", total_tokens=10),
            observed(tasks[2], "fixed", total_tokens=40),
        ]
        result = summarize(tasks, rows)
        lookup = {(row["arm"], row["task_type"]): row for row in result["groups"]}
        self.assertEqual(lookup["gated", "overall"]["success_rate_bounds"], [1 / 3, 2 / 3])
        self.assertEqual(lookup["gated", "overall"]["success_rate_scored"], 0.5)
        self.assertEqual(lookup["fixed", "overall"]["missing"], 1)
        self.assertIsNone(lookup["fixed", "first-more"]["mean_tool_calls"])
        pairs = {p["arm"]: p for p in result["comparisons"]
                 if p["baseline"] == "native" and p["task_type"] == "overall"}
        self.assertEqual(pairs["fixed"]["scored_pairs"], 2)
        self.assertEqual(pairs["fixed"]["mean_token_delta"], 25)
        self.assertEqual(pairs["gated"]["token_pairs"], 1)
        self.assertEqual(pairs["gated"]["mean_token_delta"], 5)
        self.assertEqual(pairs["gated"]["success_delta_pp"], 0)
        self.assertEqual((pairs["gated"]["wins"], pairs["gated"]["losses"]), (1, 1))
        self.assertEqual(len(result["failures"]), 4)
        fail = next(r for r in result["failures"] if r["task_type"] == "first-more" and r["arm"] == "gated")
        self.assertEqual(fail["trace_ref"], rows[5]["trace_ref"])
        self.assertEqual(fail["observation_ref"], "observations.json#/5")
        self.assertNotIn("patch", fail["decisions"][0])
        md = report(result)
        for needle in ("dec01", "observed", rows[5]["trace_ref"], "not_run", "timeout"):
            self.assertIn(needle, md)
        self.assertNotIn("unneeded raw payload", md)

    def test_rejects_unplanned_or_duplicate_rows(self):
        tasks = cases()
        row = observed(tasks[0])
        for observations in ([row, row], [row | {"task_id": "other"}], [row | {"arm": "new"}]):
            with self.assertRaises(ValueError):
                summarize(tasks, observations)
        with self.assertRaises(ValueError):
            summarize([*tasks, copy.deepcopy(tasks[0])], [])


if __name__ == "__main__":
    unittest.main()
