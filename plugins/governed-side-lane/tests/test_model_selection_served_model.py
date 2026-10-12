"""The receipt's served-model contract must stay identical to the execution adapter's."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from side_lane.adapters import claude as adapter  # noqa: E402
from side_lane.model_selection import contract  # noqa: E402
from side_lane.model_selection.contract import ContractError, ExecutionReceipt  # noqa: E402

SAMPLES = [
    "anthropic/claude-sonnet-5.5", "openai/gpt-6.1-sol", "z-ai/glm-5.3", "moonshotai/kimi-k2", "meta-llama/llama-3.1:free",
    "Anthropic/claude", "anthropic/claude~x", "vendor/.", "vendor/..", "./x", "vendor/a/b", "vendor/a b", "vendor/",
    "/model", "", "claude", "a/" + "x" * 126, "a/" + "x" * 127, "a/" + "x" * 200, "-a/b", "a/b\n", None, 5, ["a/b"],
    "openrouter/auto", "openrouter/auto-beta", "@preset/x", "x/y:thinking", "x/y_z", "x/y z", "x/ÿ",
]


class ServedModelContractTests(unittest.TestCase):
    def test_the_constants_match_the_adapter(self):
        self.assertEqual(contract.OPENROUTER_MODEL_ID_RE.pattern, adapter.OPENROUTER_MODEL_ID_RE.pattern)
        self.assertEqual(contract.OPENROUTER_MODEL_ID_MAX_CHARS, adapter.OPENROUTER_MODEL_ID_MAX_CHARS)

    def test_the_decision_matches_the_adapter_for_every_sample(self):
        for sample in SAMPLES:
            self.assertEqual(contract.is_served_model_id(sample), adapter.is_openrouter_model_id(sample), repr(sample))

    def test_the_looser_shapes_the_review_found_are_now_refused(self):
        for bad in ("Anthropic/claude-sonnet-5-5", "anthropic/claude~x", "vendor/.", "vendor/..", "a/" + "x" * 127):
            self.assertFalse(contract.is_served_model_id(bad), bad)
            receipt = ExecutionReceipt("s1", "openrouter", bad, "metered_openrouter", True, auto_resolved=True,
                                       selected_provider="openrouter", selected_model="openrouter/auto",
                                       selected_route="metered_openrouter")
            with self.assertRaises(ContractError):
                receipt.validate()
        self.assertTrue(contract.is_served_model_id("anthropic/claude-sonnet-5.5"))


class ProviderRouteTests(unittest.TestCase):
    def test_the_openrouter_provider_and_the_metered_route_go_together(self):
        with self.assertRaises(ContractError):
            ExecutionReceipt("s1", "claude", "anthropic/claude-sonnet-5.5", "metered_openrouter", True).validate()
        with self.assertRaises(ContractError):
            ExecutionReceipt("s1", "openrouter", "anthropic/claude-sonnet-5.5", "native_cli", True).validate()
        ExecutionReceipt("s1", "openrouter", "anthropic/claude-sonnet-5.5", "metered_openrouter", True).validate()
        ExecutionReceipt("s1", "claude", "claude-opus-5-5", "native_cli", True).validate()


if __name__ == "__main__":
    unittest.main()
