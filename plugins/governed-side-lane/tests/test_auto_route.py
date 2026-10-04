from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from side_lane import auto_route as ar  # noqa: E402
from side_lane import auto_route_policy as policy  # noqa: E402

NOW = 1_800_000_000.0


def inv(claude=False, codex=False, openrouter=False, usage=None):
    return ar.Inventory(
        hosts={"claude": claude, "codex": codex},
        host_usage=usage or {},
        openrouter=openrouter,
    )


class DecideTests(unittest.TestCase):
    def decide(self, inventory, query="fix the failing test", **kw):
        return ar.decide(inventory, query, now=NOW, **kw)

    def test_included_claude_runs_native_without_a_key(self):
        d = self.decide(inv(claude=True, usage={"claude": "included-oauth"}))
        self.assertEqual((d["action"], d["host"], d["metered"]), ("native", "claude", False))
        self.assertEqual(d["model"], "claude-sonnet-5")

    def test_codex_only_works(self):
        d = self.decide(inv(codex=True))
        self.assertEqual((d["action"], d["host"]), ("native", "codex"))

    def test_unknown_usage_is_included_publicly_and_excluded_privately(self):
        self.assertEqual(self.decide(inv(claude=True))["action"], "native")
        d = self.decide(inv(claude=True), unknown_usage="excluded")
        self.assertEqual(d["action"], "blocked")

    def test_extra_usage_is_never_used_silently(self):
        d = self.decide(ar.Inventory({"claude": True, "codex": False}, {"claude": "extra-usage"}, False))
        self.assertEqual(d["action"], "blocked")
        self.assertTrue(any("extra usage" in s for s in d["next_steps"]))

    def test_extra_usage_with_key_goes_to_auto_with_provider_excluded(self):
        i = ar.Inventory({"claude": True, "codex": False}, {"claude": "extra-usage"}, True)
        d = self.decide(i)
        self.assertEqual(d["action"], "auto")
        self.assertTrue(d["metered"])
        self.assertIn("anthropic/*", d["turn_settings"]["excluded_models"])
        self.assertEqual(d["turn_settings"]["excluded_providers"], ["claude"])

    def test_explicit_extra_authorization_allows_native_when_no_key(self):
        i = ar.Inventory({"claude": True, "codex": False}, {"claude": "extra-usage"}, False)
        d = self.decide(i, authorize_extra_hosts=["claude"])
        self.assertEqual((d["action"], d["metered"]), ("native", True))

    def test_included_beats_openrouter(self):
        d = self.decide(inv(claude=True, openrouter=True, usage={"claude": "included-oauth"}))
        self.assertEqual(d["action"], "native")

    def test_openrouter_only_uses_auto(self):
        d = self.decide(inv(openrouter=True))
        self.assertEqual(d["action"], "auto")
        policy.parse_turn_settings(d["turn_settings"])  # valid for the shared core

    def test_nothing_installed_is_blocked_with_setup_steps(self):
        d = self.decide(inv())
        self.assertEqual(d["action"], "blocked")
        self.assertTrue(any("install and sign in" in s for s in d["next_steps"]))
        self.assertTrue(any("OpenRouter" in s for s in d["next_steps"]))

    def test_cost_tier_picks_native_model_and_words_override(self):
        i = inv(claude=True, usage={"claude": "included-oauth"})
        self.assertEqual(self.decide(i, "use the cheapest model")["model"], "claude-haiku-4-5-20251001")
        self.assertEqual(self.decide(i, "use the best model please")["model"], "claude-opus-5")

    def test_pin_wins_and_respects_access(self):
        i = inv(claude=True, usage={"claude": "included-oauth"})
        self.assertEqual(self.decide(i, pinned_model="claude-opus-5")["action"], "pinned")
        self.assertEqual(self.decide(i, pinned_model="gpt-6-sol")["action"], "blocked")
        self.assertEqual(self.decide(i, pinned_model="deepseek/deepseek-v3.2")["action"], "blocked")
        d = self.decide(inv(openrouter=True), pinned_model="deepseek/deepseek-v3.2")
        self.assertEqual((d["action"], d["metered"]), ("pinned", True))

    def test_sensitive_service_forces_zdr(self):
        d = self.decide(inv(openrouter=True), service_names=["bigquery"])
        self.assertTrue(d["turn_settings"]["require_zdr"])

    def test_declared_exhausted_plan_file_blocks_native(self):
        plans = policy.parse_declared_plans({
            "schema_version": 1,
            "plans": [{"provider": "claude", "mode": "included", "quota_status": "exhausted",
                       "model_patterns": ["anthropic/*"]}],
        })
        d = self.decide(inv(claude=True), plans=plans)
        self.assertEqual(d["action"], "blocked")

    def test_receipt_has_no_secret_shaped_fields(self):
        d = self.decide(inv(claude=True, openrouter=True))
        text = json.dumps(d)
        self.assertNotIn("sk-", text)
        self.assertTrue(d["receipt"]["inventory"]["openrouter_key_present"])


class StaffingTests(unittest.TestCase):
    def native(self, **kw):
        return ar.decide(inv(claude=True, codex=kw.get("codex", False)), "refactor module", now=NOW)

    def test_candidates_cover_only_reachable_included_hosts_with_notes(self):
        d = self.native()
        models = [c["model"] for c in d["staffing"]["candidates"]]
        self.assertEqual(models, ["claude-haiku-4-5-20251001", "claude-sonnet-5", "claude-opus-5"])
        self.assertTrue(all(c["strengths"] and c["weaknesses"] for c in d["staffing"]["candidates"]))
        self.assertEqual(d["staffing"]["default"], d["model"])

    def test_both_hosts_list_both_ladders(self):
        d = self.native(codex=True)
        self.assertIn("gpt-6-sol", [c["model"] for c in d["staffing"]["candidates"]])

    def test_valid_choice_accepted_and_unlisted_refused(self):
        d = self.native()
        self.assertEqual(ar.validate_choice(d, "claude-opus-5")["model"], "claude-opus-5")
        with self.assertRaises(ar.AutoRouteError):
            ar.validate_choice(d, "gpt-6-sol")  # codex not installed
        with self.assertRaises(ar.AutoRouteError):
            ar.validate_choice({"action": "blocked"}, "claude-opus-5")

    def test_extra_usage_native_has_no_staffing_menu(self):
        i = ar.Inventory({"claude": True, "codex": False}, {"claude": "extra-usage"}, False)
        d = ar.decide(i, "x", authorize_extra_hosts=["claude"], now=NOW)
        self.assertNotIn("staffing", d)


class ProbeTests(unittest.TestCase):
    def auto_decision(self):
        i = ar.Inventory({"claude": True, "codex": False}, {"claude": "extra-usage"}, True)
        return ar.decide(i, "summarize this", now=NOW)

    def test_body_is_tiny_and_carries_policy(self):
        body = ar.build_probe_body(self.auto_decision()["turn_settings"], "x" * 5000)
        self.assertEqual(body["model"], "openrouter/auto")
        self.assertEqual(body["max_tokens"], ar.PROBE_MAX_TOKENS)
        self.assertEqual(len(body["messages"][0]["content"]), ar.PROBE_DIGEST_MAX_CHARS)
        self.assertEqual(body["plugins"][0]["id"], "auto-router")
        self.assertIn("anthropic/*", body["plugins"][0]["excluded_models"])

    def test_served_model_becomes_exact_route_and_key_goes_only_to_transport(self):
        calls = []

        def transport(body, headers):
            calls.append((body, headers))
            return {"model": "deepseek/deepseek-v3.2", "usage": {"cost": 0.0001}}

        keys = []
        out = ar.probe_auto_router(
            self.auto_decision(), "digest", read_key=lambda: keys.append(1) or "k", transport=transport)
        self.assertEqual(out["route"], {"action": "openrouter-exact", "model": "deepseek/deepseek-v3.2"})
        self.assertEqual(out["cost_usd"], 0.0001)
        self.assertEqual(keys, [])  # injected transport never needed the key
        self.assertNotIn("Authorization", calls[0][1])

    def test_served_model_outside_pool_is_refused(self):
        with self.assertRaises(ar.AutoRouteError):
            ar.probe_auto_router(
                self.auto_decision(), "d", read_key=lambda: "k",
                transport=lambda b, h: {"model": "anthropic/claude-sonnet-5"})
        with self.assertRaises(ar.AutoRouteError):
            ar.probe_auto_router(
                self.auto_decision(), "d", read_key=lambda: "k",
                transport=lambda b, h: {"model": "openrouter/auto"})

    def test_probe_requires_auto_decision(self):
        with self.assertRaises(ar.AutoRouteError):
            ar.probe_auto_router({"action": "native"}, "d", read_key=lambda: "k")


class PlansTests(unittest.TestCase):
    def test_example_plans_file_is_valid(self):
        path = ROOT / "config" / "examples" / "declared-plans.example.json"
        plans = policy.load_declared_plans(path)
        self.assertEqual({p.provider for p in plans}, {"claude", "codex"})

    def test_bad_usage_state_rejected(self):
        with self.assertRaises(ar.AutoRouteError):
            ar.Inventory({"claude": True}, {"claude": "free"})


if __name__ == "__main__":
    unittest.main()
