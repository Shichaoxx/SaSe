import json
import unittest

from sase.calibration import calibration_report, calibration_result
from sase.evaluation import cases


def native(task, **changes):
    pages = task["pages"][:1] if task["goal"] == "first" else task["pages"]
    ids = [item for page in pages for item in page]
    return {"task_id": task["task_id"], "arm": "native", "error": None,
            "tool_pages": list(range(1, len(pages) + 1)), "final_ids": ids,
            "termination_reason": "completed",
            "answer_diagnostics": {"format": "strict_json", "mention_counts": dict.fromkeys(ids, 1)},
            **changes}


class NativeCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.tasks = cases(30, 3)
        self.runs = [native(task) for task in self.tasks]

    def result(self, runs):
        return calibration_result(self.tasks, runs, fixture=False)

    def test_ceiling_and_descriptive_fixture_report(self):
        result = calibration_result(self.tasks, self.runs, fixture=True)
        self.assertEqual(result["status"], "ceiling")
        self.assertTrue(result["fixture"])
        self.assertTrue(result["provisional"])
        self.assertEqual(result["overall"]["planned"], 9)
        self.assertEqual(result["overall"]["missing"], 0)
        for group in result["groups"]:
            for metric in ("strict_success", "tool_use", "natural_stop", "strict_json"):
                self.assertEqual(group[metric], {"yes": 3, "no": 0, "unknown": 0})
        report = calibration_report(json.loads(json.dumps(result)))
        self.assertIn("fixture", report)
        self.assertIn("ceiling", report)
        self.assertNotIn(self.tasks[0]["task_id"], report)
        self.assertNotIn(self.tasks[0]["pages"][0][0], report)

    def test_completed_wrong_format_is_scored_and_different_from_retrieval(self):
        failures = [native(t, final_ids=None, answer_diagnostics={"format": "text", "mention_counts": {}})
                    for t in self.tasks]
        result = self.result(failures)
        self.assertEqual(result["status"], "floor")
        self.assertEqual(result["overall"]["strict_json"]["no"], 9)
        self.assertEqual(result["overall"]["natural_stop"]["yes"], 9)
        self.assertEqual(result["overall"]["tool_use"]["yes"], 9)
        failures[0] = self.runs[0]
        # Final IDs are correct but the first-page-only trial also fetched page 2.
        failures[1] = native(self.tasks[1], tool_pages=[1, 2])
        mixed = self.result(failures)
        self.assertEqual(mixed["status"], "mixed")
        self.assertEqual(mixed["overall"]["strict_success"]["yes"], 1)
        self.assertEqual(mixed["overall"]["tool_use"]["no"], 1)
        self.assertEqual(mixed["overall"]["strict_json"]["yes"], 2)

    def test_sample_rule_checks_each_type_after_evidence_integrity(self):
        batch = self.tasks[:8]
        small = calibration_result(batch, self.runs[:8], fixture=False)
        self.assertEqual(small["status"], "insufficient_sample")
        result = calibration_result(batch, self.runs[:7], fixture=False)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["issues"], {"missing_observation": 1})
        self.assertEqual(result["overall"]["strict_success"], {"yes": 7, "no": 0, "unknown": 1})

    def test_infrastructure_failure_and_bad_observation_never_become_model_failures(self):
        bad = [native(self.tasks[0], error="timeout"), native(self.tasks[1], tool_pages="x"),
               native(self.tasks[2], final_ids=7)]
        result = self.result(bad + self.runs[3:])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["overall"]["missing"], 3)
        self.assertEqual(result["overall"]["strict_success"], {"yes": 6, "no": 0, "unknown": 3})
        self.assertEqual(result["issues"]["execution_error"], 1)

    def test_missing_natural_stop_and_format_measurements_remain_unknown(self):
        runs = self.runs.copy()
        runs[0] = {k: v for k, v in runs[0].items() if k != "termination_reason"}
        runs[1] = {k: v for k, v in runs[1].items() if k != "answer_diagnostics"}
        calibration = self.result(runs)
        self.assertEqual(calibration["status"], "incomplete")
        self.assertEqual(calibration["overall"]["natural_stop"]["unknown"], 1)
        self.assertEqual(calibration["overall"]["strict_json"]["unknown"], 1)
        self.assertEqual(calibration["overall"]["strict_success"]["yes"], 9)
        self.assertEqual(calibration["overall"]["missing"], 2)

    def test_budget_and_provider_truncation_are_known_stops(self):
        runs = self.runs.copy()
        runs[0] = native(self.tasks[0], termination_reason="model_call_budget", final_ids=None,
                         answer_diagnostics=None, tool_pages=[1])
        # A length-capped answer can be syntactically valid; its stop was forced.
        runs[1] = native(self.tasks[1], termination_reason="output_limit")
        runs[2] = native(self.tasks[2], termination_reason="output_limit", final_ids=None,
                         answer_diagnostics=None)
        result = self.result(runs)
        self.assertEqual(result["status"], "mixed")
        self.assertEqual(result["overall"]["strict_success"]["no"], 3)
        self.assertEqual(result["overall"]["natural_stop"]["no"], 3)
        self.assertEqual(result["overall"]["strict_json"], {"yes": 7, "no": 0, "unknown": 2})
        self.assertFalse(result["issues"])

    def test_duplicate_foreign_arm_unknown_task_and_unstructured_input(self):
        invalid = [self.runs[3], self.runs[1] | {"arm": "gated"}, self.runs[4] | {"task_id": "x"}, 4]
        result = self.result(self.runs + invalid)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["overall"]["planned"], 9)
        self.assertEqual(result["overall"]["strict_success"], {"yes": 8, "no": 0, "unknown": 1})
        self.assertEqual(result["issues"], {"duplicate_observation": 1, "unexpected_observation": 2,
                                            "invalid_observation": 1})
        with self.assertRaises(ValueError):
            calibration_result(self.tasks + [self.tasks[0]], self.runs, fixture=False)


if __name__ == "__main__":
    unittest.main()
