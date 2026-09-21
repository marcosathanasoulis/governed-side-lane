"""Routed (router-selected pool) provider contract tests.

Mocked stream shapes model the translation boundary documented by the
integration contract (upstream body ``model`` carried into message frames,
tool_use round trips). They prove contract enforcement only; no upstream
runtime qualification is claimed.
"""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from side_lane.adapters import claude
from side_lane import routing

SUPPORTS_STRICT_MCP = mock.Mock(
    return_value=subprocess.CompletedProcess([], 0, "--strict-mcp-config", "")
)

SELECTOR = "example-pool-selector"
POOL = ["upstream-a/model-alpha", "upstream-b/model-beta"]
POLICY = {
    "requested_selector": SELECTOR,
    "allowed_upstream_models": POOL,
    "settings_precedence": "verified",
}
QUALIFIED = {
    "verified": True,
    "verified_on": "2026-01-01",
    "kind": "transport-and-local-adapter",
    "source": "mocked fixture; not runtime qualification",
}


def omniroute_provider(base_url="https://omniroute.example.invalid"):
    return {
        "gateway": "omniroute-router",
        "auth_method": "provider-key",
        "credential_service": "side-lane-omniroute-example",
        "base_url": base_url,
        "billable": True,
        "explicit_only": True,
    }


def omniroute_model_config(policy=POLICY):
    config = {
        "runtime_model": SELECTOR,
        "protocol": "anthropic-compatible",
        "qualification": dict(QUALIFIED),
    }
    if policy is not None:
        config["routing_policy_contract"] = dict(policy)
    return config


def stream(*models, toolcall=False):
    """Build a mocked translated stream: init echo, messages, result frame."""

    lines = [
        {"type": "system", "subtype": "init", "session_id": "fixture", "model": SELECTOR},
    ]
    if models:
        lines.append({"type": "assistant", "message": {
            "id": "msg_fixture_1", "model": models[0],
            "content": ([{"type": "tool_use", "id": "toolu_fixture_1", "name": "Bash",
                          "input": {"command": "git status"}}] if toolcall
                        else [{"type": "text", "text": "fixture"}]),
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }})
    for index, model in enumerate(models[1:], start=2):
        lines.append({"type": "user", "message": {
            "id": f"msg_fixture_{index}", "model": model,
            "content": [{"type": "tool_result", "tool_use_id": "toolu_fixture_1",
                         "content": "fixture"}],
        }})
        lines.append({"type": "assistant", "message": {
            "id": f"msg_fixture_{index}b", "model": model,
            "content": [{"type": "text", "text": "fixture"}],
        }})
    result_frame = {"type": "result", "subtype": "success",
                    "usage": {"input_tokens": 30, "output_tokens": 6}}
    if models:
        result_frame["model"] = models[-1]
    lines.append(result_frame)
    return "\n".join(json.dumps(line) for line in lines) + "\n"


class RoutedRouteValidationTests(unittest.TestCase):
    def test_validate_routing_policy_contract_returns_declared_set(self):
        allowed = claude.validate_routing_policy_contract(
            "omniroute", SELECTOR, omniroute_model_config()
        )
        self.assertEqual(allowed, frozenset(POOL))

    def test_route_metadata_requires_a_complete_routing_policy_contract(self):
        cases = {
            "missing contract": omniroute_model_config(policy=None),
            "selector mismatch": omniroute_model_config(policy={**POLICY, "requested_selector": "other"}),
            "unverified precedence": omniroute_model_config(policy={**POLICY, "settings_precedence": "unverified"}),
            "empty upstream set": omniroute_model_config(policy={**POLICY, "allowed_upstream_models": []}),
            "string upstream set": omniroute_model_config(policy={**POLICY, "allowed_upstream_models": "one"}),
            "duplicate upstream entry": omniroute_model_config(policy={**POLICY, "allowed_upstream_models": ["a", "a"]}),
            "exact identity claimed": omniroute_model_config() | {"identity_contract": {
                "requested_model": SELECTOR, "resolved_model": SELECTOR,
                "settings_precedence": "verified"}},
        }
        for label, model_config in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(claude.ClaudeAdapterError):
                    claude.build_transport_environment(
                        {}, provider="omniroute", model=SELECTOR,
                        provider_config=omniroute_provider(),
                        model_config=model_config, mode="execute", secret="selected",
                    )
        for label, provider_config in {
            "wrong gateway": omniroute_provider() | {"gateway": "direct-omniroute"},
            "non-HTTPS endpoint": omniroute_provider(base_url="http://omniroute.example.invalid"),
        }.items():
            with self.subTest(case=label):
                with self.assertRaises(claude.ClaudeAdapterError):
                    claude.build_transport_environment(
                        {}, provider="omniroute", model=SELECTOR,
                        provider_config=provider_config,
                        model_config=omniroute_model_config(),
                        mode="execute", secret="selected",
                    )

    def test_exact_routes_reject_the_routing_policy_contract(self):
        provider = {"gateway": "direct-kimi", "auth_method": "provider-key",
                    "billable": True, "base_url": "https://api.kimi.com/coding/"}
        with self.assertRaisesRegex(claude.ClaudeAdapterError, "reserved for routed providers"):
            claude.build_transport_environment(
                {}, provider="kimi", model="k3-256k", provider_config=provider,
                model_config={
                    "runtime_model": "k3-256k", "protocol": "anthropic-compatible",
                    "identity_contract": {"requested_model": "k3-256k", "resolved_model": "k3-256k",
                                          "settings_precedence": "verified"},
                    "routing_policy_contract": dict(POLICY),
                },
                mode="execute", secret="selected",
            )

    def test_transport_env_pins_selector_and_scrubs_inherited_auth(self):
        child = claude.build_transport_environment(
            {"PATH": "/bin", "ANTHROPIC_API_KEY": "old", "ANTHROPIC_BASE_URL": "https://old",
             "ANTHROPIC_MODEL": "old-model", "OPENAI_API_KEY": "old"},
            provider="omniroute", model=SELECTOR, provider_config=omniroute_provider(),
            model_config=omniroute_model_config(), mode="execute", secret="selected",
        )
        self.assertEqual(child["ANTHROPIC_AUTH_TOKEN"], "selected")
        self.assertEqual(child["ANTHROPIC_BASE_URL"], "https://omniroute.example.invalid")
        for name in claude.EXACT_MODEL_ENV_NAMES:
            self.assertEqual(child[name], SELECTOR)
        self.assertNotIn("ANTHROPIC_API_KEY", child)
        self.assertNotIn("OPENAI_API_KEY", child)


class RoutedLaunchTests(unittest.TestCase):
    def setUp(self):
        claude._strict_mcp_executable_cache["claude"] = True


    def repo(self, root, name):
        path = root / name
        path.mkdir()
        (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
        return path

    def launch(self, stdout, model_config=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout, ""))
            result = claude.launch(
                executable="claude", repo=repo, worktree=lane, provider="omniroute",
                model=SELECTOR, provider_config=omniroute_provider(),
                model_config=model_config or omniroute_model_config(),
                prompt="task", secret="selected", runner=runner,
                readiness_runner=SUPPORTS_STRICT_MCP,
            )
        runner.assert_called_once()
        return result

    def test_launch_requires_verified_qualification_before_any_worker(self):
        runner = mock.Mock()
        with self.assertRaisesRegex(claude.ClaudeAdapterError, "qualification"):
            claude.launch(
                executable="claude", repo=".", worktree=".", provider="omniroute",
                model=SELECTOR, provider_config=omniroute_provider(),
                model_config=omniroute_model_config() | {"qualification": {"verified": False}},
                prompt="task", secret="selected", runner=runner,
                readiness_runner=SUPPORTS_STRICT_MCP,
            )
        runner.assert_not_called()

    def test_single_in_pool_toolcall_stream_passes_and_records_actual_model(self):
        result = self.launch(stream(POOL[0], toolcall=True))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.provider, "omniroute")
        self.assertEqual(result.requested_model, SELECTOR)
        self.assertEqual(result.resolved_model, POOL[0])
        self.assertEqual(result.attested_models, (POOL[0],))
        receipt = result.as_dict()
        self.assertEqual(receipt["attested_models"], [POOL[0]])
        self.assertNotIn(SELECTOR, receipt["attested_models"])

    def test_multi_model_within_pool_is_legitimate_conversation_fallback(self):
        result = self.launch(stream(POOL[0], POOL[1]))
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(result.resolved_model)
        self.assertEqual(result.attested_models, tuple(sorted(POOL)))
        self.assertEqual(result.requested_model, SELECTOR)

    def test_out_of_pool_attestation_fails_closed_as_exit_65(self):
        result = self.launch(stream(POOL[0], "unapproved-model"))
        self.assertEqual(result.returncode, 65)
        self.assertIn("routing-violation", result.stderr)
        self.assertIn("unapproved-model", result.stderr)
        self.assertNotIn(SELECTOR, result.attested_models)

    def test_missing_or_alias_only_attestation_fails_closed(self):
        result = self.launch(stream())
        self.assertEqual(result.returncode, 65)
        self.assertIn("routing-unverified", result.stderr)
        alias_only = json.dumps({"type": "system", "subtype": "init", "model": SELECTOR})
        result = self.launch(alias_only)
        self.assertEqual(result.returncode, 65)
        self.assertIn("routing-unverified", result.stderr)
        self.assertEqual(result.attested_models, ())


class RoutedConfigTests(unittest.TestCase):
    def test_allowlist_extraction_tolerates_the_routed_provider_shape(self):
        with_model = {"providers": {"omniroute": omniroute_provider() | {
            "routes": {"execute": {"claude": {
                "protocol": "anthropic-compatible", "models": [SELECTOR]}}}}}}
        self.assertIn(("omniroute", "claude", "execute", SELECTOR),
                      routing.allowlist_from_models(with_model))
        without_model = {"providers": {"omniroute": omniroute_provider() | {"routes": {}}}}
        self.assertEqual(routing.allowlist_from_models(without_model), frozenset())

    def test_public_example_profile_is_disabled_and_generic(self):
        package_root = Path(__file__).resolve().parents[1]
        example = json.loads(
            (package_root / "config/examples/provider-profiles.disabled.json").read_text()
        )
        entry = next(item for item in example["providers"] if item["provider"] == "omniroute")
        self.assertFalse(entry["enabled"])
        self.assertEqual(entry["gateway"], "omniroute-router")
        self.assertIn(".invalid", entry["base_url"])
        self.assertEqual(sorted(entry["routing_policy_contract"]["allowed_upstream_models"]),
                         sorted(POOL))


class GuideRegressionTests(unittest.TestCase):
    """Public docs and examples keep a complete, accurate routed contract."""

    def _extract_json_blocks(self, text):
        return re.findall(r"```json\n(.*?)```", text, re.DOTALL)

    def test_omniroute_guide_example_has_complete_contract(self):
        package_root = Path(__file__).resolve().parents[1]
        guide = (package_root / "docs/omniroute-guide.md").read_text()
        blocks = self._extract_json_blocks(guide)
        self.assertTrue(blocks, "no JSON code blocks in omniroute-guide.md")
        contract_blocks = [
            json.loads(block) for block in blocks
            if "routing_policy_contract" in block
        ]
        self.assertTrue(
            contract_blocks,
            "no JSON block with a routing_policy_contract found in the guide",
        )
        for fragment in contract_blocks:
            provider = fragment.get("provider", "")
            model_configs = fragment.get("routes", {}).get("execute", {}).get(
                "claude", {}).get("model_configs", {})
            self.assertIn(SELECTOR, model_configs)
            contract = model_configs[SELECTOR]["routing_policy_contract"]
            self.assertEqual(contract["requested_selector"], SELECTOR)
            for field in (
                "allowed_upstream_models",
                "policy_revision",
                "policy_fingerprint",
                "settings_precedence",
            ):
                self.assertIn(field, contract)
            self.assertEqual(contract["settings_precedence"], "verified")
            self.assertIsNotNone(provider)
            self.assertTrue(
                fragment.get("automatic_selector_policy") is True
                or "routed-policy-snapshot" in guide,
                "guide should mention automatic collection or a manual snapshot",
            )

    def test_disabled_profile_has_complete_contract(self):
        package_root = Path(__file__).resolve().parents[1]
        example = json.loads(
            (package_root / "config/examples/provider-profiles.disabled.json").read_text()
        )
        entry = next(
            item for item in example["providers"] if item["provider"] == "omniroute"
        )
        contract = entry["routing_policy_contract"]
        for field in (
            "requested_selector",
            "allowed_upstream_models",
            "policy_revision",
            "policy_fingerprint",
            "settings_precedence",
        ):
            self.assertIn(field, contract)
        self.assertEqual(contract["settings_precedence"], "verified")

    def test_omniroute_guide_does_not_overclaim_live_enforcement(self):
        package_root = Path(__file__).resolve().parents[1]
        guide = (package_root / "docs/omniroute-guide.md").read_text()
        self.assertNotIn("validates only syntax and freshness", guide)
        self.assertIn("does not prove live server", guide)
        self.assertIn("automatic_selector_policy", guide)

    def test_no_gateway_example_uses_a_configured_native_claude_model(self):
        package_root = Path(__file__).resolve().parents[1]
        guide = (package_root / "docs/omniroute-guide.md").read_text()
        models = json.loads(
            (package_root / "config/models.json").read_text(encoding="utf-8")
        )
        allowed = set(models["providers"]["claude"]["routes"]["execute"]["claude"]["models"])
        for block in re.findall(r"```bash\n(.*?)```", guide, re.DOTALL):
            if "--provider claude" in block and "--report-only" not in block:
                match = re.search(r"--model\s+(\S+)", block)
                if match:
                    self.assertIn(
                        match.group(1),
                        allowed,
                        f"no-gateway example model {match.group(1)!r} is not in the configured claude execute allowlist",
                    )
                    return
        self.fail("no no-gateway native example block found in omniroute-guide.md")


if __name__ == "__main__":
    unittest.main()
