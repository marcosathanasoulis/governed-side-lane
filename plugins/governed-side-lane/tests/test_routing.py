from datetime import date
import copy
import json
from pathlib import Path
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
        "connector_retention": "worker-host", "connector_evidence": {"verified": True, "verified_on": "2026-08-15", "source": "fixture"},
        "privacy_classes": ["ordinary"], "capabilities": {"supported": ["workspace-write", "gitnexus"], "authority_roles": ["worker"]},
        "cost_model": ({"basis": "prepaid-flat-rate", "verified_on": "2026-08-15", "source": "fixture"} if explicit else
            {"basis": "native-oauth", "verified_on": "2026-08-15", "source": "fixture", "rates": {"unit": "workspace-credit", "input_per_million": price, "output_per_million": price * 2}}),
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


if __name__ == "__main__":
    unittest.main()
