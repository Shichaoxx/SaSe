import copy
from hashlib import sha256
import json
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

    def test_sparse_profiles_have_distinct_namespaces_and_frozen_basic_manifest(self):
        basic = cases(7, 3)
        self.assertEqual(cases(7, 3, profile="basic"), basic)
        frozen = sha256(json.dumps(basic, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(frozen, "42545def0d3c8bddfef600a031ffeb23d829e69ef584880920cc5f89e7e214a0")
        sparse = cases(7, 3, profile="sparse")
        self.assertEqual(sparse, cases(7, 3, profile="sparse"))
        self.assertFalse({t["task_id"] for t in sparse} & {t["task_id"] for t in basic})
        def records(tasks):
            return {record for task in tasks for page in task["pages"] for record in page}
        self.assertFalse(records(sparse) & records(basic))
        self.assertEqual([t["prompt"] for t in sparse], [t["prompt"] for t in basic])
        for start in range(0, len(sparse), 3):
            all_pages, first_only, single = (t["pages"] for t in sparse[start:start + 3])
            self.assertEqual([bool(page) for page in all_pages], [True, False, True, True])
            self.assertEqual(all_pages, first_only)
            self.assertEqual(single, [all_pages[0]])
        with self.assertRaises(ValueError):
            cases(profile="typo")

    def test_sparse_empty_page_is_required_and_first_page_scope_still_stops(self):
        all_task, first_only, single = cases(7, profile="sparse")
        self.assertTrue(score(all_task, observed(all_task))["success"])
        skipped_empty = score(all_task, observed(all_task, tool_pages=[1, 3, 4]))
        self.assertTrue(skipped_empty["exact_ids"])
        self.assertFalse(skipped_empty["success"])
        self.assertEqual(skipped_empty["missing_pages"], [2])
        early = score(all_task, observed(all_task, tool_pages=[1], final_ids=all_task["pages"][0]))
        self.assertFalse(early["success"])
        self.assertEqual(early["missing_pages"], [2, 3, 4])
        self.assertFalse(early["exact_ids"])
        empty_stop = score(all_task, observed(all_task, tool_pages=[1, 2], final_ids=all_task["pages"][0]))
        self.assertEqual(empty_stop["missing_pages"], [3, 4])
        self.assertFalse(empty_stop["success"])
        for task in (first_only, single):
            self.assertTrue(score(task, observed(task, tool_pages=[1]))["success"])

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

    def test_final_text_mentions_are_independent_of_strict_score_and_tool_coverage(self):
        expected = self.first["pages"][0]
        out_of_scope = self.first["pages"][1][0]
        obs = observed(self.first, final_ids=None, answer_diagnostics={
            "format": "fenced_json", "mention_counts": dict.fromkeys(expected, 1),
        }, termination_reason="completed")
        row = score(self.first, obs)
        self.assertFalse(row["success"])
        self.assertTrue(row["retrieval_complete"])
        self.assertTrue(row["answer_diagnostics"]["exact_mentions"])
        wrong = {"format": "text", "mention_counts": {expected[0]: 2, out_of_scope: 1}}
        diag = score(self.first, obs | {"answer_diagnostics": wrong})["answer_diagnostics"]
        self.assertEqual(diag["duplicate_counts"], {expected[0]: 2})
        self.assertEqual(diag["unexpected_counts"], {out_of_scope: 1})
        self.assertEqual(diag["missing_ids"], sorted(expected[1:]))
        for bad in [{"format": "text", "mention_counts": {"arbitrary private text": 1}},
                    {"format": "private text", "mention_counts": {}},
                    {"format": "text", "mention_counts": {expected[0]: True}}]:
            self.assertIsNone(score(self.first, obs | {"answer_diagnostics": bad})["answer_diagnostics"])
        # Infrastructure failure and known model-call exhaustion override stale answer fields.
        self.assertIsNone(score(self.first, obs | {"error": "timeout"})["answer_diagnostics"])
        stop = score(self.first, observed(self.first, termination_reason="model_call_budget",
                                          answer_diagnostics=obs["answer_diagnostics"]))
        self.assertEqual((stop["status"], stop["success"]), ("scored", False))
        self.assertIsNone(stop["answer_diagnostics"])
        self.assertIsNone(stop["exact_ids"])
        self.assertIn("model_call_budget", stop["reasons"])

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

    def test_reporting_budget_denominators_and_old_observations(self):
        task = cases(3)[0]
        rows = [observed(task, "native", total_tokens=19),  # legacy observation
                observed(task, "fixed", total_tokens=25, final_ids=None,
                         termination_reason="model_call_budget", tool_pages=[1] * 20),
                observed(task, "gated", total_tokens=20, final_ids=None, termination_reason="completed",
                         answer_diagnostics={"format": "text", "mention_counts": {
                             value: 1 for page in task["pages"] for value in page}})]
        summary = summarize([task], rows)
        groups = {g["arm"]: g for g in summary["groups"] if g["task_type"] == "overall"}
        self.assertEqual(groups["native"]["answer_observed"], 0)
        self.assertEqual(groups["native"]["budget_observed"], 0)
        self.assertEqual(groups["gated"]["format_counts"], {"text": 1})
        self.assertEqual(groups["gated"]["answer_mentions_exact"], 1)
        self.assertEqual((groups["fixed"]["budget_stops"], groups["fixed"]["failures"],
                          groups["fixed"]["missing"]), (1, 1, 0))
        self.assertEqual(groups["fixed"]["mean_tool_calls"], 20)
        paired = next(p for p in summary["comparisons"] if p["baseline"] == "native" and
                      p["arm"] == "fixed" and p["task_type"] == "overall")
        self.assertEqual(paired["mean_token_delta"], 6)
        self.assertIn("共 20", report(summary))
        # Re-render a pre-diagnostics saved summary without fabricating final-text evidence.
        for g in summary["groups"]:
            del g["answer_observed"]
        for row in summary["failures"]:
            del row["answer_diagnostics"]
            del row["observation_ref"]
        self.assertNotIn("## 工具覆盖与最终回答", report(summary))

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
