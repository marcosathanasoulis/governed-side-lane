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
        self.assertEqual(d["model"], "claude-sonnet-5-5")
        self.assertNotIn("selection", d)  # no key: tier ladder plus staffing menu

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

    def test_extra_usage_with_key_goes_to_auto_and_claude_competes_on_metered_price(self):
        i = ar.Inventory({"claude": True, "codex": False}, {"claude": "extra-usage"}, True)
        d = self.decide(i)
        self.assertEqual(d["action"], "auto")
        self.assertTrue(d["metered"])
        self.assertNotIn("anthropic/*", d["turn_settings"]["excluded_models"])
        self.assertNotIn("excluded_providers", d["turn_settings"])
        self.assertEqual(d["handoff"]["native"], {})  # nothing included to hand off to

    def test_included_with_key_lets_auto_pick_inside_the_included_providers(self):
        i = ar.Inventory({"claude": True, "codex": False}, {"claude": "included-oauth"}, True)
        d = self.decide(i)
        self.assertEqual(d["action"], "native")
        allowed = d["selection"]["turn_settings"]["allowed_models"]
        self.assertEqual(set(allowed), set(ar.build_handoff(["claude"], "medium")["native"]))
        self.assertTrue(all(m.startswith("anthropic/") for m in allowed))

    def test_the_tier_caps_which_native_models_the_router_may_pick(self):
        both = ["claude", "codex"]
        low = set(ar.build_handoff(both, "low")["native"])
        self.assertEqual(low, {"anthropic/claude-haiku-5.5", "openai/gpt-6-luna"})
        high = set(ar.build_handoff(both, "high")["native"])
        self.assertIn("anthropic/claude-opus-5.5", high)
        self.assertNotIn("anthropic/claude-fable-5.1", high)
        self.assertNotIn("openai/gpt-6-astra", high)
        top = set(ar.build_handoff(both, "max")["native"])
        self.assertIn("anthropic/claude-fable-5.1", top)
        self.assertIn("openai/gpt-6-astra", top)

    def test_devin_only_runs_its_own_tier_ladder(self):
        i = ar.Inventory({"claude": False, "codex": False, "devin": True}, {}, False)
        d = self.decide(i)
        self.assertEqual((d["action"], d["host"], d["model"]), ("native", "devin", "swe-2-medium"))

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
        self.assertEqual(self.decide(i, "use the cheapest model")["model"], "claude-haiku-5-5")
        self.assertEqual(self.decide(i, "use the best model please")["model"], "claude-fable-5-1")
        c = inv(codex=True, usage={"codex": "included-oauth"})
        self.assertEqual(self.decide(c)["model"], "gpt-6.1-sol")  # astra only when really needed
        self.assertEqual(self.decide(c, "use the best model please")["model"], "gpt-6-astra")
        self.assertEqual(self.decide(c, "use the cheapest model")["model"], "gpt-6-luna")

    def test_pin_wins_and_respects_access(self):
        i = inv(claude=True, usage={"claude": "included-oauth"})
        self.assertEqual(self.decide(i, pinned_model="claude-opus-5")["action"], "pinned")
        self.assertEqual(self.decide(i, pinned_model="gpt-6.1-sol")["action"], "blocked")
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
        self.assertEqual(models, ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"])
        self.assertTrue(all(c["strengths"] and c["weaknesses"] for c in d["staffing"]["candidates"]))
        self.assertEqual(d["staffing"]["default"], d["model"])

    def test_both_hosts_list_both_ladders(self):
        d = self.native(codex=True)
        self.assertIn("gpt-6-astra", [c["model"] for c in d["staffing"]["candidates"]])

    def test_valid_choice_accepted_and_unlisted_refused(self):
        d = self.native()
        self.assertEqual(ar.validate_choice(d, "claude-opus-5-5")["model"], "claude-opus-5-5")
        with self.assertRaises(ar.AutoRouteError):
            ar.validate_choice(d, "gpt-6.1-sol")  # codex not installed
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

    def included_decision(self, devin=False, tier=None):
        i = ar.Inventory({"claude": True, "codex": False, "devin": devin},
                         {"claude": "included-oauth"}, True)
        return ar.decide(i, "summarize this", now=NOW, tier_override=tier)

    def extra_with_devin(self):
        i = ar.Inventory({"claude": True, "codex": False, "devin": True}, {"claude": "extra-usage"}, True)
        return ar.decide(i, "summarize this", now=NOW)

    def test_body_is_tiny_and_carries_policy(self):
        body = ar.build_probe_body(self.auto_decision()["turn_settings"], "x" * 5000)
        self.assertEqual(body["model"], "openrouter/auto")
        self.assertEqual(body["max_tokens"], ar.PROBE_MAX_TOKENS)
        self.assertEqual(len(body["messages"][0]["content"]), ar.PROBE_DIGEST_MAX_CHARS)
        self.assertEqual(body["plugins"][0]["id"], "auto-router")
        self.assertIn("openrouter/*", body["plugins"][0]["excluded_models"])

    def test_served_model_becomes_exact_route_and_key_goes_only_to_transport(self):
        calls = []

        def transport(body, headers):
            calls.append((body, headers))
            return {"model": "deepseek/deepseek-v3.2", "usage": {"cost": 0.0001}}

        keys = []
        out = ar.probe_auto_router(
            self.auto_decision(), "digest", read_key=lambda: keys.append(1) or "k", transport=transport)
        self.assertEqual(out["route"]["action"], "openrouter-exact")
        self.assertEqual(out["route"]["model"], "deepseek/deepseek-v3.2")
        self.assertTrue(out["route"]["metered"])
        self.assertEqual(out["cost_usd"], 0.0001)
        self.assertEqual(keys, [])  # injected transport never needed the key
        self.assertNotIn("Authorization", calls[0][1])

    def test_served_model_outside_pool_is_refused(self):
        with self.assertRaises(ar.AutoRouteError):
            ar.probe_auto_router(
                self.auto_decision(), "d", read_key=lambda: "k",
                transport=lambda b, h: {"model": "openai/text-embedding-3-large"})
        with self.assertRaises(ar.AutoRouteError):
            ar.probe_auto_router(
                self.auto_decision(), "d", read_key=lambda: "k",
                transport=lambda b, h: {"model": "openrouter/auto"})

    def test_included_probe_hands_off_to_the_native_model(self):
        d = self.included_decision(tier="high")
        out = ar.probe_auto_router(d, "x", read_key=lambda: "k",
                                   transport=lambda b, h: {"model": "anthropic/claude-opus-5.5"})
        self.assertEqual((out["route"]["action"], out["route"]["host"], out["route"]["model"], out["route"]["metered"]),
                         ("native-handoff", "claude", "claude-opus-5-5", False))

    def test_included_probe_refuses_a_model_outside_the_included_providers(self):
        d = self.included_decision()
        with self.assertRaises(ar.AutoRouteError):
            ar.probe_auto_router(d, "x", read_key=lambda: "k",
                                 transport=lambda b, h: {"model": "deepseek/deepseek-v3.2"})

    def test_very_high_coding_models_map_to_the_devin_equivalent(self):
        i = ar.Inventory({"claude": True, "codex": False, "devin": True}, {"claude": "extra-usage"}, True)
        d = ar.decide(i, "summarize this", now=NOW, tier_override="xhigh")
        for served, expected in (("anthropic/claude-fable-5.1", "swe-2-max"),
                                 ("openai/gpt-6-astra", "swe-2-max"),
                                 ("anthropic/claude-opus-5.5", "swe-2-high")):
            out = ar.probe_auto_router(d, "x", read_key=lambda: "k", transport=lambda b, h, m=served: {"model": m})
            self.assertEqual((out["route"]["action"], out["route"]["model"], out["route"]["metered"]),
                             ("devin-equivalent", expected, False), served)
        other = ar.probe_auto_router(d, "x", read_key=lambda: "k",
                                     transport=lambda b, h: {"model": "deepseek/deepseek-v3.2"})
        # Devin is the included host, so a non-very-high pick stays on Devin, not metered.
        self.assertEqual((other["route"]["action"], other["route"]["model"], other["route"]["metered"]),
                         ("devin-default", "swe-2-max", False))

    def test_plain_gpt_6_sol_maps_to_devin_medium_when_nothing_is_included(self):
        d = self.extra_with_devin()
        out = ar.probe_auto_router(d, "x", read_key=lambda: "k",
                                   transport=lambda b, h: {"model": "openai/gpt-6-sol"})
        self.assertEqual((out["route"]["action"], out["route"]["model"]), ("devin-equivalent", "swe-2-medium"))

    def test_devin_needs_a_signed_in_cli(self):
        class Hosts:
            @staticmethod
            def resolve_host_executable(name):
                return None
        self.assertFalse(ar.devin_ready(Hosts))

    def test_no_devin_means_no_devin_equivalent(self):
        d = self.auto_decision()
        out = ar.probe_auto_router(d, "x", read_key=lambda: "k",
                                   transport=lambda b, h: {"model": "anthropic/claude-fable-5.1"})
        self.assertEqual(out["route"]["action"], "openrouter-exact")

    def test_probe_requires_auto_decision(self):
        with self.assertRaises(ar.AutoRouteError):
            ar.probe_auto_router({"action": "native"}, "d", read_key=lambda: "k")


class GlmGeminiAndNodesTests(unittest.TestCase):
    def test_glm_and_gemini_picks_run_on_their_own_plans(self):
        handoff = ar.build_handoff(["glm", "gemini"], "low")
        glm = ar.resolve_served(ar.build_handoff(["glm", "gemini"], "medium"), "z-ai/glm-5.3")
        self.assertEqual((glm["action"], glm["host"], glm["model"], glm["metered"]), ("native-handoff", "glm", "glm-5.3", False))
        gem = ar.resolve_served(handoff, "google/gemini-3.8-flash")
        self.assertEqual((gem["host"], gem["model"]), ("gemini", "gemini-3.8-flash"))
        # no other Google model is substituted with Flash
        self.assertEqual(ar.resolve_served(handoff, "google/gemini-3.5-flash")["action"], "openrouter-exact")
        self.assertEqual(ar.resolve_served(handoff, "google/gemini-3.1-pro-preview")["action"], "openrouter-exact")
        # Flash is a low-tier model: it is not handed off at any other tier
        for tier in ("medium", "high", "xhigh", "max"):
            above = ar.build_handoff(["glm", "gemini"], tier)
            self.assertEqual(ar.resolve_served(above, "google/gemini-3.8-flash")["action"], "openrouter-exact", tier)
        self.assertEqual(ar.resolve_served(handoff, "deepseek/deepseek-v4.1-flash")["action"], "openrouter-exact")
        # without the plans a glm pick is just a metered OpenRouter model
        self.assertEqual(ar.resolve_served(ar.build_handoff(["claude"], "medium"), "z-ai/glm-5.3")["action"], "openrouter-exact")

    def test_glm_only_pool_is_limited_to_glm_models(self):
        i = ar.Inventory({"claude": False, "codex": False, "glm": True}, {"glm": "included-oauth"}, True)
        d = ar.decide(i, "x", now=NOW)
        self.assertEqual((d["host"], d["model"]), ("glm", "glm-5.3"))
        self.assertEqual(d["selection"]["turn_settings"]["allowed_models"], ["z-ai/glm-*"])

    def test_pins_for_glm_and_gemini(self):
        i = ar.Inventory({"glm": True, "gemini": True}, {"glm": "included-oauth", "gemini": "included-oauth"}, False,
                         probed=frozenset({"gemini"}))
        self.assertEqual(ar.decide(i, "x", pinned_model="glm-5.3", now=NOW)["action"], "pinned")
        self.assertEqual(ar.decide(i, "x", pinned_model="gemini-3.8-flash", now=NOW)["host"], "gemini")
        # a paid-by-default plan that is not declared/probed needs the usual explicit authorization for a pin
        bare = ar.Inventory({"glm": True, "gemini": True}, {}, False)
        for model in ("glm-5.3", "gemini-3.8-flash"):
            self.assertEqual(ar.decide(bare, "x", pinned_model=model, now=NOW)["action"], "blocked", model)
        host = "gemini"
        authorized = ar.decide(bare, "x", pinned_model="gemini-3.8-flash", authorize_extra_hosts=[host], now=NOW)
        self.assertEqual((authorized["action"], authorized["metered"]), ("pinned", True))
    def test_gemini_is_only_offered_at_the_low_tier(self):
        i = ar.Inventory({"claude": False, "codex": False, "gemini": True}, {"gemini": "included-oauth"}, False,
                         probed=frozenset({"gemini"}))
        low = ar.decide(i, "use the cheapest model", now=NOW)
        self.assertEqual((low["action"], low["host"], low["model"]), ("native", "gemini", "gemini-3.8-flash"))
        for words in ("use the best model", "fix the failing test"):
            above = ar.decide(i, words, now=NOW)
            self.assertNotEqual(above.get("host"), "gemini", words)
            self.assertEqual(above["action"], "blocked", words)
        self.assertEqual(ar.NATIVE_LADDER["gemini"], {"low": "gemini-3.8-flash"})

    def test_gemini_and_glm_are_never_automatic_without_a_declaration_and_gemini_needs_a_probe(self):
        for hosts, usage in (({"claude": False, "codex": False, "gemini": True}, {}),
                             ({"claude": False, "codex": False, "gemini": True}, {"gemini": "included-oauth"}),  # not probed
                             ({"claude": False, "codex": False, "glm": True}, {})):
            d = ar.decide(ar.Inventory(hosts, usage, False), "use the cheapest model", now=NOW)
            self.assertNotIn(d.get("host"), ("gemini", "glm"), (hosts, usage))
            self.assertEqual(d["action"], "blocked", (hosts, usage))
        glm = ar.decide(ar.Inventory({"claude": False, "codex": False, "glm": True}, {"glm": "included-oauth"}, False),
                        "fix the failing test", now=NOW)
        self.assertEqual((glm["host"], glm["model"]), ("glm", "glm-5.3"))  # declared GLM is automatic (medium)

    def test_the_ladder_is_the_shared_mapping_so_the_two_cannot_disagree(self):
        from side_lane.model_selection import load
        table = load()
        for host, ladder in ar.NATIVE_LADDER.items():
            for tier, model in ladder.items():
                src = "xhigh" if tier == "max" else tier
                first = next(c for c in table.for_tier(src) if c.provider == host and (c.plan == "included" or c.declarable_included))
                self.assertEqual(model, first.model, (host, tier))
        self.assertEqual(ar.NATIVE_LADDER["glm"], {"medium": "glm-5.3"})
        self.assertEqual(ar.NATIVE_LADDER["devin"], {"medium": "swe-2-medium", "high": "swe-2-high",
                                                     "xhigh": "swe-2-max", "max": "swe-2-max"})
        self.assertNotIn("low", ar.NATIVE_LADDER["devin"])
        self.assertEqual(ar.NATIVE_LADDER["claude"]["xhigh"], "claude-fable-5-1")
        self.assertEqual(ar.NATIVE_LADDER["codex"]["xhigh"], "gpt-6-astra")
        self.assertEqual(ar.NATIVE_MIN_TIER["claude-fable-5-1"], "xhigh")
        self.assertEqual(ar.PROBE_REQUIRED_HOSTS, frozenset({"gemini"}))
        self.assertEqual(ar.DECLARABLE_HOSTS, frozenset({"gemini", "glm"}))
        self.assertEqual({k: v for k, v in ar.OPENROUTER_TO_NATIVE.items() if v[0] == "gemini"},
                         {"google/gemini-3.8-flash": ("gemini", "gemini-3.8-flash")})

    def test_the_ladder_only_names_models_the_catalog_can_launch(self):
        import json
        from pathlib import Path
        catalog = json.loads((Path(__file__).parents[1] / "config" / "models.json").read_text())
        launchable = {m for p in catalog.get("providers", catalog).values() for mode in p.get("routes", {}).values()
                      for host in mode.values() for m in host.get("models", [])}
        for host, ladder in ar.NATIVE_LADDER.items():
            if host in ("claude", "codex"):  # devin and gemini routes are not in the public governed catalog
                for model in ladder.values():
                    self.assertIn(model, launchable, f"{host}: {model}")

    def test_nodes_file_validation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "n.json"
            for bad in ([], [{"id": "a"}], [{"id": "a", "task": "x"}, {"id": "a", "task": "y"}], {"id": "a"}):
                path.write_text(json.dumps(bad))
                with self.assertRaises(ar.AutoRouteError):
                    ar.load_nodes(path)
            path.write_text(json.dumps([{"id": "a", "task": "x"}]))
            self.assertEqual(ar.load_nodes(path)[0]["id"], "a")

    def test_each_node_is_routed_on_its_own(self):
        import argparse
        import contextlib
        import io
        import tempfile
        parser = argparse.ArgumentParser()
        ar.add_arguments(parser)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "n.json"
            path.write_text(json.dumps([
                {"id": "small", "task": "use the cheapest model to rename a variable"},
                {"id": "big", "task": "use the best model to design a storage layer"},
            ]))
            args = parser.parse_args(["--nodes", str(path)])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ar.run(args, inventory=inv(claude=True, usage={"claude": "included-oauth"}),
                            read_key=lambda: "k")
        self.assertEqual(rc, 0)
        nodes = {n["id"]: n["decision"] for n in json.loads(out.getvalue())["nodes"]}
        self.assertEqual(nodes["small"]["model"], "claude-haiku-5-5")
        self.assertEqual(nodes["big"]["model"], "claude-fable-5-1")

    def test_task_and_nodes_are_mutually_exclusive(self):
        import argparse
        parser = argparse.ArgumentParser()
        ar.add_arguments(parser)
        self.assertEqual(ar.run(parser.parse_args([]), inventory=inv(), read_key=lambda: "k"), 2)
        self.assertEqual(ar.run(parser.parse_args(["--task", "x", "--nodes", "/tmp/x.json"]),
                                inventory=inv(), read_key=lambda: "k"), 2)


class DifficultyTests(unittest.TestCase):
    def test_tier_from_words_only_when_the_request_asks(self):
        self.assertEqual(ar.tier_from_words("please use the best model"), "max")
        self.assertEqual(ar.tier_from_words("use the cheapest model"), "low")
        self.assertIsNone(ar.tier_from_words("fix the failing test"))

    def test_rate_difficulty_returns_the_tier_and_sends_only_the_digest(self):
        seen = {}

        def transport(body):
            seen["body"] = body
            return {"answers": {"tier": {"choice": "high", "confidence": 0.8}}, "usage": {"cost": 0.00002}}

        out = ar.rate_difficulty("x" * 5000, read_key=lambda: "k", transport=transport)
        self.assertEqual((out["tier"], out["cost_usd"]), ("high", 0.00002))
        self.assertEqual(len(seen["body"]["state"]["task"]), ar.PROBE_DIGEST_MAX_CHARS)
        self.assertNotIn("Bearer", json.dumps(seen["body"]))

    def test_rate_difficulty_refuses_a_malformed_answer(self):
        with self.assertRaises(ar.AutoRouteError):
            ar.rate_difficulty("x", read_key=lambda: "k", transport=lambda b: {"answers": {"tier": {"choice": "huge"}}})

    def test_tier_override_drives_the_native_ladder_and_the_probe(self):
        i = inv(claude=True, codex=True, openrouter=True, usage={"claude": "included-oauth", "codex": "included-oauth"})
        d = ar.decide(i, "a task", now=NOW, tier_override="max")
        self.assertEqual(d["model"], "claude-fable-5-1")
        self.assertEqual(d["selection"]["turn_settings"]["cost_tier"], "max")
        with self.assertRaises(ar.AutoRouteError):
            ar.decide(i, "a task", now=NOW, tier_override="huge")


class PlansTests(unittest.TestCase):
    def test_example_plans_file_is_valid(self):
        path = ROOT / "config" / "examples" / "declared-plans.example.json"
        plans = policy.load_declared_plans(path)
        self.assertEqual({p.provider for p in plans}, {"claude", "codex"})

    def test_bad_usage_state_rejected(self):
        with self.assertRaises(ar.AutoRouteError):
            ar.Inventory({"claude": True}, {"claude": "free"})


class SparseTierRegressionTests(unittest.TestCase):
    def test_handoff_respects_tier(self):
        low = ar.build_handoff(["glm", "devin"], "low")
        self.assertEqual(low["patterns"], [])
        self.assertEqual(low["devin"], [])
        high = ar.build_handoff(["devin"], "high")
        self.assertNotIn("swe-2-max", [e["model"] for e in high["devin"]])

    def test_authorized_extra_skips_hosts_without_tier_model(self):
        inv = ar.Inventory({"gemini": True}, {}, False)
        out = ar.decide(inv, "task", authorize_extra_hosts=["gemini"], plans=())
        self.assertEqual(out["action"], "blocked")


if __name__ == "__main__":
    unittest.main()
