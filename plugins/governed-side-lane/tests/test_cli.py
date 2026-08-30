import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from side_lane import cli
from side_lane.results import LaneResult


class SideLaneTests(unittest.TestCase):
    def repo(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name)
        subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
        (path / "CLAUDE.md").write_text("# Rules\n", encoding="utf-8")
        (path / "AGENTS.md").write_text("You must read [CLAUDE.md](./CLAUDE.md); it is the authoritative source of truth.\n", encoding="utf-8")
        return path

    def test_exact_native_matrix_and_explicit_glm_metadata(self) -> None:
        config = cli.load_config()
        provider, route = cli.select_route(config, "codex", "execute", "openai", "gpt-5.6-terra")
        self.assertEqual((provider["gateway"], route["auth_method"], route["billable"]), ("native-codex", "oauth", False))
        provider, route = cli.select_route(config, "claude", "execute", "glm", "glm-5.3")
        self.assertEqual((provider["gateway"], route["auth_method"], route["billable"]), ("direct-zai", "provider-key", True))
        with self.assertRaisesRegex(cli.SideLaneError, "unknown provider"):
            cli.select_route(config, "codex", "execute", "openrouter", "openai/gpt-5.6-terra")

    def test_config_requires_schema_three(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text('{"schema_version":2,"providers":{},"capabilities":[]}', encoding="utf-8")
            with self.assertRaisesRegex(cli.SideLaneError, "schema_version 3"):
                cli.load_config(path)

    def test_explicit_models_path_override_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(
                (cli.DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with mock.patch.dict("os.environ", {"SIDE_LANE_MODELS_PATH": str(path)}):
                self.assertEqual(cli.config_path(), path)
                self.assertEqual(cli.load_config()["schema_version"], 3)

    def test_governance_and_prompt_modes_fail_closed(self) -> None:
        repo = self.repo()
        self.assertEqual(cli.validate_governance(str(repo)), repo.resolve())
        with self.assertRaises(cli.SideLaneError):
            cli.load_prompt("Edit the files", None, "review")
        with self.assertRaises(cli.SideLaneError):
            cli.load_prompt("Fix src/auth.py", None, "review")
        with self.assertRaises(cli.SideLaneError):
            cli.load_prompt("Implement feature", None, "review")
        self.assertEqual(cli.load_prompt("Edit the files", None, "execute"), "Edit the files")
        with self.assertRaises(cli.SideLaneError):
            cli.load_prompt("Deploy it", None, "execute")

    def test_governance_rejects_symlinked_entrypoint(self) -> None:
        repo = self.repo()
        target = repo / "AGENTS.real.md"
        target.write_text((repo / "AGENTS.md").read_text(encoding="utf-8"), encoding="utf-8")
        (repo / "AGENTS.md").unlink()
        (repo / "AGENTS.md").symlink_to(target)
        with self.assertRaisesRegex(cli.SideLaneError, "regular repository file"):
            cli.validate_governance(str(repo))

    def test_parser_rejects_extra_credentials_and_requires_host(self) -> None:
        parser = cli.make_parser()
        base = ["run", "--host", "claude", "--provider", "claude", "--model", "claude-sonnet-5", "--repo", ".", "--prompt", "Review"]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(base + ["--api-key", "secret"])

    def test_capability_report_uses_auth_metadata_or_override_presence_only(self) -> None:
        config = cli.load_config()
        ready = mock.Mock(ready=True, as_dict=lambda: {"state": "ready", "method": "oauth"})
        with mock.patch("side_lane.cli.auth_status", return_value=ready), mock.patch("side_lane.cli.shutil.which", return_value="/bin/tool"), mock.patch("side_lane.cli._discover_mcp_names", return_value={"gitnexus"}):
            native = cli._capability_report(config, "codex", "execute", "openai", "gpt-5.6-sol")
        self.assertEqual(native["auth"], {"state": "ready", "method": "oauth"})
        self.assertFalse(native["capabilities"]["git-push"])
        self.assertEqual(native["capability_evidence"]["git-push"]["state"], "present")
        self.assertEqual(native["capability_evidence"]["gitnexus"]["state"], "present")
        with mock.patch("side_lane.cli.credential_present", return_value=False):
            glm = cli._capability_report(config, "claude", "execute", "glm", "glm-5.3")
        self.assertEqual(glm["configured_override"], "absent")
        self.assertTrue(glm["requires_one_run_approval"])

    def test_billable_confirmation_is_checked_before_credential_lookup(self) -> None:
        args = mock.Mock(host="claude", mode="execute", provider="glm", model="glm-5.3",
            capability=[], lane_name="review", approve_billable_route=False)
        with mock.patch("side_lane.cli.read_credential") as read:
            with self.assertRaisesRegex(cli.SideLaneError, "explicit --approve"):
                cli._launch(args, cli.load_config(), self.repo(), "Review")
            read.assert_not_called()

    def test_adapter_errors_are_rendered_as_expected_cli_failures(self) -> None:
        with mock.patch("side_lane.cli.run", side_effect=cli.CodexAdapterError("bad adapter")), \
             contextlib.redirect_stderr(io.StringIO()) as errors, \
             self.assertRaisesRegex(SystemExit, "2"):
            cli.main()
        self.assertIn("side-lane: bad adapter", errors.getvalue())

    def test_required_capability_fails_before_auth_or_key(self) -> None:
        args = mock.Mock(host="codex", mode="execute", provider="openai", model="gpt-5.6-terra",
            capability=["gitnexus"], lane_name="worker", approve_billable_route=False)
        with mock.patch("side_lane.cli._capability_report", return_value={"capabilities": {"gitnexus": False}}), mock.patch("side_lane.cli.require_native_oauth") as auth:
            with self.assertRaisesRegex(cli.SideLaneError, "required capabilities unavailable"):
                cli._launch(args, cli.load_config(), self.repo(), "Implement")
            auth.assert_not_called()

    def test_native_oauth_failure_never_falls_back_to_key(self) -> None:
        args = mock.Mock(host="codex", mode="review", provider="openai", model="gpt-5.6-terra",
            capability=[], lane_name="review", approve_billable_route=False)
        lane = mock.Mock(worktree=self.repo())
        with mock.patch("side_lane.cli.create_worktree", return_value=lane), mock.patch("side_lane.cli.dispose_clean_worktree"), mock.patch("side_lane.cli.require_native_oauth", side_effect=cli.AuthError("signed out")), mock.patch("side_lane.cli.read_credential") as read:
            with self.assertRaises(cli.AuthError):
                cli._launch(args, cli.load_config(), self.repo(), "Review")
            read.assert_not_called()

    def test_review_uses_audited_disposable_worktree(self) -> None:
        repo = self.repo()
        worktree = repo.parent / "review-worktree"
        lane = mock.Mock(worktree=worktree, branch="side-lane/review-1")
        result = LaneResult(("claude",), 0, worktree, "claude", "claude",
            "native-claude", "claude-sonnet-5", "oauth", False, "", "")
        args = mock.Mock(host="claude", mode="review", provider="claude",
            model="claude-sonnet-5", capability=[], lane_name="review",
            approve_billable_route=False)
        with mock.patch("side_lane.cli.create_worktree", return_value=lane) as create, \
             mock.patch("side_lane.cli.require_native_oauth"), \
             mock.patch("side_lane.adapters.claude.launch", return_value=result) as launch, \
             mock.patch("side_lane.cli.git_status", return_value="## review"), \
             mock.patch("side_lane.cli.write_audit", return_value=repo / ".git/audit.json"), \
             mock.patch("side_lane.cli.dispose_clean_worktree") as dispose, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli._launch(args, cli.load_config(), repo, "Review"), 0)
        create.assert_called_once_with(repo, "review")
        self.assertEqual(launch.call_args.kwargs["worktree"], worktree)
        dispose.assert_called_once_with(lane)
        self.assertTrue(json.loads(output.getvalue())["worktree_disposed"])

    def test_recommend_profile_rejects_secret_usage_and_keeps_glm_explicit(self) -> None:
        for field in ("api_key", "credential", "secret", "quota", "billing", "usage"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "profile.json"
                path.write_text(json.dumps({field: "x"}), encoding="utf-8")
                with self.assertRaises(cli.SideLaneError):
                    cli.load_recommendation_profile(str(path))
        with mock.patch("side_lane.cli.auth_status") as status, \
             mock.patch("side_lane.cli.credential_present", return_value=False):
            status.return_value.ready = True
            self.assertFalse(any(route[0] == "glm" for route in cli._ready_routes(cli.load_config())))

    def test_offline_evaluate_command_never_calls_provider_or_credentials(self) -> None:
        path = Path(__file__).parent / "fixtures" / "evaluation" / "offline-evidence.json"
        with mock.patch("side_lane.cli.read_credential") as secret, \
             mock.patch("side_lane.adapters.codex.run_codex") as codex, \
             mock.patch("side_lane.adapters.claude.launch") as claude, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.run(["evaluate", "--input", str(path)]), 0)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["provider_calls_performed"])
        self.assertFalse(payload["credentials_accessed"])
        self.assertEqual(payload["evaluation_aggregates"][0]["sample_count"], 2)
        secret.assert_not_called()
        codex.assert_not_called()
        claude.assert_not_called()

    def test_recommend_builds_independent_worker_host_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile_path = Path(directory) / "profile.json"
            profile_path.write_text(json.dumps({
                "coordinator_host": "claude", "mode": "execute",
                "policy": "cost-optimized", "task_band": "debugging",
                "quality_floor": 90, "required_connectors": ["gitnexus"],
                "required_capabilities": ["workspace-write"],
                "host_cost_state": {"claude": "extra-usage", "codex": "included-oauth"},
            }), encoding="utf-8")
            args = mock.Mock(repo=directory, profile=str(profile_path))
            reports = {
                "codex": {"mcp_connectors": ["gitnexus"], "capabilities": {"workspace-write": True}},
                "claude": {"mcp_connectors": [], "capabilities": {"workspace-write": True}},
            }
            with mock.patch("side_lane.cli.validate_governance", return_value=Path(directory)), \
                 mock.patch("side_lane.cli._capability_report", side_effect=lambda _c, host, *_a: reports[host]), \
                 mock.patch("side_lane.cli._ready_routes", return_value=frozenset()), \
                 mock.patch("side_lane.cli.routing.load_catalog", return_value={}), \
                 mock.patch("side_lane.cli.routing.recommend", return_value={"winner": None}) as recommend, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli._recommend(args, cli.load_config()), 0)
            normalized = recommend.call_args.args[1]
            self.assertEqual(normalized["coordinator_host"], "claude")
            self.assertEqual(normalized["host_capabilities"]["codex"]["available_connectors"], ["gitnexus"])
            self.assertEqual(normalized["host_capabilities"]["claude"]["available_connectors"], [])

    def test_list_exposes_provider_gateway_auth_and_billing(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.run(["list"]), 0)
        text = output.getvalue()
        self.assertIn("openai\tnative-codex", text)
        self.assertIn("glm\tdirect-zai\tglm-5.3\tprovider-key\tbillable", text)


if __name__ == "__main__":
    unittest.main()
