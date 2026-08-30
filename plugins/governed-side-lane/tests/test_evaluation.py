import json
from pathlib import Path
import unittest

from side_lane import evaluation


FIXTURE = Path(__file__).parent / "fixtures" / "evaluation" / "offline-evidence.json"


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_aggregates_cost_per_accepted_result_without_provider_calls(self) -> None:
        result = evaluation.aggregate_runs(self.data["evaluation_runs"])[0]
        self.assertEqual(result["acceptance_rate"], 0.5)
        self.assertEqual(result["cost"]["expected_per_accepted_result"], 20.0)
        self.assertEqual(result["sample_count"], 2)

    def test_community_reports_preserve_disagreement_and_cannot_activate(self) -> None:
        result = evaluation.summarize_community_signals(self.data["community_signals"])[0]
        self.assertEqual(result["supporting_reports"], 1)
        self.assertEqual(result["contrary_reports"], 1)
        self.assertEqual(result["confidence"], "mixed")
        self.assertFalse(result["activation_authority"])

    def test_duplicate_community_report_fails_closed(self) -> None:
        records = self.data["community_signals"] * 2
        with self.assertRaisesRegex(evaluation.EvaluationError, "duplicate"):
            evaluation.summarize_community_signals(records)

    def test_invalid_cost_unit_fails_closed(self) -> None:
        record = dict(self.data["evaluation_runs"][0])
        record["cost"] = {"value": 1, "unit": "", "basis": "fixture"}
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.aggregate_runs([record])


if __name__ == "__main__":
    unittest.main()
