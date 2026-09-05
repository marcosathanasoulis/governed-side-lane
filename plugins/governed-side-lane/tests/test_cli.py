import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from side_lane import cli
from side_lane.results import LaneResult


class SideLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep host-executable resolution hermetic: a real override in the
        # developer's or CI environment must not steer these tests.
        patcher = mock.patch.dict(os.environ, {"SIDE_LANE_CODEX_EXECUTABLE": "", "SIDE_LANE_CLAUDE_EXECUTABLE": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

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
            capability=[], lane_name="review", approve_billable_route=False, worktree_root=None)
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
            capability=["gitnexus"], lane_name="worker", approve_billable_route=False, worktree_root=None)
        with mock.patch("side_lane.cli._capability_report", return_value={"capability_evidence": {"gitnexus": {"state": "unknown"}}, "capabilities": {"gitnexus": False}}), mock.patch("side_lane.cli.require_native_oauth") as auth:
            with self.assertRaisesRegex(cli.SideLaneError, "required capabilities unavailable"):
                cli._launch(args, cli.load_config(), self.repo(), "Implement")
            auth.assert_not_called()

    def test_native_oauth_failure_never_falls_back_to_key(self) -> None:
        args = mock.Mock(host="codex", mode="review", provider="openai", model="gpt-5.6-terra",
            capability=[], lane_name="review", approve_billable_route=False, worktree_root=None)
        lane = mock.Mock(worktree=self.repo())
        with mock.patch("side_lane.cli._require_host_executable", return_value="/opt/hosts/codex"), mock.patch("side_lane.cli.create_worktree", return_value=lane), mock.patch("side_lane.cli.dispose_clean_worktree"), mock.patch("side_lane.cli.require_native_oauth", side_effect=cli.AuthError("signed out")), mock.patch("side_lane.cli.read_credential") as read:
            with self.assertRaises(cli.AuthError):
                cli._launch(args, cli.load_config(), self.repo(), "Review")
            read.assert_not_called()

    def test_review_uses_audited_disposable_worktree(self) -> None:
        repo = self.repo()
        worktree = repo.parent / "review-worktree"
        lane = mock.Mock(worktree=worktree, branch="side-lane/review-1")
        result = LaneResult(("claude",), 0, worktree, "claude", "claude",
            "native-claude", "claude-sonnet-5", "oauth", False, "finding: bug in api.py", "")
        args = mock.Mock(host="claude", mode="review", provider="claude",
            model="claude-sonnet-5", capability=[], lane_name="review",
            approve_billable_route=False, worktree_root=None)
        with mock.patch("side_lane.cli._require_host_executable", return_value="/opt/hosts/claude"), \
             mock.patch("side_lane.cli.create_worktree", return_value=lane) as create, \
             mock.patch("side_lane.cli.require_native_oauth"), \
             mock.patch("side_lane.adapters.claude.launch", return_value=result) as launch, \
             mock.patch("side_lane.cli.git_status", return_value="## review"), \
             mock.patch("side_lane.cli.write_audit", return_value=repo / ".git/audit.json") as audit, \
             mock.patch("side_lane.cli.dispose_clean_worktree") as dispose, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli._launch(args, cli.load_config(), repo, "Review"), 0)
        create.assert_called_once_with(repo, "review", worktree_root=None)
        self.assertEqual(launch.call_args.kwargs["worktree"], worktree)
        self.assertEqual(launch.call_args.kwargs["executable"], "/opt/hosts/claude")
        self.assertEqual(audit.call_args.kwargs["stdout"], "finding: bug in api.py")
        dispose.assert_called_once_with(lane)
        lines = output.getvalue().splitlines()
        summary = json.loads("\n".join(lines[1:]))
        self.assertTrue(summary["worktree_disposed"])
        self.assertEqual(summary["result_artifact"], str(repo / ".git/audit.json"))

    def test_failed_result_persistence_preserves_the_worktree(self) -> None:
        repo = self.repo()
        worktree = repo.parent / "review-worktree"
        lane = mock.Mock(worktree=worktree, branch="side-lane/review-1")
        result = LaneResult(("claude",), 0, worktree, "claude", "claude",
            "native-claude", "claude-sonnet-5", "oauth", False, "finding: bug", "")
        args = mock.Mock(host="claude", mode="review", provider="claude",
            model="claude-sonnet-5", capability=[], lane_name="review",
            approve_billable_route=False, worktree_root=None)
        with mock.patch("side_lane.cli._require_host_executable", return_value="/opt/hosts/claude"), \
             mock.patch("side_lane.cli.create_worktree", return_value=lane), \
             mock.patch("side_lane.cli.require_native_oauth"), \
             mock.patch("side_lane.adapters.claude.launch", return_value=result), \
             mock.patch("side_lane.cli.git_status", return_value="## review"), \
             mock.patch("side_lane.cli.write_audit", side_effect=OSError("disk full")), \
             mock.patch("side_lane.cli.dispose_clean_worktree") as dispose, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                cli._launch(args, cli.load_config(), repo, "Review")
        dispose.assert_not_called()

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


class ConnectorDiscoveryTests(unittest.TestCase):
    def test_project_connector_files_are_host_specific(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".mcp.json").write_text('{"mcpServers": {"playwright": {"command": "npx"}}}', encoding="utf-8")
            (repo / ".codex").mkdir()
            (repo / ".codex" / "config.toml").write_text('[mcp_servers.gitnexus]\ncommand = "gitnexus"\n', encoding="utf-8")
            with mock.patch("side_lane.cli.Path.home", return_value=repo / "no-home"), \
                 mock.patch.dict(os.environ, {"CODEX_HOME": str(repo / "no-codex-home")}):
                claude_names = cli._discover_mcp_names("claude", repo)
                codex_names = cli._discover_mcp_names("codex", repo)
        self.assertEqual(claude_names, {"playwright"})
        self.assertEqual(codex_names, {"gitnexus"})

    def test_empty_codex_home_falls_back_to_the_default_not_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd, home = Path(directory) / "cwd", Path(directory) / "home"
            cwd.mkdir(); (home / ".codex").mkdir(parents=True)
            (cwd / "config.toml").write_text('[mcp_servers.leaked]\ncommand = "x"\n', encoding="utf-8")
            (home / ".codex" / "config.toml").write_text('[mcp_servers.expected]\ncommand = "x"\n', encoding="utf-8")
            previous = os.getcwd()
            os.chdir(cwd)
            try:
                with mock.patch("side_lane.cli.Path.home", return_value=home), \
                     mock.patch.dict(os.environ, {"CODEX_HOME": "  "}):
                    names = cli._discover_mcp_names("codex", None)
            finally:
                os.chdir(previous)
        self.assertEqual(names, {"expected"})


class ExecuteLanePermissionTests(SideLaneTests):
    def test_playwright_capability_is_reported_from_connector_names_only(self) -> None:
        config = cli.load_config()
        self.assertIn("playwright", config["capabilities"])
        with mock.patch("side_lane.cli.shutil.which", return_value="/bin/tool"), \
             mock.patch("side_lane.cli._discover_mcp_names", return_value={"playwright", "gitnexus"}):
            report = cli._capability_report(config, "claude", "execute", None, None)
        self.assertEqual(report["capability_evidence"]["playwright"]["state"], "present")
        self.assertIn("host_support_dir", report)
        with mock.patch("side_lane.cli.shutil.which", return_value="/bin/tool"), \
             mock.patch("side_lane.cli._discover_mcp_names", return_value=set()):
            report = cli._capability_report(config, "claude", "execute", None, None)
        self.assertEqual(report["capability_evidence"]["playwright"]["state"], "unavailable")

    def test_execute_launch_forwards_capabilities_and_reports_allowed_tools(self) -> None:
        repo = self.repo()
        worktree = repo.parent / "execute-worktree"
        lane = mock.Mock(worktree=worktree, branch="side-lane/task-1")
        result = LaneResult(("claude", "--allowedTools", "Bash(pnpm *)"), 0, worktree, "claude", "claude",
            "native-claude", "claude-sonnet-5", "oauth", False, "done", "",
            capabilities=("shell",), allowed_tools=("Read", "Bash(pnpm *)"))
        args = mock.Mock(host="claude", mode="execute", provider="claude",
            model="claude-sonnet-5", capability=["shell", "shell"], lane_name="task",
            approve_billable_route=False, worktree_root=None)
        readiness = {"capability_evidence": {"shell": {"state": "verified"}}, "capabilities": {"shell": True}}
        with mock.patch("side_lane.cli._require_host_executable", return_value="/opt/hosts/claude"), \
             mock.patch("side_lane.cli._capability_report", return_value=readiness), \
             mock.patch("side_lane.cli.create_worktree", return_value=lane), \
             mock.patch("side_lane.cli.require_native_oauth"), \
             mock.patch("side_lane.adapters.claude.launch", return_value=result) as launch, \
             mock.patch("side_lane.cli.git_status", return_value="## task"), \
             mock.patch("side_lane.cli.write_audit", return_value=repo / ".git/audit.json"), \
             mock.patch("side_lane.cli.dispose_clean_worktree") as dispose, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli._launch(args, cli.load_config(), repo, "Implement"), 0)
        self.assertEqual(launch.call_args.kwargs["capabilities"], ("shell",))
        dispose.assert_not_called()
        summary = json.loads("\n".join(output.getvalue().splitlines()[1:]))
        self.assertEqual(summary["allowed_tools"], ["Read", "Bash(pnpm *)"])
        self.assertEqual(summary["capabilities"], ["shell"])

    def test_codex_launch_passes_host_support_dir(self) -> None:
        repo = self.repo()
        worktree = repo.parent / "codex-worktree"
        lane = mock.Mock(worktree=worktree, branch="side-lane/task-2")
        result = LaneResult(("codex",), 0, worktree, "codex", "openai", "native-codex", "gpt-5.6-sol", "oauth", False, "", "")
        args = mock.Mock(host="codex", mode="execute", provider="openai", model="gpt-5.6-sol",
            capability=[], lane_name="task", approve_billable_route=False, worktree_root=None)
        with mock.patch("side_lane.cli._require_host_executable", return_value="/bundle/codex"), \
             mock.patch("side_lane.cli.host_support_dir", return_value="/bundle") as support, \
             mock.patch("side_lane.cli.create_worktree", return_value=lane), \
             mock.patch("side_lane.cli.require_native_oauth"), \
             mock.patch("side_lane.adapters.codex.run_codex", return_value=result) as run_codex, \
             mock.patch("side_lane.cli.git_status", return_value=""), \
             mock.patch("side_lane.cli.write_audit", return_value=repo / ".git/audit.json"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli._launch(args, cli.load_config(), repo, "Implement"), 0)
        support.assert_any_call("codex", "/bundle/codex")
        self.assertEqual(run_codex.call_args.kwargs["support_dir"], "/bundle")


class LaunchCapabilityGateTests(SideLaneTests):
    def _args(self, capability):
        return mock.Mock(host="claude", mode="execute", provider="claude", model="claude-sonnet-5",
                         capability=capability, lane_name="task", approve_billable_route=False, worktree_root=None)

    def test_present_evidence_is_enough_to_launch_but_unknown_is_not(self) -> None:
        report = {"capability_evidence": {
            "playwright": {"state": "present"}, "git-push": {"state": "present"},
            "shell": {"state": "verified"}, "secret-use": {"state": "unknown"},
            "gitnexus": {"state": "unavailable"}}, "capabilities": {"shell": True}}
        with mock.patch("side_lane.cli._capability_report", return_value=report), \
             mock.patch("side_lane.cli._require_host_executable", side_effect=cli.SideLaneError("stop here")):
            with self.assertRaisesRegex(cli.SideLaneError, "stop here"):
                cli._launch(self._args(["playwright", "git-push", "shell"]), cli.load_config(), self.repo(), "Implement")
            with self.assertRaisesRegex(cli.SideLaneError, "unavailable: secret-use"):
                cli._launch(self._args(["secret-use"]), cli.load_config(), self.repo(), "Implement")
            with self.assertRaisesRegex(cli.SideLaneError, "unavailable: gitnexus"):
                cli._launch(self._args(["gitnexus"]), cli.load_config(), self.repo(), "Implement")


if __name__ == "__main__":
    unittest.main()


class HostExecutableCliTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep host-executable resolution hermetic: a real override in the
        # developer's or CI environment must not steer these tests.
        patcher = mock.patch.dict(os.environ, {"SIDE_LANE_CODEX_EXECUTABLE": "", "SIDE_LANE_CLAUDE_EXECUTABLE": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_launch_fails_before_worktree_when_host_executable_is_missing(self) -> None:
        from side_lane import hosts

        args = mock.Mock(host="codex", mode="execute", provider="openai", model="gpt-5.6-terra",
            capability=[], lane_name="worker", approve_billable_route=False, worktree_root=None)
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".git").mkdir()
            with mock.patch("side_lane.cli.shutil.which", return_value=None), \
                 mock.patch.object(hosts, "BUNDLED_CODEX_CANDIDATES", ()), \
                 mock.patch("side_lane.cli.create_worktree") as create:
                with self.assertRaisesRegex(cli.SideLaneError, "codex executable not found"):
                    cli._launch(args, cli.load_config(), repo, "task")
                create.assert_not_called()

    def test_capability_report_and_auth_status_use_resolved_bundle_executable(self) -> None:
        from side_lane import hosts

        config = cli.load_config()
        ready = mock.Mock(ready=True, as_dict=lambda: {"state": "ready", "method": "oauth"})
        with tempfile.TemporaryDirectory() as directory:
            bundled = Path(directory) / "codex"
            bundled.write_text("#!/bin/sh\n", encoding="utf-8")
            bundled.chmod(0o755)
            with mock.patch("side_lane.cli.shutil.which", return_value=None), \
                 mock.patch.object(hosts, "BUNDLED_CODEX_CANDIDATES", (str(bundled),)), \
                 mock.patch("side_lane.cli.auth_status", return_value=ready) as status, \
                 mock.patch("side_lane.cli._discover_mcp_names", return_value=set()):
                report = cli._capability_report(config, "codex", "execute", "openai", "gpt-5.6-terra")
        self.assertEqual(report["runtime"], str(bundled))
        self.assertEqual(report["capability_evidence"]["shell"]["state"], "verified")
        status.assert_called_with("codex", executable=str(bundled))
