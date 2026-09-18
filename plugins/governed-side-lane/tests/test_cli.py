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
from side_lane.worktrees import LaneDelivery, VerifyResult, WorktreeError


def _only_summary(stdout: str) -> dict:
    """Pull the summary object out of _launch's stdout.

    _launch prints progress lines around the summary, and how many is not part
    of any contract — slicing a fixed number of leading lines off made these
    tests depend on that incidental count. Decode from the first brace instead.
    """

    start = stdout.index("{")
    return json.JSONDecoder().raw_decode(stdout[start:])[0]


class SideLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep host-executable resolution hermetic: a real override in the
        # developer's or CI environment must not steer these tests.
        patcher = mock.patch.dict(
            os.environ,
            {"SIDE_LANE_CODEX_EXECUTABLE": "", "SIDE_LANE_CLAUDE_EXECUTABLE": ""},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def repo(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name)
        subprocess.run(
            ["git", "init", "-b", "main", str(path)], check=True, capture_output=True
        )
        (path / "CLAUDE.md").write_text("# Rules\n", encoding="utf-8")
        (path / "AGENTS.md").write_text(
            "You must read [CLAUDE.md](./CLAUDE.md); it is the authoritative source of truth.\n",
            encoding="utf-8",
        )
        return path

    def devin_billing_config(self) -> dict:
        config = cli.load_config()
        config["providers"]["devin"] = {
            "gateway": "native-devin",
            "auth_method": "oauth",
            "billable": False,
            "routes": {
                "execute": {
                    "devin": {
                        "protocol": "native-devin",
                        "models": ["swe-2-medium", "grok-4-6-low"],
                        "model_configs": {
                            "swe-2-medium": {"billable": False},
                            "grok-4-6-low": {"billable": True},
                        },
                    }
                }
            },
        }
        return config

    def test_exact_native_matrix_and_explicit_glm_metadata(self) -> None:
        config = cli.load_config()
        provider, route = cli.select_route(
            config, "codex", "execute", "openai", "gpt-5.6-terra"
        )
        self.assertEqual(
            (provider["gateway"], route["auth_method"], route["billable"]),
            ("native-codex", "oauth", False),
        )
        provider, route = cli.select_route(
            config, "claude", "execute", "glm", "glm-5.3"
        )
        self.assertEqual(
            (provider["gateway"], route["auth_method"], route["billable"]),
            ("direct-zai", "provider-key", True),
        )
        with self.assertRaisesRegex(cli.SideLaneError, "unknown provider"):
            cli.select_route(
                config, "codex", "execute", "openrouter", "openai/gpt-5.6-terra"
            )

    def test_config_requires_schema_three(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(
                '{"schema_version":2,"providers":{},"capabilities":[]}',
                encoding="utf-8",
            )
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
        self.assertEqual(
            cli.load_prompt("Edit the files", None, "execute"), "Edit the files"
        )
        with self.assertRaises(cli.SideLaneError):
            cli.load_prompt("Deploy it", None, "execute")

    def test_execute_unsafe_deploy_matches_verb_not_noun_or_filename(self) -> None:
        rejected = (
            "then deploy it to dev",
            "Deploy the worker",
            "deploy to production",
            "run the deploy now",
            "please deploy",
            "Deploy that to staging",
        )
        for prompt in rejected:
            with self.assertRaises(cli.SideLaneError, msg=prompt):
                cli.load_prompt(prompt, None, "execute")

        accepted = (
            "edit common/scripts/deploy.py",
            "the deploy script",
            "DEPLOYMENT_TYPE: docker",
            "docs/deploy-runbook.md",
            "after the deployment succeeds",
            "git push",
            "deploy.py needs an update",
            "Deploy-runbook.md is stale",
            "deploy/ holds the manifests",
        )
        for prompt in accepted:
            self.assertEqual(
                cli.load_prompt(prompt, None, "execute"), prompt, msg=prompt
            )

    def test_review_unsafe_vcs_verbs_match_instruction_not_noun(self) -> None:
        """Review mode must still refuse an instruction to write history, while
        letting a reviewer TALK about commits — its most common job.

        The bare `\\b(?:commit|push|merge|deploy)\\b` this replaces made review
        mode unusable: "review commit a321031a" read as an instruction to
        commit, and so did a prompt telling the reviewer "do not commit, push"
        — the noun and the negation both matched. Same verb/imperative shape
        the EXECUTE_UNSAFE "deploy" alternative already uses.
        """
        rejected = (
            "Commit the fix.",
            "push the branch",
            "merge the PR",
            "commit and push",
            "run the commit",
            "Please commit your changes",
            "deploy it to dev",
            "Commit this and push to origin.",
            "Merge the branch into dev.",
            "Now push your changes.",
            "deploy to production",
            "Push the fix.",
            "commit your work",
            "Deploy that to staging",
            "Fix it, then commit the result.",
            "Merge it into dev.",
            "push to origin now",
            # Command form. lane-governance.md "Review mode" forbids commit /
            # push / deploy outright, so `git push` is an instruction to do the
            # forbidden thing, not prose. (EXECUTE_UNSAFE deliberately accepts
            # a plain `git push` — that is execute mode, a different boundary;
            # do not copy its cases here.)
            "git push",
            "git commit -a",
            "git merge dev",
            "```\ngit push origin main\n```",
            "run `git commit -am wip`",
        )
        for prompt in rejected:
            with self.assertRaises(cli.SideLaneError, msg=prompt):
                cli.load_prompt(prompt, None, "review")

        accepted = (
            # The two that actually blocked a real review run.
            "review commit a321031a",
            "Do NOT edit, write, stage, commit, push.",
            # Nouns, and sentence-initial verbs heading a noun phrase.
            "the commit under review",
            "merge commit 7a8ed4ab",
            "check the commit message",
            "Merge commit 7a8ed4ab introduced the regression.",
            "Push notifications are broken on iOS.",
            "Deploy scripts live in common/scripts.",
            "Commit messages should be conventional.",
            "Merge conflicts appear in the lockfile.",
            "commit a321031a on branch foo",
            "Compare 7a8ed4ab..a321031a and report.",
            "Review the commit and report findings.",
            "a squash-merge consumed the branch",
            "Do not edit, write, stage, commit, push, or create any file.",
            # Negations are PROHIBITIONS and must reach the reviewer. The
            # comma-list form above passes for the wrong reason — nothing
            # follows the verb — so these object-bearing ones are the real
            # coverage.
            "Do not commit the changes.",
            "Do not push the branch.",
            "Don't commit the result.",
            "Never merge the PR yourself.",
            "You must not push the branch.",
            # Prose that merely names a command, vs. the command itself above.
            "inspect git push output",
            # Filenames/identifiers, as the EXECUTE_UNSAFE deploy test asserts.
            "deploy.py needs no change",
            "the deploy script",
            "DEPLOYMENT_TYPE: docker",
            "deploy/ holds the manifests",
            "after the deployment succeeds",
            "Deploy-runbook.md is stale",
        )
        for prompt in accepted:
            self.assertEqual(
                cli.load_prompt(prompt, None, "review"), prompt, msg=prompt
            )

    def test_governance_rejects_symlinked_entrypoint(self) -> None:
        repo = self.repo()
        target = repo / "AGENTS.real.md"
        target.write_text(
            (repo / "AGENTS.md").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (repo / "AGENTS.md").unlink()
        (repo / "AGENTS.md").symlink_to(target)
        with self.assertRaisesRegex(cli.SideLaneError, "regular repository file"):
            cli.validate_governance(str(repo))

    def test_parser_rejects_extra_credentials_and_requires_host(self) -> None:
        parser = cli.make_parser()
        base = [
            "run",
            "--host",
            "claude",
            "--provider",
            "claude",
            "--model",
            "claude-sonnet-5",
            "--repo",
            ".",
            "--prompt",
            "Review",
        ]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(base + ["--api-key", "secret"])
        parsed = parser.parse_args(["auth-status", "--host", "devin"])
        self.assertEqual(parsed.host, "devin")

    def test_mocked_devin_route_dispatches_through_normal_cli_launch(self) -> None:
        config = cli.load_config()
        model = "swe-2-medium"
        config["providers"]["devin"] = {
            "gateway": "native-devin",
            "auth_method": "oauth",
            "billable": False,
            "routes": {
                "execute": {
                    "devin": {
                        "protocol": "native-devin",
                        "models": [model],
                        "model_configs": {
                            model: {
                                "identity_contract": {
                                    "requested_model": model,
                                    "resolved_model": model,
                                    "settings_precedence": "verified",
                                },
                                "qualification": {
                                    "verified": True,
                                    "verified_on": "2026-09-11",
                                    "source": "mocked local report",
                                },
                                "timeout_seconds": 600,
                            }
                        },
                    }
                }
            },
        }
        repo = self.repo()
        worktree = repo.parent / "devin-worktree"
        lane = mock.Mock(worktree=worktree, branch="side-lane/devin-1")
        result = LaneResult(
            ("devin",),
            0,
            worktree,
            "devin",
            "devin",
            "native-devin",
            model,
            "oauth",
            False,
            '{"model":"swe-2-medium"}',
            "",
            requested_model=model,
            resolved_model=model,
        )
        args = mock.Mock(
            host="devin",
            mode="execute",
            provider="devin",
            model=model,
            capability=[],
            lane_name="devin-1",
            approve_billable_route=False,
            worktree_root=None,
            verify=None,
        )
        with (
            mock.patch(
                "side_lane.cli._require_host_executable",
                return_value="/opt/hosts/devin",
            ),
            mock.patch("side_lane.cli.create_worktree", return_value=lane),
            mock.patch(
                "side_lane.cli.lane_delivery",
                return_value=LaneDelivery(committed=True, uncommitted=()),
            ),
            mock.patch("side_lane.cli.require_native_oauth") as auth,
            mock.patch(
                "side_lane.adapters.devin.launch", return_value=result
            ) as launch,
            mock.patch("side_lane.cli.git_status", return_value="## lane"),
            mock.patch(
                "side_lane.cli.write_audit", return_value=repo / ".git/audit.json"
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli._launch(args, config, repo, "Implement it"), 0)
        auth.assert_called_once_with("devin", executable="/opt/hosts/devin")
        self.assertEqual(
            launch.call_args.kwargs["model_config"]["timeout_seconds"], 600
        )
        self.assertEqual(
            launch.call_args.kwargs["model_config"]["identity_contract"][
                "resolved_model"
            ],
            model,
        )

    def test_capability_report_uses_auth_metadata_or_override_presence_only(
        self,
    ) -> None:
        config = cli.load_config()
        ready = mock.Mock(
            ready=True, as_dict=lambda: {"state": "ready", "method": "oauth"}
        )
        with (
            mock.patch("side_lane.cli.auth_status", return_value=ready),
            mock.patch("side_lane.cli.shutil.which", return_value="/bin/tool"),
            mock.patch(
                "side_lane.cli._discover_mcp_inventory",
                return_value=({"gitnexus"}, set()),
            ),
        ):
            native = cli._capability_report(
                config, "codex", "execute", "openai", "gpt-5.6-sol"
            )
        self.assertEqual(native["auth"], {"state": "ready", "method": "oauth"})
        self.assertFalse(native["capabilities"]["git-push"])
        self.assertEqual(native["capability_evidence"]["git-push"]["state"], "present")
        self.assertEqual(native["capability_evidence"]["gitnexus"]["state"], "present")
        with mock.patch("side_lane.cli.credential_present", return_value=False):
            glm = cli._capability_report(config, "claude", "execute", "glm", "glm-5.3")
        self.assertEqual(glm["configured_override"], "absent")
        self.assertTrue(glm["requires_one_run_approval"])

    def test_billable_confirmation_is_checked_before_credential_lookup(self) -> None:
        args = mock.Mock(
            host="claude",
            mode="execute",
            provider="glm",
            model="glm-5.3",
            capability=[],
            lane_name="review",
            approve_billable_route=False,
            worktree_root=None,
        )
        with mock.patch("side_lane.cli.read_credential") as read:
            with self.assertRaisesRegex(cli.SideLaneError, "explicit --approve"):
                cli._launch(args, cli.load_config(), self.repo(), "Review")
            read.assert_not_called()

    def test_metered_devin_selection_list_and_approval_gate_use_model_billing(
        self,
    ) -> None:
        config = self.devin_billing_config()
        provider, route = cli.select_route(
            config, "devin", "execute", "devin", "grok-4-6-low"
        )
        self.assertTrue(provider["billable"])
        self.assertTrue(route["billable"])
        with (
            mock.patch("side_lane.cli.load_config", return_value=config),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli.run(["list"]), 0)
        self.assertIn(
            "devin\texecute\tdevin\tnative-devin\tgrok-4-6-low\toauth\tbillable",
            output.getvalue(),
        )
        args = mock.Mock(
            host="devin",
            mode="execute",
            provider="devin",
            model="grok-4-6-low",
            capability=[],
            lane_name="metered",
            approve_billable_route=False,
            worktree_root=None,
        )
        with (
            mock.patch("side_lane.cli._require_host_executable") as executable,
            mock.patch("side_lane.cli.create_worktree") as create,
            mock.patch("side_lane.cli.require_native_oauth") as auth,
            mock.patch("side_lane.cli.read_credential") as read,
        ):
            with self.assertRaisesRegex(cli.SideLaneError, "explicit --approve"):
                cli._launch(args, config, self.repo(), "Implement")
        executable.assert_not_called()
        create.assert_not_called()
        auth.assert_not_called()
        read.assert_not_called()

    def test_approved_metered_devin_passes_effective_billing_to_adapter_and_audit(
        self,
    ) -> None:
        config = self.devin_billing_config()
        repo = self.repo()
        worktree = repo.parent / "metered-devin-worktree"
        lane = mock.Mock(worktree=worktree, branch="side-lane/metered")
        result = LaneResult(
            ("devin",),
            0,
            worktree,
            "devin",
            "devin",
            "native-devin",
            "grok-4-6-low",
            "oauth",
            True,
            "done",
            "",
            requested_model="grok-4-6-low",
            resolved_model="grok-4-6-low",
        )
        args = mock.Mock(
            host="devin",
            mode="execute",
            provider="devin",
            model="grok-4-6-low",
            capability=[],
            lane_name="metered",
            approve_billable_route=True,
            worktree_root=None,
            verify=None,
        )
        with (
            mock.patch(
                "side_lane.cli._require_host_executable", return_value="/opt/devin"
            ),
            mock.patch("side_lane.cli.create_worktree", return_value=lane),
            mock.patch(
                "side_lane.cli.lane_delivery",
                return_value=LaneDelivery(committed=True, uncommitted=()),
            ),
            mock.patch("side_lane.cli.require_native_oauth") as auth,
            mock.patch("side_lane.cli.read_credential") as read,
            mock.patch(
                "side_lane.adapters.devin.launch", return_value=result
            ) as launch,
            mock.patch("side_lane.cli.git_status", return_value="## metered"),
            mock.patch(
                "side_lane.cli.write_audit", return_value=repo / ".git/audit.json"
            ) as audit,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli._launch(args, config, repo, "Implement"), 0)
        auth.assert_called_once_with("devin", executable="/opt/devin")
        read.assert_not_called()
        self.assertTrue(launch.call_args.kwargs["provider_config"]["billable"])
        self.assertTrue(launch.call_args.kwargs["model_config"]["billable"])
        self.assertTrue(audit.call_args.kwargs["billable"])

    def test_included_devin_model_remains_non_billable(self) -> None:
        config = self.devin_billing_config()
        provider, route = cli.select_route(
            config, "devin", "execute", "devin", "swe-2-medium"
        )
        self.assertFalse(provider["billable"])
        self.assertFalse(route["billable"])
        with (
            mock.patch("side_lane.cli.load_config", return_value=config),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli.run(["list"]), 0)
        self.assertIn(
            "devin\texecute\tdevin\tnative-devin\tswe-2-medium\toauth\tsubscription",
            output.getvalue(),
        )

    def test_model_billing_overrides_are_rejected_outside_native_devin_oauth(
        self,
    ) -> None:
        provider_key = cli.load_config()
        provider_key["providers"]["glm"]["routes"]["execute"]["claude"][
            "model_configs"
        ] = {"glm-5.3": {"billable": False}}
        with self.assertRaisesRegex(cli.SideLaneError, "only for native Devin OAuth"):
            cli.select_route(provider_key, "claude", "execute", "glm", "glm-5.3")
        native_oauth = cli.load_config()
        native_oauth["providers"]["claude"]["routes"]["execute"]["claude"][
            "model_configs"
        ] = {"claude-sonnet-5": {"billable": True}}
        with self.assertRaisesRegex(cli.SideLaneError, "only for native Devin OAuth"):
            cli.select_route(
                native_oauth, "claude", "execute", "claude", "claude-sonnet-5"
            )

    def test_adapter_errors_are_rendered_as_expected_cli_failures(self) -> None:
        with (
            mock.patch(
                "side_lane.cli.run", side_effect=cli.CodexAdapterError("bad adapter")
            ),
            contextlib.redirect_stderr(io.StringIO()) as errors,
            self.assertRaisesRegex(SystemExit, "2"),
        ):
            cli.main()
        self.assertIn("side-lane: bad adapter", errors.getvalue())

    def test_required_capability_fails_before_auth_or_key(self) -> None:
        args = mock.Mock(
            host="codex",
            mode="execute",
            provider="openai",
            model="gpt-5.6-terra",
            capability=["gitnexus"],
            lane_name="worker",
            approve_billable_route=False,
            worktree_root=None,
        )
        with (
            mock.patch(
                "side_lane.cli._capability_report",
                return_value={
                    "capability_evidence": {"gitnexus": {"state": "unknown"}},
                    "capabilities": {"gitnexus": False},
                },
            ),
            mock.patch("side_lane.cli.require_native_oauth") as auth,
        ):
            with self.assertRaisesRegex(
                cli.SideLaneError, "required capabilities unavailable"
            ):
                cli._launch(args, cli.load_config(), self.repo(), "Implement")
            auth.assert_not_called()

    def test_native_oauth_failure_never_falls_back_to_key(self) -> None:
        # verify=None keeps the auto-created Mock attribute from reading as a
        # passed --verify flag (the execute-only guard would reject it first).
        args = mock.Mock(
            host="codex",
            mode="review",
            provider="openai",
            model="gpt-5.6-terra",
            capability=[],
            lane_name="review",
            approve_billable_route=False,
            worktree_root=None,
            verify=None,
        )
        lane = mock.Mock(worktree=self.repo())
        with (
            mock.patch(
                "side_lane.cli._require_host_executable",
                return_value="/opt/hosts/codex",
            ),
            mock.patch("side_lane.cli.create_worktree", return_value=lane),
            mock.patch("side_lane.cli.dispose_clean_worktree"),
            mock.patch(
                "side_lane.cli.require_native_oauth",
                side_effect=cli.AuthError("signed out"),
            ),
            mock.patch("side_lane.cli.read_credential") as read,
        ):
            with self.assertRaises(cli.AuthError):
                cli._launch(args, cli.load_config(), self.repo(), "Review")
            read.assert_not_called()

    def test_review_uses_audited_disposable_worktree(self) -> None:
        repo = self.repo()
        worktree = repo.parent / "review-worktree"
        lane = mock.Mock(worktree=worktree, branch="side-lane/review-1")
        result = LaneResult(
            ("claude",),
            0,
            worktree,
            "claude",
            "claude",
            "native-claude",
            "claude-sonnet-5",
            "oauth",
            False,
            "finding: bug in api.py",
            "",
            requested_model="claude-sonnet-5",
            resolved_model="claude-sonnet-5",
            usage={"input_tokens": 12},
            provider_artifact="/tmp/provider.json",
        )
        args = mock.Mock(
            host="claude",
            mode="review",
            provider="claude",
            model="claude-sonnet-5",
            capability=[],
            lane_name="review",
            approve_billable_route=False,
            worktree_root=None,
            verify=None,
        )
        with (
            mock.patch(
                "side_lane.cli._require_host_executable",
                return_value="/opt/hosts/claude",
            ),
            mock.patch("side_lane.cli.create_worktree", return_value=lane) as create,
            mock.patch("side_lane.cli.require_native_oauth"),
            mock.patch(
                "side_lane.adapters.claude.launch", return_value=result
            ) as launch,
            mock.patch("side_lane.cli.git_status", return_value="## review"),
            mock.patch(
                "side_lane.cli.write_audit", return_value=repo / ".git/audit.json"
            ) as audit,
            mock.patch("side_lane.cli.dispose_clean_worktree") as dispose,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli._launch(args, cli.load_config(), repo, "Review"), 0)
        create.assert_called_once_with(repo, "review", worktree_root=None)
        self.assertEqual(launch.call_args.kwargs["worktree"], worktree)
        self.assertEqual(launch.call_args.kwargs["executable"], "/opt/hosts/claude")
        self.assertEqual(audit.call_args.kwargs["stdout"], "finding: bug in api.py")
        self.assertEqual(audit.call_args.kwargs["requested_model"], "claude-sonnet-5")
        self.assertEqual(audit.call_args.kwargs["resolved_model"], "claude-sonnet-5")
        self.assertEqual(audit.call_args.kwargs["usage"], {"input_tokens": 12})
        self.assertEqual(
            audit.call_args.kwargs["provider_artifact"], "/tmp/provider.json"
        )
        dispose.assert_called_once_with(lane)
        lines = output.getvalue().splitlines()
        summary = json.loads("\n".join(lines[1:]))
        self.assertTrue(summary["worktree_disposed"])
        self.assertEqual(summary["result_artifact"], str(repo / ".git/audit.json"))

    def test_failed_result_persistence_preserves_the_worktree(self) -> None:
        repo = self.repo()
        worktree = repo.parent / "review-worktree"
        lane = mock.Mock(worktree=worktree, branch="side-lane/review-1")
        result = LaneResult(
            ("claude",),
            0,
            worktree,
            "claude",
            "claude",
            "native-claude",
            "claude-sonnet-5",
            "oauth",
            False,
            "finding: bug",
            "",
        )
        args = mock.Mock(
            host="claude",
            mode="review",
            provider="claude",
            model="claude-sonnet-5",
            capability=[],
            lane_name="review",
            approve_billable_route=False,
            worktree_root=None,
            verify=None,
        )
        with (
            mock.patch(
                "side_lane.cli._require_host_executable",
                return_value="/opt/hosts/claude",
            ),
            mock.patch("side_lane.cli.create_worktree", return_value=lane),
            mock.patch(
                "side_lane.cli.lane_delivery",
                return_value=LaneDelivery(committed=True, uncommitted=()),
            ),
            mock.patch("side_lane.cli.require_native_oauth"),
            mock.patch("side_lane.adapters.claude.launch", return_value=result),
            mock.patch("side_lane.cli.git_status", return_value="## review"),
            mock.patch("side_lane.cli.write_audit", side_effect=OSError("disk full")),
            mock.patch("side_lane.cli.dispose_clean_worktree") as dispose,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(OSError):
                cli._launch(args, cli.load_config(), repo, "Review")
        dispose.assert_not_called()

    def _delivered_lane(
        self,
        delivery,
        *,
        no_publish=False,
        publish_error=None,
        verify_command=None,
        verify_result=None,
    ):
        """Drive _launch through a finished execute lane; report what happened.

        The push and the verification command are both mocked: these tests
        pin the CLI contract (when a push or a verification is attempted,
        what the summary claims, what the exit code is), while
        test_worktrees.py covers the real invocations.
        """
        repo = self.repo()
        worktree = repo.parent / "execute-worktree"
        lane = mock.Mock(worktree=worktree, branch="side-lane/task-1")
        result = LaneResult(
            ("claude",),
            0,
            worktree,
            "claude",
            "claude",
            "native-claude",
            "claude-sonnet-5",
            "oauth",
            False,
            "",
            "",
        )
        args = mock.Mock(
            host="claude",
            mode="execute",
            provider="claude",
            model="claude-sonnet-5",
            capability=[],
            lane_name="task",
            approve_billable_route=False,
            worktree_root=None,
            allow_no_commit=False,
            no_publish=no_publish,
            verify=verify_command,
        )
        publish = mock.MagicMock(return_value="origin/side-lane/task-1")
        if publish_error is not None:
            publish.side_effect = publish_error
        verify = mock.MagicMock(return_value=verify_result)
        with (
            mock.patch(
                "side_lane.cli._require_host_executable",
                return_value="/opt/hosts/claude",
            ),
            mock.patch("side_lane.cli.create_worktree", return_value=lane),
            mock.patch("side_lane.cli.require_native_oauth"),
            mock.patch("side_lane.adapters.claude.launch", return_value=result),
            mock.patch("side_lane.cli.lane_delivery", return_value=delivery),
            mock.patch("side_lane.cli.git_status", return_value="## task"),
            mock.patch(
                "side_lane.cli.write_audit", return_value=repo / ".git/audit.json"
            ),
            mock.patch("side_lane.cli.dispose_clean_worktree"),
            mock.patch("side_lane.cli.publish_lane_branch", publish),
            mock.patch("side_lane.cli.verify_lane", verify),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            code = cli._launch(args, cli.load_config(), repo, "Implement")
        return (
            code,
            _only_summary(output.getvalue()),
            errors.getvalue(),
            publish,
            lane,
            verify,
        )

    def test_delivered_lane_publishes_its_branch(self) -> None:
        code, summary, _errors, publish, lane, _verify = self._delivered_lane(
            LaneDelivery(committed=True, uncommitted=())
        )
        self.assertEqual(code, 0)
        publish.assert_called_once_with(lane)
        self.assertTrue(summary["published"])
        self.assertEqual(summary["published_ref"], "origin/side-lane/task-1")

    def test_lane_that_committed_nothing_is_not_published(self) -> None:
        code, summary, _errors, publish, _lane, _verify = self._delivered_lane(
            LaneDelivery(committed=False, uncommitted=())
        )
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        publish.assert_not_called()
        self.assertNotIn("published", summary)

    def test_lane_that_left_work_uncommitted_is_not_published(self) -> None:
        code, summary, _errors, publish, _lane, _verify = self._delivered_lane(
            LaneDelivery(committed=True, uncommitted=("forgotten.py",))
        )
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        publish.assert_not_called()
        self.assertNotIn("published", summary)

    def test_publish_failure_is_reported_but_not_fatal(self) -> None:
        code, summary, errors, _publish, _lane, _verify = self._delivered_lane(
            LaneDelivery(committed=True, uncommitted=()),
            publish_error=WorktreeError(
                "cannot publish lane branch side-lane/task-1 to origin: "
                "'origin' does not appear to be a git repository"
            ),
        )
        # The work is committed with or without the remote; failing the lane
        # here would be worse than not publishing at all.
        self.assertEqual(code, 0)
        self.assertFalse(summary["published"])
        self.assertIn(
            "does not appear to be a git repository", summary["publish_error"]
        )
        self.assertIn("lane delivered but could not publish the branch", errors)
        # One warning line, and the published fields rode in the single
        # summary print rather than a second one.
        self.assertEqual(len(errors.splitlines()), 1)

    def test_no_publish_flag_skips_the_push_entirely(self) -> None:
        code, summary, _errors, publish, _lane, _verify = self._delivered_lane(
            LaneDelivery(committed=True, uncommitted=()), no_publish=True
        )
        self.assertEqual(code, 0)
        publish.assert_not_called()
        self.assertIsNone(summary["published"])
        self.assertNotIn("published_ref", summary)

    def test_verify_pass_reports_success_and_keeps_exit_zero(self) -> None:
        ok = VerifyResult(command="make test", exit_code=0, output="ok")
        code, summary, errors, _publish, _lane, verify = self._delivered_lane(
            LaneDelivery(committed=True, uncommitted=()),
            verify_command="make test",
            verify_result=ok,
        )
        self.assertEqual(code, 0)
        self.assertTrue(summary["verified"])
        self.assertEqual(summary["verify_command"], "make test")
        self.assertEqual(summary["verify_exit"], 0)
        self.assertEqual(summary["verify_output"], "ok")
        verify.assert_called_once()
        self.assertNotIn("verification failed", errors)

    def test_verify_failure_fails_the_run_with_five(self) -> None:
        bad = VerifyResult(
            command="make test", exit_code=2, output="FAILED (failures=9)"
        )
        code, summary, errors, _publish, _lane, _verify = self._delivered_lane(
            LaneDelivery(committed=True, uncommitted=()),
            verify_command="make test",
            verify_result=bad,
        )
        self.assertEqual(cli.LANE_VERIFY_FAILED, 5)
        self.assertEqual(code, cli.LANE_VERIFY_FAILED)
        self.assertFalse(summary["verified"])
        self.assertEqual(summary["verify_exit"], 2)
        self.assertIn("FAILED (failures=9)", summary["verify_output"])
        # The stderr message carries the command and the output tail: without
        # both, the operator is back to trusting prose.
        self.assertIn("make test", errors)
        self.assertIn("FAILED (failures=9)", errors)

    def test_a_failed_verification_still_publishes(self) -> None:
        # Preserving the branch is what stops work being stranded on one
        # machine, and failing work is exactly the work someone must be able
        # to look at. A verify failure must not skip the push.
        bad = VerifyResult(command="make test", exit_code=1, output="nope")
        code, summary, _errors, publish, lane, _verify = self._delivered_lane(
            LaneDelivery(committed=True, uncommitted=()),
            verify_command="make test",
            verify_result=bad,
        )
        self.assertEqual(code, cli.LANE_VERIFY_FAILED)
        publish.assert_called_once_with(lane)
        self.assertTrue(summary["published"])
        self.assertEqual(summary["published_ref"], "origin/side-lane/task-1")

    def test_no_verify_command_means_no_verdict_and_no_run(self) -> None:
        code, summary, _errors, _publish, _lane, verify = self._delivered_lane(
            LaneDelivery(committed=True, uncommitted=())
        )
        self.assertEqual(code, 0)
        self.assertIsNone(summary["verified"])
        verify.assert_not_called()
        self.assertNotIn("verify_command", summary)

    def test_an_undelivered_lane_is_not_verified(self) -> None:
        # It already fails (exit 3); running its tests answers nothing.
        code, summary, _errors, publish, _lane, verify = self._delivered_lane(
            LaneDelivery(committed=False, uncommitted=()), verify_command="make test"
        )
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        verify.assert_not_called()
        self.assertIsNone(summary["verified"])
        publish.assert_not_called()

    def test_verify_is_rejected_outside_execute_mode(self) -> None:
        # A review lane disposes its worktree before verification could run;
        # accepting the flag there would be a silent no-op.
        args = mock.Mock(
            host="claude",
            mode="review",
            provider="claude",
            model="claude-sonnet-5",
            capability=[],
            lane_name="review",
            approve_billable_route=False,
            worktree_root=None,
            verify="make test",
        )
        with self.assertRaisesRegex(cli.SideLaneError, "execute mode"):
            cli._launch(args, cli.load_config(), self.repo(), "Review")

    def test_recommend_profile_rejects_secret_usage_and_keeps_glm_explicit(
        self,
    ) -> None:
        for field in ("api_key", "credential", "secret", "quota", "billing", "usage"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "profile.json"
                path.write_text(json.dumps({field: "x"}), encoding="utf-8")
                with self.assertRaises(cli.SideLaneError):
                    cli.load_recommendation_profile(str(path))
        with (
            mock.patch("side_lane.cli.auth_status") as status,
            mock.patch("side_lane.cli.credential_present", return_value=False),
        ):
            status.return_value.ready = True
            self.assertFalse(
                any(route[0] == "glm" for route in cli._ready_routes(cli.load_config()))
            )

    def test_offline_evaluate_command_never_calls_provider_or_credentials(self) -> None:
        path = (
            Path(__file__).parent / "fixtures" / "evaluation" / "offline-evidence.json"
        )
        with (
            mock.patch("side_lane.cli.read_credential") as secret,
            mock.patch("side_lane.adapters.codex.run_codex") as codex,
            mock.patch("side_lane.adapters.claude.launch") as claude,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
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
            profile_path.write_text(
                json.dumps(
                    {
                        "coordinator_host": "claude",
                        "mode": "execute",
                        "policy": "cost-optimized",
                        "task_band": "debugging",
                        "quality_floor": 90,
                        "required_connectors": ["gitnexus"],
                        "required_capabilities": ["workspace-write"],
                        "host_cost_state": {
                            "claude": "extra-usage",
                            "codex": "included-oauth",
                        },
                    }
                ),
                encoding="utf-8",
            )
            args = mock.Mock(repo=directory, profile=str(profile_path))
            reports = {
                "codex": {
                    "mcp_connectors": ["gitnexus"],
                    "capabilities": {"workspace-write": True},
                },
                "claude": {
                    "mcp_connectors": [],
                    "capabilities": {"workspace-write": True},
                },
            }
            with (
                mock.patch(
                    "side_lane.cli.validate_governance", return_value=Path(directory)
                ),
                mock.patch(
                    "side_lane.cli._capability_report",
                    side_effect=lambda _c, host, *_a: reports[host],
                ),
                mock.patch("side_lane.cli._ready_routes", return_value=frozenset()),
                mock.patch("side_lane.cli.routing.load_catalog", return_value={}),
                mock.patch(
                    "side_lane.cli.routing.recommend", return_value={"winner": None}
                ) as recommend,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli._recommend(args, cli.load_config()), 0)
            normalized = recommend.call_args.args[1]
            self.assertEqual(normalized["coordinator_host"], "claude")
            self.assertEqual(
                normalized["host_capabilities"]["codex"]["available_connectors"],
                ["gitnexus"],
            )
            self.assertEqual(
                normalized["host_capabilities"]["claude"]["available_connectors"], []
            )

    def test_recommend_execute_staffing_accepts_only_exact_configured_playwright_presence(
        self,
    ) -> None:
        report = {
            "mcp_connectors": ["gitnexus", "playwright"],
            "capabilities": {
                "workspace-write": True,
                "playwright": False,
                "gitnexus": False,
                "git-push": False,
                "workflow-write": False,
            },
            "capability_evidence": {
                "playwright": {"state": "present"},
                "git-push": {"state": "present"},
                "workflow-write": {"state": "present"},
            },
        }
        execute = cli._recommendation_host_snapshot(report, "execute")
        self.assertEqual(execute["available_connectors"], ["gitnexus", "playwright"])
        self.assertEqual(
            execute["available_capabilities"], ["playwright", "workspace-write"]
        )
        review_report = {
            **report,
            "capabilities": {
                **report["capabilities"],
                "playwright": True,
                "gitnexus": True,
            },
        }
        review = cli._recommendation_host_snapshot(review_report, "review")
        self.assertEqual(review["available_connectors"], [])
        self.assertEqual(review["available_capabilities"], ["workspace-write"])

    def test_list_exposes_provider_gateway_auth_and_billing(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.run(["list"]), 0)
        text = output.getvalue()
        self.assertIn("openai\tnative-codex", text)
        self.assertIn("glm\tdirect-zai\tglm-5.3\tprovider-key\tbillable", text)
        for model in ("claude-opus-5", "claude-fable-5-1", "claude-sonnet-5", "claude-haiku-4-5"):
            self.assertIn(f"claude\texecute\tanthropic\tdirect-anthropic\t{model}\tprovider-key\tbillable", text)
        self.assertNotIn("claude\treview\tanthropic\t", text)
        for model in ("gpt-5.5", "gpt-5.5-pro", "gpt-5.3-codex"):
            self.assertIn(f"codex\texecute\topenai-api-key\tcodex-api-key\t{model}\tprovider-key\tbillable", text)
        self.assertNotIn("codex\treview\topenai-api-key\t", text)

    def test_candidates_is_offline_research_metadata(self) -> None:
        with (
            mock.patch("side_lane.cli.credential_present") as credential,
            mock.patch("side_lane.cli.auth_status") as auth,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli.run(["candidates", "--json"]), 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload)
        self.assertTrue(all(item["executable"] is False for item in payload))
        self.assertTrue(all(item["credential_checked"] is False for item in payload))
        credential.assert_not_called()
        auth.assert_not_called()


class ConnectorDiscoveryTests(unittest.TestCase):
    def test_project_connector_files_are_host_specific(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".mcp.json").write_text(
                '{"mcpServers": {"playwright": {"command": "npx"}}}', encoding="utf-8"
            )
            (repo / ".codex").mkdir()
            (repo / ".codex" / "config.toml").write_text(
                '[mcp_servers.gitnexus]\ncommand = "gitnexus"\n', encoding="utf-8"
            )
            with (
                mock.patch("side_lane.cli.Path.home", return_value=repo / "no-home"),
                mock.patch.dict(
                    os.environ, {"CODEX_HOME": str(repo / "no-codex-home")}
                ),
            ):
                claude_names = cli._discover_mcp_names("claude", repo)
                codex_names = cli._discover_mcp_names("codex", repo)
        self.assertEqual(claude_names, {"playwright"})
        self.assertEqual(codex_names, {"gitnexus"})

    def test_claude_per_project_entries_from_other_projects_are_out_of_scope(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            repo = Path(directory) / "repo"
            home.mkdir()
            repo.mkdir()
            (home / ".claude.json").write_text(
                '{"mcpServers":{"gitnexus":{"command":"g"}},'
                '"projects":{"/elsewhere":{"mcpServers":{"codegraph":{"command":"c"},"contentful":{"command":"x"}}},'
                + json.dumps(str(repo))
                + ':{"mcpServers":{"zoom":{"command":"z"}}}}}',
                encoding="utf-8",
            )
            (repo / ".mcp.json").write_text(
                '{"mcpServers": {"playwright": {"command": "npx"}}}', encoding="utf-8"
            )
            with mock.patch("side_lane.cli.Path.home", return_value=home):
                in_scope, out_of_scope = cli._discover_mcp_inventory("claude", repo)
                names = cli._discover_mcp_names("claude", repo)
                with mock.patch("side_lane.cli.shutil.which", return_value="/bin/tool"):
                    report = cli._capability_report(
                        cli.load_config(), "claude", "execute", None, None, repo
                    )
        # Root-level user config and the repo's own .mcp.json count; per-project
        # entries do not, even when keyed by this repo — a lane runs in a fresh worktree.
        self.assertEqual(in_scope, {"gitnexus", "playwright"})
        self.assertEqual(out_of_scope, {"codegraph", "contentful", "zoom"})
        self.assertEqual(names, in_scope)
        self.assertEqual(report["mcp_connectors"], ["gitnexus", "playwright"])
        self.assertEqual(
            report["mcp_connectors_out_of_scope"], ["codegraph", "contentful", "zoom"]
        )
        self.assertEqual(report["capability_evidence"]["gitnexus"]["state"], "present")
        codegraph = report["capability_evidence"]["codegraph"]
        self.assertEqual(codegraph["state"], "unknown")
        self.assertIn("another project's scope", codegraph["basis"])
        self.assertNotIn("/elsewhere", json.dumps(report))

    def test_empty_codex_home_falls_back_to_the_default_not_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd, home = Path(directory) / "cwd", Path(directory) / "home"
            cwd.mkdir()
            (home / ".codex").mkdir(parents=True)
            (cwd / "config.toml").write_text(
                '[mcp_servers.leaked]\ncommand = "x"\n', encoding="utf-8"
            )
            (home / ".codex" / "config.toml").write_text(
                '[mcp_servers.expected]\ncommand = "x"\n', encoding="utf-8"
            )
            previous = os.getcwd()
            os.chdir(cwd)
            try:
                with (
                    mock.patch("side_lane.cli.Path.home", return_value=home),
                    mock.patch.dict(os.environ, {"CODEX_HOME": "  "}),
                ):
                    names = cli._discover_mcp_names("codex", None)
            finally:
                os.chdir(previous)
        self.assertEqual(names, {"expected"})

    def test_devin_uses_only_native_mcp_files_and_requires_exact_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            repo = Path(directory) / "repo"
            (home / ".config" / "devin").mkdir(parents=True)
            (home / ".claude").mkdir()
            (repo / ".devin").mkdir(parents=True)
            (home / ".config" / "devin" / "mcp_config.json").write_text(
                '{"mcpServers":{"playwright-local":{"command":"native-near"},'
                '"gitnexus":{"command":"native-exact"}}}',
                encoding="utf-8",
            )
            (repo / ".devin" / "mcp_config.json").write_text(
                '{"mcpServers":{"codegraph":{"command":"native-project"}}}',
                encoding="utf-8",
            )
            (repo / ".devin" / "mcp_config.local.json").write_text(
                '{"mcpServers":{"playwright":{"command":"native-local"}}}',
                encoding="utf-8",
            )
            (home / ".claude.json").write_text(
                '{"mcpServers":{"claude-only":{"command":"ignored"}}}', encoding="utf-8"
            )
            (home / ".claude" / "settings.json").write_text(
                '{"mcpServers":{"claude-settings":{"command":"ignored"}}}',
                encoding="utf-8",
            )
            (repo / ".mcp.json").write_text(
                '{"mcpServers":{"claude-project":{"command":"ignored"}}}',
                encoding="utf-8",
            )
            with (
                mock.patch("side_lane.cli.Path.home", return_value=home),
                mock.patch.dict(os.environ, {"APPDATA": ""}),
                mock.patch("side_lane.cli._host_executable", return_value="/opt/devin"),
                mock.patch(
                    "side_lane.cli.auth_status",
                    return_value=mock.Mock(as_dict=lambda: {"state": "ready"}),
                ),
            ):
                names, out_of_scope = cli._discover_mcp_inventory("devin", repo)
                report = cli._capability_report(
                    {
                        "providers": {},
                        "capabilities": ["playwright", "gitnexus", "codegraph"],
                    },
                    "devin",
                    "execute",
                    None,
                    None,
                    repo,
                )
        self.assertEqual(
            names, {"playwright-local", "playwright", "gitnexus", "codegraph"}
        )
        self.assertEqual(out_of_scope, set())
        self.assertFalse({"claude-only", "claude-settings", "claude-project"} & names)
        self.assertEqual(
            report["capability_evidence"]["playwright"]["state"], "present"
        )
        self.assertEqual(report["capability_evidence"]["gitnexus"]["state"], "present")
        self.assertEqual(report["capability_evidence"]["codegraph"]["state"], "present")

    def test_devin_near_match_connector_names_do_not_satisfy_capabilities(self) -> None:
        with (
            mock.patch("side_lane.cli._host_executable", return_value="/opt/devin"),
            mock.patch(
                "side_lane.cli._discover_mcp_inventory",
                return_value=({"playwright-local", "gitnexus-local"}, set()),
            ),
        ):
            report = cli._capability_report(
                {"providers": {}, "capabilities": ["playwright", "gitnexus"]},
                "devin",
                "execute",
                None,
                None,
            )
        self.assertEqual(
            report["capability_evidence"]["playwright"]["state"], "unavailable"
        )
        self.assertEqual(
            report["capability_evidence"]["gitnexus"]["state"], "name-mismatch"
        )


class ExecuteLanePermissionTests(SideLaneTests):
    def test_playwright_capability_is_reported_from_connector_names_only(self) -> None:
        config = cli.load_config()
        self.assertIn("playwright", config["capabilities"])
        with (
            mock.patch("side_lane.cli.shutil.which", return_value="/bin/tool"),
            mock.patch(
                "side_lane.cli._discover_mcp_inventory",
                return_value=({"playwright", "gitnexus"}, set()),
            ),
        ):
            report = cli._capability_report(config, "claude", "execute", None, None)
        self.assertEqual(
            report["capability_evidence"]["playwright"]["state"], "present"
        )
        self.assertIn("host_support_dir", report)
        with (
            mock.patch("side_lane.cli.shutil.which", return_value="/bin/tool"),
            mock.patch(
                "side_lane.cli._discover_mcp_inventory", return_value=(set(), set())
            ),
        ):
            report = cli._capability_report(config, "claude", "execute", None, None)
        self.assertEqual(
            report["capability_evidence"]["playwright"]["state"], "unavailable"
        )
        with (
            mock.patch("side_lane.cli.shutil.which", return_value="/bin/tool"),
            mock.patch(
                "side_lane.cli._discover_mcp_inventory",
                return_value=({"playwright-local"}, set()),
            ),
        ):
            report = cli._capability_report(config, "claude", "execute", None, None)
        self.assertEqual(
            report["capability_evidence"]["playwright"]["state"], "unavailable"
        )

    def test_graph_capabilities_require_the_exact_connector_name(self) -> None:
        config = cli.load_config()
        for capability in ("gitnexus", "codegraph"):
            with (
                mock.patch("side_lane.cli.shutil.which", return_value="/bin/tool"),
                mock.patch(
                    "side_lane.cli._discover_mcp_inventory",
                    return_value=({capability, "playwright"}, set()),
                ),
            ):
                exact = cli._capability_report(config, "claude", "execute", None, None)
            self.assertEqual(
                exact["capability_evidence"][capability]["state"], "present"
            )
            with (
                mock.patch("side_lane.cli.shutil.which", return_value="/bin/tool"),
                mock.patch(
                    "side_lane.cli._discover_mcp_inventory",
                    return_value=({f"{capability}-local", capability.upper()}, set()),
                ),
            ):
                mismatch = cli._capability_report(
                    config, "claude", "execute", None, None
                )
            evidence = mismatch["capability_evidence"][capability]
            self.assertEqual(evidence["state"], "name-mismatch")
            self.assertIn(f"{capability}-local", evidence["basis"])
            self.assertIn(f"mcp__{capability}__*", evidence["basis"])
            self.assertFalse(mismatch["capabilities"][capability])
            with (
                mock.patch("side_lane.cli.shutil.which", return_value="/bin/tool"),
                mock.patch(
                    "side_lane.cli._discover_mcp_inventory", return_value=(set(), set())
                ),
            ):
                absent = cli._capability_report(config, "claude", "execute", None, None)
            self.assertEqual(
                absent["capability_evidence"][capability]["state"], "unknown"
            )
            # Codex renders no fixed mcp__ namespace grants, so a near-miss name stays usable there.
            with (
                mock.patch("side_lane.cli.shutil.which", return_value="/bin/tool"),
                mock.patch(
                    "side_lane.cli._discover_mcp_inventory",
                    return_value=({f"{capability}-local"}, set()),
                ),
            ):
                codex = cli._capability_report(config, "codex", "execute", None, None)
            self.assertEqual(
                codex["capability_evidence"][capability]["state"], "present"
            )

    def test_execute_launch_forwards_capabilities_and_reports_allowed_tools(
        self,
    ) -> None:
        repo = self.repo()
        worktree = repo.parent / "execute-worktree"
        lane = mock.Mock(worktree=worktree, branch="side-lane/task-1")
        result = LaneResult(
            ("claude", "--allowedTools", "Bash(pnpm *)"),
            0,
            worktree,
            "claude",
            "claude",
            "native-claude",
            "claude-sonnet-5",
            "oauth",
            False,
            "done",
            "",
            capabilities=("shell",),
            allowed_tools=("Read", "Bash(pnpm *)"),
        )
        args = mock.Mock(
            host="claude",
            mode="execute",
            provider="claude",
            model="claude-sonnet-5",
            capability=["shell", "shell"],
            lane_name="task",
            approve_billable_route=False,
            worktree_root=None,
            verify=None,
        )
        readiness = {
            "capability_evidence": {"shell": {"state": "verified"}},
            "capabilities": {"shell": True},
        }
        with (
            mock.patch(
                "side_lane.cli._require_host_executable",
                return_value="/opt/hosts/claude",
            ),
            mock.patch("side_lane.cli._capability_report", return_value=readiness),
            mock.patch("side_lane.cli.create_worktree", return_value=lane),
            mock.patch(
                "side_lane.cli.lane_delivery",
                return_value=LaneDelivery(committed=True, uncommitted=()),
            ),
            mock.patch("side_lane.cli.require_native_oauth"),
            mock.patch(
                "side_lane.adapters.claude.launch", return_value=result
            ) as launch,
            mock.patch("side_lane.cli.git_status", return_value="## task"),
            mock.patch(
                "side_lane.cli.write_audit", return_value=repo / ".git/audit.json"
            ),
            mock.patch("side_lane.cli.dispose_clean_worktree") as dispose,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
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
        result = LaneResult(
            ("codex",),
            0,
            worktree,
            "codex",
            "openai",
            "native-codex",
            "gpt-5.6-sol",
            "oauth",
            False,
            "",
            "",
        )
        args = mock.Mock(
            host="codex",
            mode="execute",
            provider="openai",
            model="gpt-5.6-sol",
            capability=[],
            lane_name="task",
            approve_billable_route=False,
            worktree_root=None,
            verify=None,
        )
        with (
            mock.patch(
                "side_lane.cli._require_host_executable", return_value="/bundle/codex"
            ),
            mock.patch(
                "side_lane.cli.host_support_dir", return_value="/bundle"
            ) as support,
            mock.patch("side_lane.cli.create_worktree", return_value=lane),
            mock.patch(
                "side_lane.cli.lane_delivery",
                return_value=LaneDelivery(committed=True, uncommitted=()),
            ),
            mock.patch("side_lane.cli.require_native_oauth"),
            mock.patch(
                "side_lane.adapters.codex.run_codex", return_value=result
            ) as run_codex,
            mock.patch("side_lane.cli.git_status", return_value=""),
            mock.patch(
                "side_lane.cli.write_audit", return_value=repo / ".git/audit.json"
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli._launch(args, cli.load_config(), repo, "Implement"), 0)
        support.assert_any_call("codex", "/bundle/codex")
        self.assertEqual(run_codex.call_args.kwargs["support_dir"], "/bundle")


class LaunchCapabilityGateTests(SideLaneTests):
    def _args(self, capability):
        return mock.Mock(
            host="claude",
            mode="execute",
            provider="claude",
            model="claude-sonnet-5",
            capability=capability,
            lane_name="task",
            approve_billable_route=False,
            worktree_root=None,
        )

    def test_present_evidence_is_enough_to_launch_but_unknown_is_not(self) -> None:
        report = {
            "capability_evidence": {
                "playwright": {"state": "present"},
                "git-push": {"state": "present"},
                "shell": {"state": "verified"},
                "secret-use": {"state": "unknown"},
                "gitnexus": {"state": "unavailable"},
                "codegraph": {"state": "name-mismatch"},
            },
            "capabilities": {"shell": True},
        }
        with (
            mock.patch("side_lane.cli._capability_report", return_value=report),
            mock.patch(
                "side_lane.cli._require_host_executable",
                side_effect=cli.SideLaneError("stop here"),
            ),
        ):
            with self.assertRaisesRegex(cli.SideLaneError, "stop here"):
                cli._launch(
                    self._args(["playwright", "git-push", "shell"]),
                    cli.load_config(),
                    self.repo(),
                    "Implement",
                )
            with self.assertRaisesRegex(cli.SideLaneError, "unavailable: secret-use"):
                cli._launch(
                    self._args(["secret-use"]),
                    cli.load_config(),
                    self.repo(),
                    "Implement",
                )
            with self.assertRaisesRegex(cli.SideLaneError, "unavailable: gitnexus"):
                cli._launch(
                    self._args(["gitnexus"]),
                    cli.load_config(),
                    self.repo(),
                    "Implement",
                )
            with self.assertRaisesRegex(cli.SideLaneError, "unavailable: codegraph"):
                cli._launch(
                    self._args(["codegraph"]),
                    cli.load_config(),
                    self.repo(),
                    "Implement",
                )


if __name__ == "__main__":
    unittest.main()


class HostExecutableCliTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep host-executable resolution hermetic: a real override in the
        # developer's or CI environment must not steer these tests.
        patcher = mock.patch.dict(
            os.environ,
            {"SIDE_LANE_CODEX_EXECUTABLE": "", "SIDE_LANE_CLAUDE_EXECUTABLE": ""},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_launch_fails_before_worktree_when_host_executable_is_missing(self) -> None:
        from side_lane import hosts

        args = mock.Mock(
            host="codex",
            mode="execute",
            provider="openai",
            model="gpt-5.6-terra",
            capability=[],
            lane_name="worker",
            approve_billable_route=False,
            worktree_root=None,
        )
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".git").mkdir()
            with (
                mock.patch("side_lane.cli.shutil.which", return_value=None),
                mock.patch.object(hosts, "BUNDLED_CODEX_CANDIDATES", ()),
                mock.patch("side_lane.cli.create_worktree") as create,
            ):
                with self.assertRaisesRegex(
                    cli.SideLaneError, "codex executable not found"
                ):
                    cli._launch(args, cli.load_config(), repo, "task")
                create.assert_not_called()

    def test_capability_report_and_auth_status_use_resolved_bundle_executable(
        self,
    ) -> None:
        from side_lane import hosts

        config = cli.load_config()
        ready = mock.Mock(
            ready=True, as_dict=lambda: {"state": "ready", "method": "oauth"}
        )
        with tempfile.TemporaryDirectory() as directory:
            bundled = Path(directory) / "codex"
            bundled.write_text("#!/bin/sh\n", encoding="utf-8")
            bundled.chmod(0o755)
            with (
                mock.patch("side_lane.cli.shutil.which", return_value=None),
                mock.patch.object(hosts, "BUNDLED_CODEX_CANDIDATES", (str(bundled),)),
                mock.patch("side_lane.cli.auth_status", return_value=ready) as status,
                mock.patch(
                    "side_lane.cli._discover_mcp_inventory", return_value=(set(), set())
                ),
            ):
                report = cli._capability_report(
                    config, "codex", "execute", "openai", "gpt-5.6-terra"
                )
        self.assertEqual(report["runtime"], str(bundled))
        self.assertEqual(report["capability_evidence"]["shell"]["state"], "verified")
        status.assert_called_with("codex", executable=str(bundled))
