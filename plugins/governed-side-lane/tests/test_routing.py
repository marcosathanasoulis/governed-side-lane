from datetime import date
import copy
import os
import json
from pathlib import Path
import tempfile
import unittest

from side_lane import routing

TODAY = date(2026, 8, 29)


def route(route_id: str, model: str, score: int, price: float, *, provider: str = "openai", host: str = "codex", vendor: str = "openai", explicit: bool = False) -> dict[str, object]:
    mode = "execute"
    gateway = "direct-zai" if provider == "glm" else ("native-codex" if host == "codex" else "native-claude")
    protocol = "anthropic-compatible" if provider == "glm" else ("native-codex" if host == "codex" else "native-claude")
    return {"id": route_id, "provider": provider, "gateway": gateway,
        "auth_method": "provider-key" if explicit else "oauth", "billable": explicit,
        "explicit_only": explicit, "model_vendor": vendor, "model": model, "host": host,
        "mode": mode, "state": "executable", "execution_allowlisted": True, "protocol": protocol,
        "execution_location": "local-user-workspace",
        "connector_retention": "worker-host", "connector_evidence": {"verified": True, "verified_on": "2026-08-15", "source": "fixture"},
        "privacy_classes": ["ordinary"], "capabilities": {"supported": ["workspace-write", "gitnexus"], "authority_roles": ["worker"]},
        "cost_model": ({"basis": "prepaid-flat-rate", "verified_on": "2026-08-15", "source": "fixture"} if explicit else
            {"basis": "native-oauth", "verified_on": "2026-08-15", "source": "fixture", "rates": {"unit": "usd", "input_per_million": price, "output_per_million": price * 2}}),
        "behavioral_capabilities": {"architecture": {"score": score, "evidence_types": ["community-signal", "local-evaluation"]}},
        "community_signal_refs": [f"{route_id}-community"],
        "local_evaluation": {"verified": True, "verified_on": "2026-08-15", "source": f"{route_id} eval", "acceptance_rate": 0.8, "task_scores": {"complex": score}}}


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = {"catalog_version": "fixture", "catalog_verified_on": "2026-08-15",
            "freshness_days": {"price": 30, "evidence": 60}, "routes": [
                route("terra", "gpt-5.6-terra", 92, 2), route("sol", "gpt-5.6-sol", 97, 8),
                route("fable", "claude-fable-5", 99, 10, provider="claude", host="claude", vendor="claude"),
                route("glm", "glm-5.3", 95, 0.5, provider="glm", host="claude", vendor="glm", explicit=True)]}
        self.allowlist = frozenset((item["provider"], item["host"], item["mode"], item["model"]) for item in self.catalog["routes"])

    def profile(self, **overrides: object) -> dict[str, object]:
        value: dict[str, object] = {"originating_host": "codex", "mode": "execute", "policy": "best-fit",
            "task_band": "complex", "quality_floor": 90, "required_connectors": [],
            "required_capabilities": [], "required_behavioral_capabilities": [],
            "host_capabilities": {"codex": {"available_connectors": [], "available_capabilities": ["workspace-write", "gitnexus"]},
                "claude": {"available_connectors": [], "available_capabilities": ["workspace-write", "gitnexus"]}},
            "input_tokens": 1000, "output_tokens": 100}
        value.update(overrides)
        return value

    def test_best_fit_and_cost_optimized_use_task_relative_floor(self) -> None:
        best = routing.recommend(self.catalog, self.profile(), runtime_allowlist=self.allowlist, credential_present_routes=self.allowlist, today=TODAY)
        cheap = routing.recommend(self.catalog, self.profile(policy="cost-optimized", host_cost_state={"codex": "extra-usage", "claude": "extra-usage"}), runtime_allowlist=self.allowlist, credential_present_routes=self.allowlist, today=TODAY)
        self.assertEqual(best["winner"]["route_id"], "fable")
        self.assertEqual(cheap["winner"]["route_id"], "terra")
        self.assertEqual(best["winner"]["gateway"], "native-claude")
        self.assertTrue(best["winner"]["connector_identity_changed"])

    def test_glm_requires_opt_in_then_flat_rate_can_rank_cheapest(self) -> None:
        result = routing.recommend(self.catalog, self.profile(prefer="glm"), runtime_allowlist=self.allowlist, credential_present_routes=self.allowlist, today=TODAY)
        self.assertIsNone(result["winner"])
        glm = next(item for item in result["exclusions"] if item["route_id"] == "glm")
        self.assertIn("explicit-opt-in-required", glm["reasons"])
        enabled = routing.recommend(self.catalog, self.profile(policy="cost-optimized", include_glm=True,
            glm_availability="available", host_cost_state={"codex": "extra-usage", "claude": "extra-usage"}),
            runtime_allowlist=self.allowlist, credential_present_routes=self.allowlist, today=TODAY)
        self.assertEqual(enabled["winner"]["route_id"], "glm")
        self.assertEqual(enabled["winner"]["estimated_cost"]["value"], 0)

    def test_quota_paused_glm_is_ineligible_without_fallback(self) -> None:
        result = routing.recommend(self.catalog, self.profile(prefer="glm", include_glm=True,
            glm_availability="temporarily-unavailable"), runtime_allowlist=self.allowlist,
            credential_present_routes=self.allowlist, today=TODAY)
        self.assertIsNone(result["winner"])
        glm = next(item for item in result["exclusions"] if item["route_id"] == "glm")
        self.assertIn("glm-temporarily-unavailable", glm["reasons"])

    def test_extra_usage_prefers_capability_matched_included_host(self) -> None:
        result = routing.recommend(self.catalog, self.profile(policy="cost-optimized",
            required_behavioral_capabilities=["architecture"],
            host_cost_state={"claude": "extra-usage", "codex": "included-oauth"}),
            runtime_allowlist=self.allowlist, credential_present_routes=self.allowlist, today=TODAY)
        self.assertEqual(result["winner"]["host"], "codex")
        self.assertTrue(result["winner"]["estimated_cost"]["incremental_zero"])

    def test_preferred_provider_pool_is_soft_and_applies_after_eligibility(self) -> None:
        preferred = routing.recommend(self.catalog,
            self.profile(preferred_provider_pool=["claude"]),
            runtime_allowlist=self.allowlist, credential_present_routes=self.allowlist,
            today=TODAY)
        self.assertEqual(preferred["winner"]["route_id"], "fable")
        self.assertTrue(preferred["preferred_provider_pool_applied"])
        self.assertEqual(preferred["preference_rationale"],
                         "qualified-route-in-preferred-provider-pool")
        fallback = routing.recommend(self.catalog,
            self.profile(preferred_provider_pool=["glm"]),
            runtime_allowlist=self.allowlist, credential_present_routes=self.allowlist,
            today=TODAY)
        self.assertEqual(fallback["winner"]["route_id"], "fable")
        self.assertTrue(fallback["preferred_provider_pool_fallback"])
        self.assertIn("preferred-provider-pool-fallback", fallback["reason_codes"])
        glm = next(item for item in fallback["exclusions"] if item["route_id"] == "glm")
        self.assertIn("explicit-opt-in-required", glm["reasons"])

    def test_preferred_provider_pool_accepts_provider_or_model_vendor_names(self) -> None:
        catalog = copy.deepcopy(self.catalog)
        catalog["routes"][0]["provider"] = "openai-gateway"
        allowlist = frozenset((item["provider"], item["host"], item["mode"], item["model"])
                              for item in catalog["routes"])
        by_provider = routing.recommend(catalog,
            self.profile(preferred_provider_pool=["openai-gateway"]),
            runtime_allowlist=allowlist, credential_present_routes=allowlist, today=TODAY)
        self.assertEqual([item["route_id"] for item in by_provider["ranked_routes"]], ["terra"])
        by_vendor = routing.recommend(catalog,
            self.profile(preferred_provider_pool=["openai"]),
            runtime_allowlist=allowlist, credential_present_routes=allowlist, today=TODAY)
        self.assertEqual([item["route_id"] for item in by_vendor["ranked_routes"]], ["sol", "terra"])

    def test_noncash_candidate_does_not_erase_comparable_cash_ranking(self) -> None:
        catalog = copy.deepcopy(self.catalog)
        catalog["routes"][0]["cost_model"]["rates"]["unit"] = "workspace-credit"
        result = routing.recommend(catalog, self.profile(policy="cost-optimized",
            host_cost_state={"codex": "extra-usage", "claude": "extra-usage"}),
            runtime_allowlist=self.allowlist, credential_present_routes=self.allowlist,
            today=TODAY)
        self.assertIsNotNone(result["winner"])
        self.assertNotEqual(result["winner"]["route_id"], "terra")
        terra = next(item for item in result["exclusions"] if item["route_id"] == "terra")
        self.assertIn("noncash-cost-unit", terra["reasons"])

    def test_latency_budget_uses_only_fresh_local_evaluation_median(self) -> None:
        catalog = copy.deepcopy(self.catalog)
        catalog["routes"][2]["local_evaluation"]["median_duration_ms"] = 2_000
        within = routing.recommend(catalog, self.profile(max_duration_ms=2_500),
            runtime_allowlist=self.allowlist, credential_present_routes=self.allowlist,
            today=TODAY)
        self.assertEqual(within["winner"]["route_id"], "fable")
        self.assertEqual(within["winner"]["median_duration_ms"], 2_000)
        too_slow = routing.recommend(catalog, self.profile(max_duration_ms=1_500),
            runtime_allowlist=self.allowlist, credential_present_routes=self.allowlist,
            today=TODAY)
        self.assertIsNone(too_slow["winner"])
        fable = next(item for item in too_slow["exclusions"] if item["route_id"] == "fable")
        self.assertIn("duration-budget-exceeded", fable["reasons"])

    def test_origin_host_connectors_allowlist_and_auth_presence_are_hard_gates(self) -> None:
        result = routing.recommend(self.catalog, self.profile(required_connectors=["gitnexus"]), runtime_allowlist=frozenset(), credential_present_routes=frozenset(), today=TODAY)
        terra = next(item for item in result["exclusions"] if item["route_id"] == "terra")
        self.assertIn("runtime-allowlist-mismatch", terra["reasons"])
        self.assertIn("credential-absent", terra["reasons"])
        self.assertIn("required-connector-unavailable-on-worker-host", terra["reasons"])

    def test_executable_route_requires_reviewed_evidence(self) -> None:
        broken = copy.deepcopy(self.catalog)
        broken["routes"][0].pop("cost_model")
        with self.assertRaisesRegex(routing.RoutingError, "reviewed cost model"):
            routing.validate_catalog(broken)

    def test_discovery_is_pure_and_nonactivating(self) -> None:
        before = copy.deepcopy(self.catalog)
        candidates = routing.discovery_review_candidates(self.catalog, [{"provider": "openai", "model": "future"}])
        self.assertEqual(candidates[0]["state"], "review-candidate")
        self.assertEqual(before, self.catalog)

    def test_catalog_candidates_are_read_only_and_default_disabled(self) -> None:
        catalog = copy.deepcopy(self.catalog)
        catalog["candidates"] = [{
            "id": "cloud-agent", "state": "candidate", "default_enabled": False,
            "provider": "example", "model_vendor": "example", "requested_model": "latest",
            "execution_location": "cloud-only", "qualification_state": "cloud-only-excluded",
            "model_evidence": {"verified": True, "verified_on": "2026-08-15", "source": "fixture"},
            "notes": "A hosted workspace is not a local side lane.",
        }]
        candidates = routing.list_catalog_candidates(catalog)
        self.assertEqual(candidates[0]["id"], "cloud-agent")
        self.assertFalse(candidates[0]["executable"])
        self.assertFalse(candidates[0]["runtime_allowlisted"])
        self.assertFalse(candidates[0]["credential_checked"])
        self.assertFalse(candidates[0]["authorization_checked"])
        self.assertNotIn("cloud-agent", {item["route_id"] for item in routing.recommend(
            catalog, self.profile(), runtime_allowlist=self.allowlist,
            credential_present_routes=self.allowlist, today=TODAY)["ranked_routes"]})
        collision = copy.deepcopy(catalog)
        collision["candidates"][0]["id"] = "terra"
        with self.assertRaisesRegex(routing.RoutingError, "duplicate candidate id"):
            routing.validate_catalog(collision)
        unsourced = copy.deepcopy(catalog)
        unsourced["candidates"][0].pop("model_evidence")
        with self.assertRaisesRegex(routing.RoutingError, "model evidence is not reviewed"):
            routing.validate_catalog(unsourced)

    def test_session_economics_counts_failed_correction_and_explicit_coordinator_cost(self) -> None:
        cost = routing.estimate_session_cost({
            "basis": "external-billable", "verified_on": "2026-08-15", "source": "fixture",
            "rates": {"unit": "usd", "input_per_million": 2, "cached_read_per_million": 0.5,
                "cache_write_per_million": 3, "output_per_million": 8},
            "acquisition": {"monthly_fee_usd": 20, "setup_cost_usd": 5, "expected_overflow_usd": 2},
        }, [
            {"kind": "failed-attempt", "uncached_input_tokens": 1_000_000, "output_tokens": 100_000, "tool_cost_usd": 0.25},
            {"kind": "correction", "cached_read_tokens": 2_000_000, "cache_write_tokens": 100_000,
                "output_tokens": 50_000, "host_cost_usd": 0.5},
            {"kind": "coordinator", "coordinator_cost_usd": 1.25},
        ], host_cost_state="extra-usage", now=TODAY, max_days=30, accepted_completions=2)
        self.assertIsNotNone(cost)
        self.assertEqual(cost["attempt_count"], 3)
        self.assertEqual(cost["tool_and_host_overhead_usd"], 0.75)
        self.assertEqual(cost["value"], 6.5)
        self.assertEqual(cost["expected_cost_per_accepted_result"], 3.25)
        self.assertEqual(cost["acquisition_cost"]["total_usd"], 27)

    def test_unknown_session_units_or_zero_accepted_completions_are_not_cost_optimized(self) -> None:
        broken = copy.deepcopy(self.catalog)
        broken["routes"][0]["cost_model"]["rates"]["unit"] = "workspace-credit"
        result = routing.recommend(broken, self.profile(policy="cost-optimized", host_cost_state={"codex": "extra-usage"},
            session_attempts=[{"kind": "primary", "uncached_input_tokens": 100, "tool_cost_usd": 1}]), runtime_allowlist=self.allowlist,
            credential_present_routes=self.allowlist, today=TODAY)
        terra = next(item for item in result["exclusions"] if item["route_id"] == "terra")
        self.assertIn("cost-basis-missing-or-stale", terra["reasons"])
        self.assertNotIn("accepted-completion-cost-unknown", terra["reasons"])
        zero = routing.recommend(self.catalog, self.profile(policy="cost-optimized", host_cost_state={"codex": "extra-usage"},
            accepted_completions=0, session_cost_basis="route-specific-cohort", cohort_route_id="terra"), runtime_allowlist=self.allowlist,
            credential_present_routes=self.allowlist, today=TODAY)
        zero_terra = next(item for item in zero["exclusions"] if item["route_id"] == "terra")
        self.assertIn("accepted-completion-cost-unknown", zero_terra["reasons"])

    def test_route_specific_cohort_is_never_reused_for_another_route(self) -> None:
        result = routing.recommend(self.catalog, self.profile(policy="cost-optimized",
            host_cost_state={"codex": "extra-usage", "claude": "extra-usage"},
            session_cost_basis="route-specific-cohort", cohort_route_id="terra", accepted_completions=1),
            runtime_allowlist=self.allowlist, credential_present_routes=self.allowlist, today=TODAY)
        self.assertEqual([item["route_id"] for item in result["ranked_routes"]], ["terra"])
        sol = next(item for item in result["exclusions"] if item["route_id"] == "sol")
        self.assertIn("cost-basis-missing-or-stale", sol["reasons"])

    def test_route_specific_cohorts_compare_observed_efficiency_and_include_failures(self) -> None:
        result = routing.recommend(self.catalog, self.profile(policy="cost-optimized",
            host_cost_state={"codex": "extra-usage", "claude": "extra-usage"},
            session_cost_basis="route-specific-cohorts", route_session_cohorts={
                "terra": {"session_attempts": [
                    {"kind": "failed-primary", "uncached_input_tokens": 1_000_000},
                    {"kind": "accepted-retry", "uncached_input_tokens": 1_000_000},
                ], "accepted_completions": 1},
                "sol": {"session_attempts": [
                    {"kind": "accepted-primary", "uncached_input_tokens": 100_000},
                ], "accepted_completions": 1},
            }), runtime_allowlist=self.allowlist,
            credential_present_routes=self.allowlist, today=TODAY)
        self.assertEqual(result["winner"]["route_id"], "sol")
        terra = next(item for item in result["ranked_routes"] if item["route_id"] == "terra")
        self.assertEqual(terra["estimated_cost"]["attempt_count"], 2)
        self.assertEqual(terra["expected_cost_per_accepted_result"], 4.0)
        self.assertEqual(result["winner"]["expected_cost_per_accepted_result"], 0.8)
        self.assertEqual(result["winner"]["cost_per_accepted_basis"],
                         "observed-route-specific-cohorts")
        fable = next(item for item in result["exclusions"] if item["route_id"] == "fable")
        self.assertIn("route-session-cohort-missing", fable["reasons"])

    def test_route_specific_cohorts_never_borrow_and_zero_success_fails_closed(self) -> None:
        result = routing.recommend(self.catalog, self.profile(policy="cost-optimized",
            host_cost_state={"codex": "extra-usage", "claude": "extra-usage"},
            session_cost_basis="route-specific-cohorts", route_session_cohorts={
                "terra": {"session_attempts": [{"uncached_input_tokens": 10}],
                          "accepted_completions": 0},
            }), runtime_allowlist=self.allowlist,
            credential_present_routes=self.allowlist, today=TODAY)
        self.assertIsNone(result["winner"])
        terra = next(item for item in result["exclusions"] if item["route_id"] == "terra")
        self.assertIn("accepted-completion-cost-unknown", terra["reasons"])
        sol = next(item for item in result["exclusions"] if item["route_id"] == "sol")
        self.assertIn("route-session-cohort-missing", sol["reasons"])

    def test_route_specific_cohorts_validate_each_cohort(self) -> None:
        common = {"session_cost_basis": "route-specific-cohorts"}
        with self.assertRaisesRegex(routing.RoutingError, "requires route_session_cohorts"):
            routing._profile(self.profile(**common))
        with self.assertRaisesRegex(routing.RoutingError, "terra.session_attempts"):
            routing._profile(self.profile(**common, route_session_cohorts={
                "terra": {"session_attempts": [], "accepted_completions": 1},
            }))
        with self.assertRaisesRegex(routing.RoutingError, "per-route accepted"):
            routing._profile(self.profile(**common, accepted_completions=1,
                route_session_cohorts={"terra": {
                    "session_attempts": [{"uncached_input_tokens": 1}],
                    "accepted_completions": 1,
                }}))

    def test_execute_rejects_cloud_workspace_route(self) -> None:
        cloud = route("cloud", "example", 99, 1)
        cloud["execution_location"] = "cloud-only"
        catalog = copy.deepcopy(self.catalog)
        catalog["routes"].append(cloud)
        allowlist = self.allowlist | frozenset({("openai", "codex", "execute", "example")})
        result = routing.recommend(catalog, self.profile(), runtime_allowlist=allowlist,
            credential_present_routes=allowlist, today=TODAY)
        record = next(item for item in result["exclusions"] if item["route_id"] == "cloud")
        self.assertIn("not-local-user-workspace", record["reasons"])

    def test_rate_bands_and_temporary_offers_fail_closed_when_plan_or_date_does_not_match(self) -> None:
        model = {"basis": "external-billable", "verified_on": "2026-08-15", "source": "fixture",
                 "rate_bands": [{"plan_state": "off-peak", "min_input_tokens": 0, "max_input_tokens": 1000,
                     "starts_on": "2026-08-01", "expires_on": "2026-08-31",
                     "rates": {"unit": "usd", "input_per_million": 1, "output_per_million": 2}}]}
        kwargs = {"host_cost_state": "extra-usage", "now": TODAY, "max_days": 30}
        matched = routing.estimate_session_cost(model, [{"uncached_input_tokens": 1000}], plan_state="off-peak", **kwargs)
        self.assertEqual(matched["value"], 0.001)
        self.assertIsNone(routing.estimate_session_cost(model, [{"uncached_input_tokens": 1000}], plan_state="standard", **kwargs))
        self.assertIsNone(routing.estimate_session_cost(model, [{"uncached_input_tokens": 1001}], plan_state="off-peak", **kwargs))
        per_attempt = {**model, "rate_bands": [
            {"min_input_tokens": 0, "max_input_tokens": 512_000,
             "rates": {"unit": "usd", "input_per_million": 1, "output_per_million": 2}},
            {"min_input_tokens": 512_001,
             "rates": {"unit": "usd", "input_per_million": 10, "output_per_million": 20}},
        ]}
        split = routing.estimate_session_cost(per_attempt,
            [{"uncached_input_tokens": 300_000}, {"uncached_input_tokens": 300_000}], **kwargs)
        self.assertEqual(split["value"], 0.6)

    def test_production_catalog_exactly_matches_runtime_inventory(self) -> None:
        catalog = routing.load_catalog()
        models = json.loads((Path(__file__).parents[1] / "config/models.json").read_text())
        runtime = routing.allowlist_from_models(models)
        inventory = frozenset((item["provider"], item["host"], item["mode"], item["model"]) for item in catalog["routes"])
        self.assertEqual(inventory, runtime)
        self.assertTrue(all(item["state"] == "discoverable" for item in catalog["routes"]))
        cards = catalog["rate_cards"]
        self.assertEqual(cards["openai-chatgpt-workspace-credits-2026-08-29"]["models"]["gpt-5.6-sol"]["output_per_million"], 500)
        self.assertEqual(cards["anthropic-api-list-price-proxy-2026-08-29"]["applicability"], "comparison-proxy-not-native-oauth-spend")

    def test_catalog_path_environment_override_uses_private_evidence_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-catalog.json"
            payload = json.loads(routing.DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
            payload["catalog_version"] = "private-local-evidence"
            path.write_text(json.dumps(payload), encoding="utf-8")
            previous = os.environ.get(routing.ROUTING_CATALOG_ENV)
            os.environ[routing.ROUTING_CATALOG_ENV] = str(path)
            try:
                self.assertEqual(routing.load_catalog()["catalog_version"], "private-local-evidence")
                self.assertNotEqual(routing.load_catalog(routing.DEFAULT_CATALOG_PATH)["catalog_version"],
                                    "private-local-evidence")
            finally:
                if previous is None:
                    os.environ.pop(routing.ROUTING_CATALOG_ENV, None)
                else:
                    os.environ[routing.ROUTING_CATALOG_ENV] = previous


if __name__ == "__main__":
    unittest.main()
