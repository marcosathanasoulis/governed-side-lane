from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from side_lane import model_select as ms  # noqa: E402


FIXTURE_SNAPSHOT = {
    "schema_version": 1,
    "snapshot_id": "model-select-test-fixture",
    "generated_from": {
        "selector_commit": "0" * 40,
        "generated_at": "2026-09-30T00:00:00Z",
        "method": "fixture",
    },
    "task_families": {
        "general_coding_execution": {
            "description": "fixture family",
            "models": {
                "claude-opus-5": {
                    "success_probability": None,
                    "quality_tier": "high",
                    "capabilities": {"tools": True, "code_edit": True, "long_context": None},
                    "token_multiplier": None,
                },
                "claude-haiku-4-5-20251001": {
                    "success_probability": None,
                    "quality_tier": "low",
                    "capabilities": {"tools": True, "code_edit": True, "long_context": None},
                    "token_multiplier": None,
                },
                "gpt-6-sol": {
                    "success_probability": None,
                    "quality_tier": "high",
                    "capabilities": {"tools": True, "code_edit": True, "long_context": None},
                    "token_multiplier": None,
                },
                "qwen/qwen3-coder": {
                    "success_probability": None,
                    "quality_tier": "medium",
                    "capabilities": {"tools": True, "code_edit": True, "long_context": None},
                    "token_multiplier": None,
                },
                "bytedance/ui-tars-1.5-7b": {
                    "success_probability": None,
                    "quality_tier": "low",
                    "capabilities": {"tools": False, "code_edit": False, "long_context": None},
                    "token_multiplier": None,
                },
            },
        }
    },
    "jev_decisions": [],
    "notes": {},
}


def _profile(**overrides):
    defaults = dict(task_family="general_coding_execution", input_tokens=1000, output_tokens=200)
    defaults.update(overrides)
    return ms.TaskProfile(**defaults)


class SnapshotValidationTests(unittest.TestCase):
    def test_validate_accepts_fixture(self) -> None:
        ms.validate_snapshot(FIXTURE_SNAPSHOT)

    def test_validate_rejects_bad_schema_version(self) -> None:
        with self.assertRaises(ms.ModelSelectError):
            ms.validate_snapshot({**FIXTURE_SNAPSHOT, "schema_version": 99})


class RankCandidatesTests(unittest.TestCase):
    def test_claude_only(self) -> None:
        result = ms.rank_candidates(
            snapshot=FIXTURE_SNAPSHOT,
            available_hosts={"claude": True, "codex": False},
            openrouter_available=False,
            profile=_profile(),
        )
        hosts = {item.host for item in result.ranked}
        self.assertEqual(hosts, {"claude"})
        self.assertEqual(result.snapshot_id, "model-select-test-fixture")

    def test_codex_only(self) -> None:
        result = ms.rank_candidates(
            snapshot=FIXTURE_SNAPSHOT,
            available_hosts={"claude": False, "codex": True},
            openrouter_available=False,
            profile=_profile(),
        )
        hosts = {item.host for item in result.ranked}
        self.assertEqual(hosts, {"codex"})

    def test_both_hosts(self) -> None:
        result = ms.rank_candidates(
            snapshot=FIXTURE_SNAPSHOT,
            available_hosts={"claude": True, "codex": True},
            openrouter_available=False,
            profile=_profile(),
        )
        hosts = {item.host for item in result.ranked}
        self.assertEqual(hosts, {"claude", "codex"})

    def test_both_plus_openrouter_without_prices_is_unknown_not_free(self) -> None:
        result = ms.rank_candidates(
            snapshot=FIXTURE_SNAPSHOT,
            available_hosts={"claude": True, "codex": True},
            openrouter_available=True,
            profile=_profile(),
        )
        openrouter_items = [item for item in result.ranked if item.host == "openrouter"]
        self.assertTrue(openrouter_items)
        for item in openrouter_items:
            self.assertIsNone(item.expected_cost_per_success_usd)
            self.assertEqual(item.basis, "unknown")

    def test_both_plus_openrouter_with_live_prices(self) -> None:
        price_rows = {
            "qwen/qwen3-coder": {"input_usd_per_million": "0.30", "output_usd_per_million": "1.00"},
        }
        result = ms.rank_candidates(
            snapshot=FIXTURE_SNAPSHOT,
            available_hosts={"claude": False, "codex": False},
            openrouter_available=True,
            profile=_profile(),
            price_rows=price_rows,
        )
        qwen = next(item for item in result.ranked if item.model_id == "qwen/qwen3-coder")
        self.assertEqual(qwen.basis, "live_price")
        expected = (Decimal("0.30") * 1000 + Decimal("1.00") * 200) / Decimal(1_000_000)
        self.assertEqual(qwen.expected_cost_per_success_usd, expected)

    def test_extra_usage_declared_without_price_is_excluded(self) -> None:
        result = ms.rank_candidates(
            snapshot=FIXTURE_SNAPSHOT,
            available_hosts={"claude": True, "codex": False},
            openrouter_available=False,
            profile=_profile(),
            host_usage={"claude": "extra-usage"},
        )
        self.assertEqual(result.ranked, [])
        self.assertTrue(any("extra_usage_unknown_metered_price" in reason for _, reason in result.excluded))

    def test_included_usage_gets_nonzero_opportunity_cost_when_reference_known(self) -> None:
        price_rows = {
            "claude-opus-5": {"input_usd_per_million": "15.00", "output_usd_per_million": "75.00"},
        }
        result = ms.rank_candidates(
            snapshot=FIXTURE_SNAPSHOT,
            available_hosts={"claude": True, "codex": False},
            openrouter_available=False,
            profile=_profile(required_capabilities=frozenset({"tools"})),
            host_usage={"claude": "included-oauth"},
            price_rows=price_rows,
        )
        opus = next(item for item in result.ranked if item.model_id == "claude-opus-5")
        self.assertEqual(opus.basis, "snapshot_prior+opportunity_cost")
        self.assertIsNotNone(opus.expected_cost_per_success_usd)
        self.assertGreater(opus.expected_cost_per_success_usd, Decimal(0))

    def test_unknown_price_never_treated_as_free(self) -> None:
        result = ms.rank_candidates(
            snapshot=FIXTURE_SNAPSHOT,
            available_hosts={"claude": True, "codex": False},
            openrouter_available=False,
            profile=_profile(),
        )
        for item in result.ranked:
            if item.basis == "unknown":
                self.assertIsNone(item.expected_cost_per_success_usd)

    def test_quality_floor_excludes_low_tier(self) -> None:
        result = ms.rank_candidates(
            snapshot=FIXTURE_SNAPSHOT,
            available_hosts={"claude": True, "codex": False},
            openrouter_available=False,
            profile=_profile(quality_floor="high"),
        )
        models = {item.model_id for item in result.ranked}
        self.assertNotIn("claude-haiku-4-5-20251001", models)
        self.assertTrue(any(reason == "below_quality_floor" for _, reason in result.excluded))

    def test_capability_gate_excludes_missing_tool_support(self) -> None:
        result = ms.rank_candidates(
            snapshot=FIXTURE_SNAPSHOT,
            available_hosts={"claude": False, "codex": False},
            openrouter_available=True,
            profile=_profile(required_capabilities=frozenset({"tools"})),
            price_rows={"bytedance/ui-tars-1.5-7b": {"input_usd_per_million": "0.1", "output_usd_per_million": "0.2"}},
        )
        models = {item.model_id for item in result.ranked}
        self.assertNotIn("bytedance/ui-tars-1.5-7b", models)


class JevRecheckTests(unittest.TestCase):
    def _ranking(self) -> ms.RankingResult:
        return ms.RankingResult(
            snapshot_id="fixture",
            ranked=[
                ms.RankedCandidate("a", "model-a", "claude", Decimal("1.00"), "live_price", "high"),
                ms.RankedCandidate("b", "model-b", "codex", Decimal("1.02"), "live_price", "high"),
                ms.RankedCandidate("c", "model-c", "openrouter", Decimal("5.00"), "live_price", "medium"),
            ],
            excluded=[],
        )

    def test_accepts_within_tolerance(self) -> None:
        route, reason = ms.recheck_jev_choice("b", self._ranking())
        self.assertEqual(route, "b")
        self.assertEqual(reason, "within_tolerance_of_best")

    def test_rejects_outside_tolerance_keeps_scorer_pick(self) -> None:
        route, reason = ms.recheck_jev_choice("c", self._ranking())
        self.assertEqual(route, "a")
        self.assertEqual(reason, "outside_tolerance_kept_scorer_pick")

    def test_rejects_ineligible_choice(self) -> None:
        route, reason = ms.recheck_jev_choice("not-a-route", self._ranking())
        self.assertEqual(route, "a")
        self.assertEqual(reason, "jev_choice_not_eligible")


class JevRequestTests(unittest.TestCase):
    def test_payload_excludes_task_text(self) -> None:
        result = ms.rank_candidates(
            snapshot=FIXTURE_SNAPSHOT,
            available_hosts={"claude": True, "codex": False},
            openrouter_available=False,
            profile=_profile(),
        )
        payload = ms.jev_request(result, _profile())
        self.assertEqual(set(payload.keys()), {"task_family", "required_capabilities", "quality_floor", "candidates"})
        serialized = json.dumps(payload)
        self.assertNotIn("task_text", serialized)


class TransportConsentTests(unittest.TestCase):
    def test_transport_not_called_without_consent(self) -> None:
        called = []

        def transport(payload):
            called.append(payload)
            return {"route_id": "a"}

        with self.assertRaises(ms.ModelSelectError):
            ms.call_jev({"x": 1}, transport=transport, consent=False)
        self.assertEqual(called, [])

    def test_transport_called_with_explicit_consent(self) -> None:
        called = []

        def transport(payload):
            called.append(payload)
            return {"route_id": "a"}

        response = ms.call_jev({"x": 1}, transport=transport, consent=True)
        self.assertEqual(response, {"route_id": "a"})
        self.assertEqual(called, [{"x": 1}])


class HostDetectionTests(unittest.TestCase):
    def test_detect_available_hosts_uses_injectable_which(self) -> None:
        hosts = ms.detect_available_hosts(which=lambda name: "/usr/bin/" + name if name == "claude" else None)
        self.assertTrue(hosts["claude"])
        self.assertFalse(hosts["codex"])

    def test_openrouter_present_uses_env_backend(self) -> None:
        import os

        env_name = "SIDE_LANE_CREDENTIAL_" + ms.OPENROUTER_CREDENTIAL_SERVICE.upper().replace("-", "_")
        old = os.environ.pop(env_name, None)
        old_backend = os.environ.get("SIDE_LANE_CREDENTIAL_BACKEND")
        try:
            os.environ["SIDE_LANE_CREDENTIAL_BACKEND"] = "env"
            self.assertFalse(ms.openrouter_present())
            os.environ[env_name] = "present-value"
            self.assertTrue(ms.openrouter_present())
        finally:
            os.environ.pop(env_name, None)
            if old is not None:
                os.environ[env_name] = old
            os.environ.pop("SIDE_LANE_CREDENTIAL_BACKEND", None)
            if old_backend is not None:
                os.environ["SIDE_LANE_CREDENTIAL_BACKEND"] = old_backend


class CliTests(unittest.TestCase):
    def test_cli_prints_ranked_json_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            profile_path = home / "task.json"
            profile_path.write_text(
                json.dumps({"task_family": "general_coding_execution", "input_tokens": 100, "output_tokens": 10}),
                encoding="utf-8",
            )
            snapshot_path = home / "snapshot.json"
            snapshot_path.write_text(json.dumps(FIXTURE_SNAPSHOT), encoding="utf-8")

            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(home),
                "SIDE_LANE_MODEL_SELECT_SNAPSHOT_PATH": str(snapshot_path),
                "SIDE_LANE_CREDENTIAL_BACKEND": "env",
                "SIDE_LANE_CLAUDE_EXECUTABLE": "",
                "SIDE_LANE_CODEX_EXECUTABLE": "",
            }
            result = subprocess.run(
                [sys.executable, "-m", "side_lane.model_select", "--profile", str(profile_path)],
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["snapshot_id"], "model-select-test-fixture")
            # No hosts and no OpenRouter key configured in this hermetic env.
            self.assertEqual(payload["ranked"], [])


if __name__ == "__main__":
    unittest.main()
