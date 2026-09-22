from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from side_lane import cli, report_stop_hook
from side_lane.results import LaneResult
from side_lane.worktrees import (
    UNTRACKED_STATUS as UNTRACKED,
    ChangedPath,
    LaneDelivery,
    VerifyResult,
    WorktreeError,
)

# Hermetic suite: an inherited SIDE_LANE_MODELS_PATH or
# SIDE_LANE_ROUTING_CATALOG_PATH would silently re-point the config and
# catalog defaults these tests exercise, so both are removed at import.
# Tests that want an override set it explicitly (mock.patch.dict).
for _var in ("SIDE_LANE_MODELS_PATH", "SIDE_LANE_ROUTING_CATALOG_PATH"):
    os.environ.pop(_var, None)


def _only_summary(stdout: str) -> dict:
    """Pull the summary object out of _launch's stdout.

    _launch prints progress lines around the summary, and how many is not part
    of any contract — slicing a fixed number of leading lines off made these
    tests depend on that incidental count. Decode from the first brace instead.
    """

    start = stdout.index("{")
    return json.JSONDecoder().raw_decode(stdout[start:])[0]


#: The execute adapter entry point each host's `run` branch calls. A lane is
#: host-neutral when the same verdict is reached through whichever one it uses.
HOST_LAUNCH_TARGETS = {
    "claude": "side_lane.adapters.claude.launch",
    "codex": "side_lane.adapters.codex.run_codex",
    "devin": "side_lane.adapters.devin.launch",
}

#: The one accept/refuse table for the browser-report artifact namespace, shared
#: with the cloud worker's collector: the runner's half asserts it against
#: `cli.BROWSER_REPORT_ARTIFACT_RE` and the `BROWSER_ARTIFACT_MAX_*` caps, and
#: the cloud counterpart asserts the same file against `pipeline`'s. A one-sided
#: change to the namespace or its caps fails on the other side.
REPORT_ARTIFACT_NAMES = (
    Path(__file__).resolve().parent / "fixtures" / "report_artifact_names.json"
)


def _report_artifact_name_table() -> dict:
    return json.loads(REPORT_ARTIFACT_NAMES.read_text(encoding="utf-8"))


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
        worktree = repo / "devin-worktree"
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
            lane_name="devin-1", skill=[],
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
            lane_name="review", skill=[],
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
            lane_name="metered", skill=[],
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
        worktree = repo / "metered-devin-worktree"
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
            lane_name="metered", skill=[],
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
            lane_name="worker", skill=[],
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
            lane_name="review", skill=[],
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
            lane_name="review", skill=[],
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
            lane_name="review", skill=[],
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
        worker_returncode=0,
    ):
        """Drive _launch through a finished execute lane; report what happened.

        The push and the verification command are both mocked: these tests
        pin the CLI contract (when a push or a verification is attempted,
        what the summary claims, what the exit code is), while
        test_worktrees.py covers the real invocations.
        """
        repo = self.repo()
        # Inside repo's tempdir (not a fixed sibling of it): execute lanes now
        # materialize the skill bundle into this path for real, so a shared
        # path would leak deliveries across tests — and into the OS temp root.
        worktree = repo / "execute-worktree"
        lane = mock.Mock(worktree=worktree, branch="side-lane/task-1")
        result = LaneResult(
            ("claude",),
            worker_returncode,
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
            lane_name="task", skill=[],
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

    def test_an_execute_lane_verifies_even_when_the_worker_exited_nonzero(self) -> None:
        """Pinned on purpose: the report-only gate does not reach execute lanes.

        An execute lane's verification is still gated on `delivery.delivered`
        alone, exactly as it always was. Adding this run's other outcomes to
        that gate would change the ordinary execute contract and its summary —
        a separate decision, not a side effect of the report-only repair.
        """
        ok = VerifyResult(command="make test", exit_code=0, output="ok")
        code, summary, _errors, _publish, _lane, verify = self._delivered_lane(
            LaneDelivery(committed=True, uncommitted=()),
            verify_command="make test", verify_result=ok, worker_returncode=3,
        )
        self.assertEqual(code, 3)
        verify.assert_called_once()
        self.assertTrue(summary["verified"])
        self.assertTrue(summary["delivered"])

    def test_verify_is_rejected_outside_execute_mode(self) -> None:
        # A review lane disposes its worktree before verification could run;
        # accepting the flag there would be a silent no-op.
        args = mock.Mock(
            host="claude",
            mode="review",
            provider="claude",
            model="claude-sonnet-5",
            capability=[],
            lane_name="review", skill=[],
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
        for model in ("claude-opus-5", "claude-fable-5-1", "claude-sonnet-5", "claude-haiku-4-5-20251001"):
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

    def test_slack_read_evidence_separates_registration_from_authentication(self) -> None:
        # Registration presence is metadata evidence; it never claims Slack
        # authentication or a live read. Exact-name mismatch fails closed the
        # way the graph capability checks do.
        for names, expected_state in (
            ({"slack"}, "present"),
            (set(), "unknown"),
            ({"slack-readonly", "zoom"}, "name-mismatch"),
        ):
            with self.subTest(names=sorted(names), expected_state=expected_state):
                with (
                    mock.patch(
                        "side_lane.cli._host_executable", return_value="/opt/claude"
                    ),
                    mock.patch(
                        "side_lane.cli._discover_mcp_inventory",
                        return_value=(names, set()),
                    ),
                ):
                    report = cli._capability_report(
                        {"providers": {}, "capabilities": ["slack-read"]},
                        "claude",
                        "execute",
                        None,
                        None,
                    )
                evidence = report["capability_evidence"]["slack-read"]
                self.assertEqual(evidence["state"], expected_state)
                if expected_state == "present":
                    self.assertIn(
                        "authentication and a live read are not tested",
                        evidence["basis"],
                    )
                if expected_state == "name-mismatch":
                    self.assertIn("register the server as 'slack'", evidence["basis"])
                # Registration of any kind is not a verified capability.
                self.assertFalse(report["capabilities"]["slack-read"])

    def test_slack_read_requires_exact_registration_on_codex(self) -> None:
        # The slack-read grants embed the server name 'slack' exactly on every
        # host, so a Codex near-miss registration is name-mismatch and fails
        # the launch gate; an exact registration is present without auth proof.
        for names, expected_state in (
            ({"slack-mcp"}, "name-mismatch"),
            ({"slack"}, "present"),
        ):
            with self.subTest(names=sorted(names), expected_state=expected_state):
                with (
                    mock.patch(
                        "side_lane.cli._host_executable", return_value="/opt/codex"
                    ),
                    mock.patch(
                        "side_lane.cli._discover_mcp_inventory",
                        return_value=(names, set()),
                    ),
                ):
                    report = cli._capability_report(
                        {"providers": {}, "capabilities": ["slack-read"]},
                        "codex",
                        "execute",
                        None,
                        None,
                    )
                evidence = report["capability_evidence"]["slack-read"]
                self.assertEqual(evidence["state"], expected_state)
                if expected_state == "name-mismatch":
                    self.assertIn("register the server as 'slack'", evidence["basis"])
                else:
                    self.assertIn(
                        "authentication and a live read are not tested",
                        evidence["basis"],
                    )
                self.assertNotRegex(
                    evidence["basis"],
                    r"authenticat(ed|ion) (read|succeeded|is verified)",
                )
                self.assertFalse(report["capabilities"]["slack-read"])

    def test_cm_services_capabilities_require_the_exact_fixed_server(self) -> None:
        # asana-read/drive-read/algolia-read grants embed the server name 'cm-services'
        # exactly, so registration evidence demands the exact name on every
        # host; presence is registration metadata only — same-account
        # provisioning, authentication, and a live read stay untested.
        for names, expected_state in (
            ({"cm-services"}, "present"),
            (set(), "unknown"),
            ({"cm-services-legacy"}, "name-mismatch"),
        ):
            for host in ("claude", "devin"):
                with self.subTest(names=sorted(names), host=host,
                                  expected_state=expected_state):
                    with (
                        mock.patch(
                            "side_lane.cli._host_executable", return_value=f"/opt/{host}"
                        ),
                        mock.patch(
                            "side_lane.cli._discover_mcp_inventory",
                            return_value=(names, set()),
                        ),
                    ):
                        report = cli._capability_report(
                            {"providers": {},
                             "capabilities": ["asana-read", "drive-read", "gcloud-read", "database-read", "algolia-read"]},
                            host,
                            "execute",
                            None,
                            None,
                        )
                    for capability in ("asana-read", "drive-read", "gcloud-read", "database-read", "algolia-read"):
                        evidence = report["capability_evidence"][capability]
                        self.assertEqual(evidence["state"], expected_state)
                        if expected_state == "present":
                            self.assertIn("same-account provisioning", evidence["basis"])
                            self.assertIn("not tested", evidence["basis"])
                        if expected_state == "name-mismatch":
                            self.assertIn(
                                "register the server as 'cm-services'", evidence["basis"]
                            )
                        # Registration of any kind is not a verified capability.
                        self.assertFalse(report["capabilities"][capability])

    def test_graph_connector_codex_substring_behavior_preserved(self) -> None:
        # The slack exact-name tightening must not change the graph
        # capabilities' Codex behavior: connector-name presence remains the
        # evidence there, so a near-miss is still present, not name-mismatch.
        with (
            mock.patch("side_lane.cli._host_executable", return_value="/opt/codex"),
            mock.patch(
                "side_lane.cli._discover_mcp_inventory",
                return_value=({"gitnexus-local"}, set()),
            ),
        ):
            report = cli._capability_report(
                {"providers": {}, "capabilities": ["gitnexus"]},
                "codex",
                "execute",
                None,
                None,
            )
        evidence = report["capability_evidence"]["gitnexus"]
        self.assertEqual(evidence["state"], "present")
        self.assertIn("metadata only", evidence["basis"])

    def test_codex_retains_native_service_cli_presence_without_bridge(self) -> None:
        with (
            mock.patch("side_lane.cli._host_executable", return_value="/opt/codex"),
            mock.patch("side_lane.cli._discover_mcp_inventory", return_value=(set(), set())),
            mock.patch("side_lane.cli.shutil.which", side_effect=lambda name: "/bin/" + name),
        ):
            report = cli._capability_report(
                {"providers": {}, "capabilities": ["gcloud-read", "database-read"]},
                "codex", "execute", None, None,
            )
        self.assertEqual(report["capability_evidence"]["gcloud-read"]["state"], "present")
        self.assertIn("native gcloud executable", report["capability_evidence"]["gcloud-read"]["basis"])
        self.assertEqual(report["capability_evidence"]["database-read"]["state"], "present")
        self.assertIn("native psql executable", report["capability_evidence"]["database-read"]["basis"])

    def test_devin_registration_sources_distinguish_absent_from_unrecognised(
        self,
    ) -> None:
        # An empty `mcp_connectors` has two different causes, and only the file
        # dispositions can tell them apart: nothing registered, or a file this
        # scanner did not recognise. Reporting one as the other is how a
        # registration gap and a parser gap get conflated.
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            repo = Path(directory) / "repo"
            (home / ".config" / "devin").mkdir(parents=True)
            (repo / ".devin").mkdir(parents=True)
            (home / ".config" / "devin" / "mcp_config.json").write_text(
                '{"mcpServers":{"gitnexus":{"command":"native"}}}', encoding="utf-8"
            )
            # Present, parses, but declares no container this scanner reads.
            (repo / ".devin" / "mcp_config.json").write_text(
                '{"servers":{"codegraph":{"command":"other-shape"}}}', encoding="utf-8"
            )
            with mock.patch("side_lane.cli.Path.home", return_value=home):
                sources = cli._mcp_registration_sources("devin", repo)
        self.assertEqual(
            sources,
            [
                {
                    "scope": "user",
                    "path": str(home / ".config" / "devin" / "mcp_config.json"),
                    "state": "registered",
                },
                {
                    "scope": "project",
                    "path": str(repo / ".devin" / "mcp_config.json"),
                    "state": "no-servers",
                },
                {
                    "scope": "local",
                    "path": str(repo / ".devin" / "mcp_config.local.json"),
                    "state": "missing",
                },
            ],
        )

    def test_registration_sources_report_unparsed_and_out_of_scope_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            repo = Path(directory) / "repo"
            (home / ".config" / "devin").mkdir(parents=True)
            (repo / ".devin").mkdir(parents=True)
            (home / ".config" / "devin" / "mcp_config.json").write_text(
                '{"mcpServers": {', encoding="utf-8"
            )
            (repo / ".devin" / "mcp_config.json").write_text(
                json.dumps({"projects": {"/elsewhere": {"mcpServers": {"zoom": {}}}}}),
                encoding="utf-8",
            )
            with mock.patch("side_lane.cli.Path.home", return_value=home):
                sources = cli._mcp_registration_sources("devin", repo)
        self.assertEqual([source["state"] for source in sources],
                         ["unparsed", "out-of-scope-only", "missing"])
        # Only paths ever leave the registration files, never their values.
        self.assertNotIn("/elsewhere", json.dumps(sources))

    def test_capability_report_carries_registration_sources_and_never_verifies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            repo = Path(directory) / "repo"
            home.mkdir()
            repo.mkdir()
            config = {"providers": {}, "capabilities": ["gitnexus", "codegraph"]}
            with (
                mock.patch("side_lane.cli.Path.home", return_value=home),
                mock.patch("side_lane.cli._host_executable", return_value="/opt/devin"),
                mock.patch(
                    "side_lane.cli._discover_mcp_inventory",
                    return_value=({"gitnexus"}, set()),
                ),
            ):
                report = cli._capability_report(config, "devin", "execute", None, None, repo)
        self.assertIn("mcp_registration_sources", report)
        # A registered connector is presence evidence, never a verified
        # capability: no live call and no fresh graph has been demonstrated.
        self.assertEqual(
            report["capability_evidence"]["gitnexus"]["state"], "present"
        )
        self.assertFalse(report["capabilities"]["gitnexus"])
        self.assertFalse(report["capabilities"]["codegraph"])
        self.assertIn(
            "mcp_registration_sources", report["capability_evidence"]["codegraph"]["basis"]
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
        # Same reasoning as _delivered_lane: a per-test worktree, because
        # execute lanes materialize the skill bundle here for real.
        worktree = repo / "execute-worktree"
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
            lane_name="task", skill=[],
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
        worktree = repo / "codex-worktree"
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
            lane_name="task", skill=[],
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
            lane_name="task", skill=[],
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
                "slack-read": {"state": "name-mismatch"},
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
            # `load_config()` may follow an ambient SIDE_LANE_MODELS_PATH that
            # lags the package config, so ensure the capability is known here
            # regardless; the gate under test is the evidence state, not the
            # allowlist lookup.
            config = dict(cli.load_config())
            config["capabilities"] = sorted(
                set(config["capabilities"]) | {"slack-read"}
            )
            with self.assertRaisesRegex(cli.SideLaneError, "unavailable: slack-read"):
                cli._launch(
                    self._args(["slack-read"]),
                    config,
                    self.repo(),
                    "Implement",
                )

    def test_cm_services_cloud_capabilities_reach_the_real_launch_gate(self) -> None:
        """The cloud bridge must satisfy the launch gate without local CLIs."""
        config = cli.load_config()
        repo = self.repo()
        lane = mock.Mock(worktree=repo / "lane", branch="side-lane/cm-services")
        result = LaneResult(
            ("claude",), 0, lane.worktree, "claude", "claude", "native-claude",
            "claude-sonnet-5", "oauth", False, "", "",
            requested_model="claude-sonnet-5", resolved_model="claude-sonnet-5",
        )
        args = mock.Mock(
            host="claude", mode="execute", provider="claude", model="claude-sonnet-5",
            capability=["gcloud-read", "database-read"], lane_name="cm-services",
            skill=[], approve_billable_route=False, worktree_root=None, verify=None,
            read_root=[], mcp_config=None,
        )
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            (home_path / ".claude.json").write_text(
                json.dumps({"mcpServers": {"cm-services": {"command": "cm-services"}}}),
                encoding="utf-8",
            )
            # Neither executable exists in this synthetic cloud image; the
            # actual capability report must use the fixed MCP registration.
            with (
                mock.patch.dict(os.environ, {"HOME": home}, clear=False),
                mock.patch("side_lane.cli.shutil.which", side_effect=lambda name: None if name in {"gcloud", "psql"} else "/bin/tool"),
                mock.patch("side_lane.cli._require_host_executable", return_value="/opt/claude"),
                mock.patch("side_lane.cli.create_worktree", return_value=lane),
                mock.patch("side_lane.cli.lane_delivery", return_value=LaneDelivery(committed=True, uncommitted=())),
                mock.patch("side_lane.cli.require_native_oauth"),
                mock.patch("side_lane.adapters.claude.launch", return_value=result),
                mock.patch("side_lane.cli.git_status", return_value="## lane"),
                mock.patch("side_lane.cli.write_audit", return_value=repo / ".git/audit.json"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli._launch(args, config, repo, "Use the service bridge"), 0)

    def test_algolia_read_launch_gate_with_cm_services_evidence(self) -> None:
        """algolia-read is admitted only when cm-services is registered exactly;
        an unregistered bridge fails the launch gate before a worktree is made.
        """
        config = cli.load_config()
        repo = self.repo()
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            (home_path / ".claude.json").write_text(
                json.dumps({"mcpServers": {"cm-services": {"command": "cm-services"}}}),
                encoding="utf-8",
            )
            args = mock.Mock(
                host="claude", mode="execute", provider="claude", model="claude-sonnet-5",
                capability=["algolia-read"], lane_name="algolia", skill=[],
                approve_billable_route=False, worktree_root=None, verify=None,
                read_root=[], mcp_config=None,
            )
            with (
                mock.patch.dict(os.environ, {"HOME": home}, clear=False),
                mock.patch("side_lane.cli.shutil.which", side_effect=lambda name: None if name in {"gcloud", "psql"} else "/bin/tool"),
                mock.patch("side_lane.cli._require_host_executable", side_effect=cli.SideLaneError("stop here")),
                mock.patch("side_lane.cli.create_worktree") as create,
            ):
                with self.assertRaisesRegex(cli.SideLaneError, "stop here"):
                    cli._launch(args, config, repo, "Use algolia")
                create.assert_not_called()
                # Remove the exact server: the gate must reject algolia-read.
                (home_path / ".claude.json").write_text(
                    json.dumps({"mcpServers": {}}), encoding="utf-8"
                )
                args = mock.Mock(
                    host="claude", mode="execute", provider="claude", model="claude-sonnet-5",
                    capability=["algolia-read"], lane_name="algolia", skill=[],
                    approve_billable_route=False, worktree_root=None, verify=None,
                    read_root=[], mcp_config=None,
                )
                with self.assertRaisesRegex(cli.SideLaneError, "unavailable: algolia-read"):
                    cli._launch(args, config, repo, "Use algolia")
                create.assert_not_called()

    def test_gateway_read_launch_gate_with_cm_services_evidence(self) -> None:
        """gateway-read is admitted only on the exact cm-services registration.

        The registration is presence evidence only: the gate never reports a
        live read, and an absent or near-miss server fails it before any
        worktree is created.
        """
        config = cli.load_config()
        repo = self.repo()
        for inventory in (
            {"cm-services": {"command": "cm-services"}},
            {"cm-services-local": {"command": "cm-services"}},
            {},
        ):
            with self.subTest(inventory=inventory):
                with tempfile.TemporaryDirectory() as home:
                    home_path = Path(home)
                    (home_path / ".claude.json").write_text(
                        json.dumps({"mcpServers": inventory}), encoding="utf-8"
                    )
                    args = mock.Mock(
                        host="claude", mode="execute", provider="claude", model="claude-sonnet-5",
                        capability=["gateway-read"], lane_name="gateway", skill=[],
                        approve_billable_route=False, worktree_root=None, verify=None,
                        read_root=[], mcp_config=None,
                    )
                    with (
                        mock.patch.dict(os.environ, {"HOME": home}, clear=False),
                        mock.patch("side_lane.cli.shutil.which", side_effect=lambda name: None if name in {"gcloud", "psql"} else "/bin/tool"),
                        mock.patch("side_lane.cli._require_host_executable", side_effect=cli.SideLaneError("stop here")),
                        mock.patch("side_lane.cli.create_worktree") as create,
                    ):
                        if "cm-services" in inventory:
                            # Exact registration: the gate passes and launch
                            # stops only at the next guard.
                            with self.assertRaisesRegex(cli.SideLaneError, "stop here"):
                                cli._launch(args, config, repo, "Read the run status")
                        else:
                            with self.assertRaisesRegex(
                                cli.SideLaneError, "unavailable: gateway-read"
                            ):
                                cli._launch(args, config, repo, "Read the run status")
                        create.assert_not_called()


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
            lane_name="worker", skill=[],
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


class ReportOnlyCliTests(unittest.TestCase):
    """`side-lane run --report-only`: CLI gating, plumbing, and final acceptance.

    The flag is the CLI half of the same-invocation Stop-hook repair: it is
    opt-in, execute-only, Claude-only, and requires the finite USD cap that
    makes the cap and the hook the same command. The runner's last word is its
    own look at the report — a lane that never produced one is not accepted,
    whatever the completion prose said.
    """

    REPORT_NAME = "SIDE_LANE_REPORT.md"

    def setUp(self) -> None:
        # Keep host-executable resolution hermetic, as the shared base does.
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
            "You must read [CLAUDE.md](./CLAUDE.md); it is the authoritative "
            "source of truth.\n",
            encoding="utf-8",
        )
        return path

    def _args(self, **overrides):
        values = dict(
            host="claude", mode="execute", provider="claude",
            model="claude-sonnet-5", capability=[], lane_name="task", skill=[],
            approve_billable_route=False, worktree_root=None,
            allow_no_commit=False, no_publish=True, verify=None,
            read_root=[], mcp_config=None, report_only=True,
            report_deliverable=False,
        )
        values.update(overrides)
        return mock.Mock(**values)

    @contextlib.contextmanager
    def _patched(self, repo, lane, launch, *, model_config=None, create=None,
                 delivery=None, delivery_error=None, capability_evidence=None,
                 launch_attr="side_lane.adapters.claude.launch",
                 host_support=None):
        """The hermetic CLI environment: no host executable, git, or network.

        ``create`` replaces the worktree factory when a test wants the real one
        (the default is a mock returning ``lane``). ``delivery`` is the mocked
        lane tree verdict — the default is a lane that wrote its report and
        committed nothing, which is what a report-only worker is told to do —
        and ``delivery_error`` makes the inspection itself fail.
        ``capability_evidence`` replaces the host-inventory scan, which is the
        only part of a ``--capability`` lane that needs a real host; a test that
        grants one states the evidence it wants instead. ``launch_attr`` is the
        adapter entry point the host's `run` branch calls, and ``host_support``
        the support directory the Codex branch resolves. Yields the
        ``publish_lane_branch`` and ``dispose_clean_worktree`` mocks.
        """

        if delivery_error is not None:
            delivery_patch = {"side_effect": delivery_error}
        else:
            delivery_patch = {
                "return_value": (
                    LaneDelivery(committed=False, uncommitted=(self.REPORT_NAME,))
                    if delivery is None
                    else delivery
                )
            }
        claude_route = (
            {"gateway": "native-claude", "auth_method": "oauth", "billable": False},
            model_config if model_config is not None
            else {"runtime_model": "claude-sonnet-5", "protocol": "native-claude",
                  "max_budget_usd": 2.5},
        )
        capability_patch = (
            contextlib.nullcontext() if capability_evidence is None
            else mock.patch(
                "side_lane.cli._capability_report",
                return_value={"capability_evidence": capability_evidence},
            )
        )
        host_support_patch = (
            contextlib.nullcontext() if host_support is None
            else mock.patch("side_lane.cli.host_support_dir",
                            return_value=host_support)
        )
        with (
            mock.patch("side_lane.cli.create_worktree",
                       **({"side_effect": create} if create is not None
                          else {"return_value": lane})),
            mock.patch("side_lane.cli._require_host_executable",
                       return_value="/opt/hosts/claude"),
            mock.patch("side_lane.cli.select_route", return_value=claude_route),
            mock.patch("side_lane.cli.require_native_oauth"),
            mock.patch(launch_attr, launch),
            host_support_patch,
            mock.patch("side_lane.cli.lane_delivery", **delivery_patch),
            mock.patch("side_lane.cli.git_status", return_value="## task"),
            mock.patch("side_lane.cli.write_audit",
                       return_value=repo / ".git" / "audit.json"),
            mock.patch("side_lane.cli.dispose_clean_worktree") as dispose,
            mock.patch("side_lane.cli.publish_lane_branch",
                       return_value="origin/side-lane/task-1") as publish,
            capability_patch,
        ):
            yield publish, dispose

    def _drive(self, *, report_only=True, report=None, model_config=None,
               host="claude", mode="execute", no_publish=True,
               inherited_report=None, worker_writes=(), delivery=None,
               delivery_error=None, returncode=0, allow_no_commit=False,
               report_deliverable=False, capability=(), capability_evidence=None):
        """Run _launch through a mocked Claude adapter; return what happened.

        ``inherited_report`` is written into the lane before the runner is
        called, the way a lane added from a HEAD that tracks
        ``SIDE_LANE_REPORT.md`` arrives with a report no worker wrote.
        ``worker_writes`` are other files the fake worker creates, and
        ``report`` is the report the fake worker writes — the way a worker
        would — so the real post-run gate is what is under test.
        ``delivery`` overrides the mocked lane tree verdict (the default is the
        lane a report-only worker is told to leave: its report and no commit)
        and ``delivery_error`` makes that inspection fail instead.
        """

        repo = self.repo()
        worktree = repo / "execute-worktree"
        if inherited_report is not None:
            worktree.mkdir(parents=True, exist_ok=True)
            (worktree / self.REPORT_NAME).write_text(
                inherited_report, encoding="utf-8")
        lane = mock.Mock(worktree=worktree, branch="side-lane/task-1")
        if delivery is None and delivery_error is None:
            delivery = (
                LaneDelivery(committed=False, uncommitted=(self.REPORT_NAME,))
                if report_only or report_deliverable
                else LaneDelivery(committed=True, uncommitted=())
            )
        result = LaneResult(
            (host,), returncode, worktree, host, "claude", "native-claude",
            "claude-sonnet-5", "oauth", False, "done", "",
        )

        def fake_launch(*_args, **_kwargs):
            worktree.mkdir(parents=True, exist_ok=True)
            for name, content in worker_writes:
                path = worktree / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            if report is not None:
                (worktree / self.REPORT_NAME).write_text(report, encoding="utf-8")
            return result

        launch = mock.MagicMock(side_effect=fake_launch)
        with (
            self._patched(repo, lane, launch, model_config=model_config,
                          delivery=delivery, delivery_error=delivery_error,
                          capability_evidence=capability_evidence,
                          launch_attr=HOST_LAUNCH_TARGETS[host],
                          host_support=None if host == "claude" else "/opt/support") as (
                publish, _dispose),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            code = cli._launch(
                self._args(report_only=report_only, host=host, mode=mode,
                           no_publish=no_publish, allow_no_commit=allow_no_commit,
                           report_deliverable=report_deliverable,
                           capability=list(capability)),
                cli.load_config(), repo, "Implement",
            )
        self.publish_lane = publish
        return code, output.getvalue(), errors.getvalue(), launch, worktree

    # --- the flag exists only on the one supported route ---------------------

    def test_parser_accepts_the_flag(self) -> None:
        args = cli.make_parser().parse_args([
            "run", "--host", "claude", "--mode", "execute", "--provider", "claude",
            "--model", "claude-sonnet-5", "--repo", ".", "--lane-name", "task",
            "--prompt", "Implement", "--report-only",
        ])
        self.assertTrue(args.report_only)

    def test_review_mode_is_rejected_before_anything_is_created(self) -> None:
        repo = self.repo()
        with (
            mock.patch("side_lane.cli.create_worktree") as create,
            mock.patch("side_lane.adapters.claude.launch") as launch,
        ):
            with self.assertRaises(cli.SideLaneError) as caught:
                cli._launch(self._args(mode="review"), cli.load_config(), repo, "Review")
        self.assertIn("execute", str(caught.exception))
        create.assert_not_called()
        launch.assert_not_called()

    def test_other_hosts_are_rejected_before_anything_is_created(self) -> None:
        for host in ("codex", "devin"):
            with self.subTest(host=host):
                repo = self.repo()
                with mock.patch("side_lane.cli.create_worktree") as create:
                    with self.assertRaises(cli.SideLaneError) as caught:
                        cli._launch(self._args(host=host), cli.load_config(), repo,
                                    "Implement")
                self.assertIn("claude", str(caught.exception))
                create.assert_not_called()

    def test_missing_or_invalid_budget_fails_before_any_worktree(self) -> None:
        base = {"runtime_model": "claude-sonnet-5", "protocol": "native-claude"}
        for label, extra in (
            ("missing", {}),
            ("zero", {"max_budget_usd": 0}),
            ("infinite", {"max_budget_usd": float("inf")}),
            ("text", {"max_budget_usd": "much"}),
        ):
            with self.subTest(label=label):
                repo = self.repo()
                model_config = dict(base, **extra)
                with (
                    mock.patch("side_lane.cli._require_host_executable",
                               return_value="/opt/hosts/claude"),
                    mock.patch("side_lane.cli.select_route", return_value=(
                        {"gateway": "native-claude", "auth_method": "oauth",
                         "billable": False}, model_config)),
                    mock.patch("side_lane.cli.create_worktree") as create,
                    mock.patch("side_lane.adapters.claude.launch") as launch,
                ):
                    with self.assertRaises(cli.SideLaneError):
                        cli._launch(self._args(), cli.load_config(), repo, "Implement")
                create.assert_not_called()
                launch.assert_not_called()

    # --- the flag reaches only the Claude execute adapter --------------------

    def test_opt_in_is_forwarded_to_the_claude_adapter(self) -> None:
        code, _stdout, _stderr, launch, _worktree = self._drive(report="# Findings\n- item\n")
        self.assertEqual(code, 0)
        self.assertIs(launch.call_args.kwargs["report_only"], True)

    def test_default_execute_run_does_not_forward_the_opt_in(self) -> None:
        code, _stdout, _stderr, launch, _worktree = self._drive(
            report_only=False, report="# Findings\n")
        self.assertEqual(code, 0)
        self.assertIs(launch.call_args.kwargs["report_only"], False)

    # --- the runner's own look at the report is the last word ----------------

    def test_missing_report_after_one_feedback_is_not_accepted(self) -> None:
        code, stdout, stderr, _launch, worktree = self._drive(report=None)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        self.assertEqual(_only_summary(stdout).get("report_present"), False)
        self.assertIn(self.REPORT_NAME, stderr)
        self.assertIn(str(worktree), stderr)

    def test_missing_report_blocks_publication_and_delivery_summary(self) -> None:
        code, stdout, _stderr, _launch, _worktree = self._drive(
            report=None, no_publish=False)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        self.publish_lane.assert_not_called()
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertNotIn("published", summary)

    def test_invalid_report_blocks_publication_and_delivery_summary(self) -> None:
        code, stdout, _stderr, _launch, _worktree = self._drive(
            report=" \n\t ", no_publish=False)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        self.publish_lane.assert_not_called()
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertNotIn("published", summary)

    def test_blank_report_is_not_accepted(self) -> None:
        for label, report in (("empty", ""), ("whitespace", "   \n\t ")):
            with self.subTest(label=label):
                code, stdout, _stderr, _launch, _worktree = self._drive(report=report)
                self.assertEqual(code, cli.LANE_NOT_DELIVERED)
                self.assertEqual(
                    _only_summary(stdout).get("report_present"), False)

    def test_present_report_is_accepted(self) -> None:
        code, stdout, _stderr, _launch, worktree = self._drive(report="# Findings\n- item\n")
        self.assertEqual(code, 0)
        summary = _only_summary(stdout)
        self.assertEqual(summary.get("report_present"), True)
        self.assertEqual(summary.get("report_path"),
                         str(worktree / self.REPORT_NAME))

    def test_report_gate_is_absent_without_the_opt_in(self) -> None:
        code, stdout, _stderr, _launch, _worktree = self._drive(
            report_only=False, report=None)
        self.assertEqual(code, 0)
        self.assertNotIn("report_present", _only_summary(stdout))

    # --- the report must be this run's, not the lane's inheritance -----------
    #
    # A repository that tracks SIDE_LANE_REPORT.md hands every lane added from
    # HEAD a complete-looking report no worker wrote, and the pre-repair gate
    # (nonempty regular file at the fixed path) accepted it. An unrelated
    # commit or an empty session was then enough to call a lane delivered.

    INHERITED = "# Findings from a previous task\n\n- plausible-looking claim\n"

    def test_inherited_report_with_an_unrelated_change_is_not_delivered(self) -> None:
        code, stdout, stderr, _launch, _worktree = self._drive(
            inherited_report=self.INHERITED,
            worker_writes=(("src/change.py", "print('unrelated')\n"),),
            report=None, no_publish=False,
        )
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["report_present"], False)
        self.assertEqual(summary["report_state"], "stale")
        self.assertEqual(summary["report_preexisting"], True)
        self.assertNotIn("published", summary)
        self.publish_lane.assert_not_called()
        self.assertIn("unchanged", stderr)

    def test_inherited_report_restored_by_the_worker_is_still_stale(self) -> None:
        # `git checkout -- .` puts the inherited bytes back; a baseline that
        # only quarantined the file would let that restore re-open the hole.
        code, stdout, _stderr, _launch, _worktree = self._drive(
            inherited_report=self.INHERITED, report=self.INHERITED)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        self.assertEqual(_only_summary(stdout).get("report_state"), "stale")

    def test_worker_written_report_is_accepted_over_the_inherited_one(self) -> None:
        code, stdout, _stderr, _launch, worktree = self._drive(
            inherited_report=self.INHERITED,
            report="# Findings\n\n- what this run actually measured\n")
        self.assertEqual(code, 0)
        summary = _only_summary(stdout)
        self.assertEqual(summary["report_present"], True)
        self.assertEqual(summary["report_state"], "current")
        # It is the lane's report that counts, not the preserved copy.
        self.assertEqual(summary["report_path"], str(worktree / self.REPORT_NAME))

    def test_inherited_report_is_preserved_in_lane_scratch_and_left_in_place(self) -> None:
        code, stdout, _stderr, _launch, worktree = self._drive(
            inherited_report=self.INHERITED, report=None)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        preserved = (worktree / ".side-lane-scratch" / "report-inherited"
                     / self.REPORT_NAME)
        self.assertEqual(_only_summary(stdout).get("report_preserved"),
                         str(preserved))
        # The historical file is copied, never moved, modified, or deleted.
        self.assertEqual(preserved.read_text(encoding="utf-8"), self.INHERITED)
        self.assertEqual((worktree / self.REPORT_NAME).read_text(encoding="utf-8"),
                         self.INHERITED)

    def test_preserved_history_is_never_silently_overwritten(self) -> None:
        # Two runs over one lane keep the earliest inherited bytes.
        worktree = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, worktree, ignore_errors=True)
        report = worktree / self.REPORT_NAME
        report.write_text(self.INHERITED, encoding="utf-8")
        report_stop_hook.capture_report_baseline(report)
        report.write_text("# rewritten by a later run\n", encoding="utf-8")
        second = report_stop_hook.capture_report_baseline(report)
        self.assertEqual(second.preserved_path.read_text(encoding="utf-8"),
                         self.INHERITED)

    def test_real_lane_inheriting_a_tracked_report_is_not_delivered(self) -> None:
        """End-to-end on a real git worktree: the defect this repair closes.

        The repository tracks SIDE_LANE_REPORT.md at HEAD, so the lane created
        for this run starts with a complete-looking report no worker wrote. An
        unrelated commit in the lane must not make that an accepted delivery.
        """
        repo = self.repo()
        (repo / self.REPORT_NAME).write_text(self.INHERITED, encoding="utf-8")
        git = ["git", "-c", "user.email=dev@example.com", "-c", "user.name=Dev"]
        # Lane creation requires a clean coordinator checkout; the report is
        # tracked at HEAD, which is exactly how a lane comes to inherit one.
        subprocess.run(git + ["-C", str(repo), "add", "-A"],
                       check=True, capture_output=True)
        subprocess.run(git + ["-C", str(repo), "commit", "-m", "prior task report"],
                       check=True, capture_output=True)

        created: list = []
        real_create = cli.create_worktree

        def capture(*args, **kwargs):
            lane = real_create(*args, **kwargs)
            created.append(lane)
            return lane

        def fake_launch(*_args, **_kwargs):
            worktree = created[0].worktree
            (worktree / "unrelated.py").write_text("print('x')\n", encoding="utf-8")
            subprocess.run(git + ["-C", str(worktree), "add", "unrelated.py"],
                           check=True, capture_output=True)
            subprocess.run(git + ["-C", str(worktree), "commit", "-m", "unrelated work"],
                           check=True, capture_output=True)
            return LaneResult(
                ("claude",), 0, worktree, "claude", "claude", "native-claude",
                "claude-sonnet-5", "oauth", False, "done", "",
            )

        launch = mock.MagicMock(side_effect=fake_launch)
        with (
            self._patched(repo, None, launch, create=capture) as (_publish, _dispose),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            code = cli._launch(self._args(), cli.load_config(), repo, "Implement")

        # The lane really did inherit the tracked report.
        lane = created[0]
        self.assertEqual((lane.worktree / self.REPORT_NAME).read_text(encoding="utf-8"),
                         self.INHERITED)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED, errors.getvalue())
        summary = _only_summary(output.getvalue())
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["report_state"], "stale")
        # ...and its history survived the run in the lane's ignored scratch.
        preserved = Path(summary["report_preserved"])
        self.assertEqual(preserved.read_text(encoding="utf-8"), self.INHERITED)
        self.assertEqual(preserved.parent.parent.parent, lane.worktree)

    def test_unsafe_preexisting_report_path_fails_closed_before_the_worker(self) -> None:
        for label in ("symlink", "directory"):
            with self.subTest(label=label):
                repo = self.repo()
                worktree = repo / "execute-worktree"
                worktree.mkdir(parents=True)
                report = worktree / self.REPORT_NAME
                if label == "symlink":
                    (worktree / "real.md").write_text(
                        "# Findings\n", encoding="utf-8")
                    report.symlink_to(worktree / "real.md")
                else:
                    report.mkdir()
                lane = mock.Mock(worktree=worktree, branch="side-lane/task-1")
                launch = mock.MagicMock()
                with self._patched(repo, lane, launch) as (_publish, dispose):
                    with self.assertRaises(cli.SideLaneError) as caught:
                        cli._launch(self._args(), cli.load_config(), repo, "Implement")
                self.assertIn(label, str(caught.exception))
                launch.assert_not_called()
                # The lane is still disposed of; nothing was unlinked or
                # replaced to make the report path inspectable.
                self.assertEqual(dispose.call_count, 1)
                self.assertTrue(report.is_symlink() if label == "symlink"
                                else report.is_dir())

    # --- report delivery is independent of implementation delivery -----------
    #
    # README documents report-only with `--allow-no-commit --no-publish`, but
    # the pre-repair verdict ANDed the report gate with implementation
    # `lane_delivery` (a commit plus a clean tree). A worker told to change no
    # source and make no git change can never satisfy that half, so an
    # authentic report-only lane exited 3 without `--allow-no-commit`, and with
    # it could only reach exit 0 by way of the report being an uncommitted path
    # the flag's own condition rejects — `delivered: false` for a lane that had
    # in fact delivered its report. These tests exercise the acceptance the way
    # a real lane meets it: a real worktree, real git, and the real
    # `lane_delivery`.

    GIT = ("git", "-c", "user.email=dev@example.com", "-c", "user.name=Dev")
    FINDINGS = "# Findings\n\n- what this run actually measured\n"

    def _committed_repo(self, *, tracked_report: str | None = None) -> Path:
        """A repository whose checkout is clean, so a lane can be added."""
        repo = self.repo()
        if tracked_report is not None:
            (repo / self.REPORT_NAME).write_text(tracked_report, encoding="utf-8")
        subprocess.run(self.GIT + ("-C", str(repo), "add", "-A"),
                       check=True, capture_output=True)
        subprocess.run(self.GIT + ("-C", str(repo), "commit", "-m", "baseline"),
                       check=True, capture_output=True)
        return repo

    def _real_lane(self, worker, *, args_overrides=None, tracked_report=None,
                   verify=None, verify_lane=None, source_mutations_error=None,
                   tracked_scratch=None, capability_evidence=None):
        """Drive _launch over a real lane worktree and real git inspection.

        Only the host executable, the route, the adapter call, the audit write,
        the push, and (when a test passes one) the verification call are
        hermetic. The worktree is created by the real factory and the post-run
        verdict comes from the real `lane_delivery`, so what is under test is
        the state a lane actually reaches. ``worker`` receives the lane worktree
        and the coordinator checkout, and may return an exit code for the fake
        host process.

        ``verify`` is a real shell command run by the real `verify_lane` (a
        callable receives the coordinator checkout path, for a command that has
        to name it). ``verify_lane`` replaces the verification call instead, for
        tests that need to count calls or hand back a chosen verdict.
        ``source_mutations_error`` makes the post-run checkout comparison fail,
        and ``tracked_scratch`` commits one path under the scratch directory so
        a lane can be given a *tracked* file to touch there.
        """

        repo = self._committed_repo(tracked_report=tracked_report)
        if tracked_scratch is not None:
            scratch_file = repo / tracked_scratch
            scratch_file.parent.mkdir(parents=True, exist_ok=True)
            scratch_file.write_text("tracked before the lane\n", encoding="utf-8")
            subprocess.run(self.GIT + ("-C", str(repo), "add", "-A"),
                           check=True, capture_output=True)
            subprocess.run(self.GIT + ("-C", str(repo), "commit", "-m", "tracked scratch"),
                           check=True, capture_output=True)
        overrides = dict(args_overrides or {})
        if verify is not None:
            overrides["verify"] = verify(repo) if callable(verify) else verify
        created: list = []
        real_create = cli.create_worktree

        def capture(*args, **kwargs):
            lane = real_create(*args, **kwargs)
            created.append(lane)
            return lane

        def fake_launch(*_args, **_kwargs):
            worktree = created[0].worktree
            outcome = worker(worktree, repo)
            return LaneResult(
                ("claude",), 0 if outcome is None else outcome, worktree,
                "claude", "claude", "native-claude", "claude-sonnet-5",
                "oauth", False, "done", "",
            )

        launch = mock.MagicMock(side_effect=fake_launch)
        claude_route = (
            {"gateway": "native-claude", "auth_method": "oauth", "billable": False},
            {"runtime_model": "claude-sonnet-5", "protocol": "native-claude",
             "max_budget_usd": 2.5},
        )
        # The real verify_lane runs unless a test says otherwise: the whole
        # point of the integrity cases below is what a real command does to a
        # real lane. A test that only needs the call counted passes its own
        # mock.
        patched_verify = (
            contextlib.nullcontext() if verify_lane is None
            else mock.patch("side_lane.cli.verify_lane", verify_lane)
        )
        source_patch = (
            contextlib.nullcontext() if source_mutations_error is None
            else mock.patch("side_lane.cli.source_mutations",
                            side_effect=source_mutations_error)
        )
        capability_patch = (
            contextlib.nullcontext() if capability_evidence is None
            else mock.patch(
                "side_lane.cli._capability_report",
                return_value={"capability_evidence": capability_evidence},
            )
        )
        with (
            mock.patch("side_lane.cli.create_worktree", side_effect=capture),
            mock.patch("side_lane.cli._require_host_executable",
                       return_value="/opt/hosts/claude"),
            mock.patch("side_lane.cli.select_route", return_value=claude_route),
            mock.patch("side_lane.cli.require_native_oauth"),
            mock.patch("side_lane.adapters.claude.launch", launch),
            mock.patch("side_lane.cli.write_audit",
                       return_value=repo / ".git" / "audit.json"),
            mock.patch("side_lane.cli.publish_lane_branch") as publish,
            source_patch,
            capability_patch,
            patched_verify as verify_call,
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            code = cli._launch(
                self._args(**overrides), cli.load_config(), repo, "Research",
            )
        self.verify_call = verify_call
        return code, output.getvalue(), errors.getvalue(), publish, created[0], repo

    def _write_report(self, worktree, content=None) -> None:
        (worktree / self.REPORT_NAME).write_text(
            self.FINDINGS if content is None else content, encoding="utf-8")

    def test_real_report_only_lane_without_a_commit_is_delivered(self) -> None:
        """The defect this repair closes: an authentic report-only lane.

        The worker wrote its report and nothing else, and made no git change
        because it was told not to. Pre-repair this exited 3 without
        `--allow-no-commit` (and reported `delivered: false` with it).
        """

        def worker(worktree, _repo):
            self._write_report(worktree)

        for allow_no_commit in (False, True):
            with self.subTest(allow_no_commit=allow_no_commit):
                code, stdout, errors, publish, lane, _repo = self._real_lane(
                    worker, args_overrides={
                        "allow_no_commit": allow_no_commit, "no_publish": True,
                    })
                self.assertEqual(code, 0, errors)
                summary = _only_summary(stdout)
                self.assertTrue(summary["delivered"])
                self.assertEqual(summary["report_state"], "current")
                self.assertFalse(summary["committed"])
                self.assertEqual(summary["uncommitted"], [self.REPORT_NAME])
                self.assertEqual(summary["report_only_unexpected_paths"], [])
                publish.assert_not_called()

    def test_real_report_only_lane_is_never_published(self) -> None:
        """No automatic push of a report branch, with or without --no-publish."""

        def worker(worktree, _repo):
            self._write_report(worktree)

        code, stdout, errors, publish, _lane, _repo = self._real_lane(
            worker, args_overrides={"no_publish": False})
        self.assertEqual(code, 0, errors)
        summary = _only_summary(stdout)
        self.assertTrue(summary["delivered"])
        publish.assert_not_called()
        self.assertIsNone(summary["published"])
        self.assertNotIn("published_ref", summary)

    def test_real_report_only_lane_that_commits_is_not_delivered(self) -> None:
        def worker(worktree, _repo):
            self._write_report(worktree)
            (worktree / "notes.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(self.GIT + ("-C", str(worktree), "add", "notes.py"),
                           check=True, capture_output=True)
            subprocess.run(
                self.GIT + ("-C", str(worktree), "commit", "-m", "worker commit"),
                check=True, capture_output=True)

        code, stdout, errors, publish, _lane, _repo = self._real_lane(worker)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertTrue(summary["committed"])
        self.assertIn("committed", errors)
        publish.assert_not_called()

    def test_real_report_only_lane_with_source_work_is_not_delivered(self) -> None:
        """A fresh report does not excuse uncommitted implementation work."""

        def worker(worktree, _repo):
            self._write_report(worktree)
            (worktree / "src").mkdir()
            (worktree / "src" / "change.py").write_text(
                "print('unrelated')\n", encoding="utf-8")

        code, stdout, errors, publish, _lane, _repo = self._real_lane(worker)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        # The report itself is still this run's; the source tree is what fails.
        self.assertEqual(summary["report_state"], "current")
        # Git collapses a wholly untracked directory into one entry, and these
        # are git's own paths: the operator is sent to what git reported.
        self.assertEqual(summary["report_only_unexpected_paths"], ["src/"])
        self.assertIn("src/", errors)
        publish.assert_not_called()

    def test_real_report_only_lane_may_replace_a_tracked_report(self) -> None:
        """A tracked report shows up modified, not untracked — still allowed.

        It is the lane's own artifact either way, and it is fresh, so the
        replacement is the delivery. The inherited-and-untouched case is the
        stale one covered above.
        """

        def worker(worktree, _repo):
            self._write_report(worktree)

        code, stdout, errors, _publish, lane, _repo = self._real_lane(
            worker, tracked_report=self.INHERITED)
        self.assertEqual(code, 0, errors)
        summary = _only_summary(stdout)
        self.assertTrue(summary["delivered"])
        self.assertEqual(summary["report_state"], "current")
        self.assertEqual(summary["report_preexisting"], True)
        self.assertEqual(summary["uncommitted"], [self.REPORT_NAME])
        self.assertEqual((lane.worktree / self.REPORT_NAME).read_text(
            encoding="utf-8"), self.FINDINGS)

    def test_real_report_only_lane_is_not_delivered_when_the_checkout_changed(self) -> None:
        """The source-checkout guard still fails the run, on exit 6.

        A changed coordinator checkout is fatal for the run, and the
        machine-readable verdict says so too: `delivered` is what a downstream
        consumer (a model qualification, for one) reads, and it never sees the
        exit code.
        """

        def worker(worktree, repo):
            self._write_report(worktree)
            (repo / "stray-report.md").write_text("outside the lane\n",
                                                 encoding="utf-8")

        code, stdout, errors, publish, _lane, _repo = self._real_lane(worker)
        self.assertEqual(code, cli.LANE_SOURCE_MUTATED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["source_changes"], ["stray-report.md"])
        self.assertIn("stray-report.md", errors)
        publish.assert_not_called()

    def test_scratch_and_screenshot_artifacts_are_not_implementation_work(self) -> None:
        """The documented exceptions: the report, and *untracked* scratch files.

        Screenshots and intermediate output belong in `.side-lane-scratch/`,
        which git excludes — so this is decided from the tree verdict the runner
        would receive if that exclusion were ever missing. The exemption is the
        status (`??`) as much as the prefix, which the tracked-mutation test
        below pins from the other side.
        """
        code, stdout, _stderr, _launch, _worktree = self._drive(
            report="# Findings\n", delivery=LaneDelivery(
                committed=False,
                uncommitted=(self.REPORT_NAME,
                             ".side-lane-scratch/notes.md",
                             ".side-lane-scratch/screenshots/before.png"),
                changed=(ChangedPath(UNTRACKED, self.REPORT_NAME),
                         ChangedPath(UNTRACKED, ".side-lane-scratch/notes.md"),
                         ChangedPath(UNTRACKED,
                                     ".side-lane-scratch/screenshots/before.png")),
            ))
        self.assertEqual(code, 0)
        summary = _only_summary(stdout)
        self.assertTrue(summary["delivered"])
        self.assertEqual(summary["report_only_unexpected_paths"], [])

    def test_a_tracked_change_under_scratch_is_source_work(self) -> None:
        """A path-only prefix match would accept this; the status is what stops it.

        Each of these is a change to a path git tracks under the scratch
        directory — the same string as a legitimate screenshot to a path-only
        API, and a source change to git. Every one must be refused, and the
        report itself must stay identified as this run's own.
        """
        for label, status in (
            ("modified", " M"),
            ("staged", "M "),
            ("added to the index", "A "),
            ("deleted", " D"),
            ("renamed", "R "),
        ):
            with self.subTest(label=label):
                scratch = ".side-lane-scratch/tracked-note.md"
                code, stdout, stderr, _launch, _worktree = self._drive(
                    report="# Findings\n", delivery=LaneDelivery(
                        committed=False,
                        uncommitted=(self.REPORT_NAME, scratch),
                        changed=(ChangedPath(UNTRACKED, self.REPORT_NAME),
                                 ChangedPath(status, scratch)),
                    ))
                self.assertEqual(code, cli.LANE_NOT_DELIVERED)
                summary = _only_summary(stdout)
                self.assertFalse(summary["delivered"])
                self.assertEqual(summary["report_state"], "current")
                self.assertEqual(summary["report_only_unexpected_paths"], [scratch])
                self.assertIn(scratch, stderr)

    def test_a_path_without_a_status_is_refused_rather_than_guessed(self) -> None:
        """An inspection that carried no status is not evidence of a screenshot.

        `LaneDelivery` can be built without statuses (the tree verdict's other
        callers never need them), and the one thing the scratch rule must not
        do is read "no status" as "untracked" and wave a tracked edit through.
        """
        code, stdout, _stderr, _launch, _worktree = self._drive(
            report="# Findings\n", delivery=LaneDelivery(
                committed=False,
                uncommitted=(self.REPORT_NAME, ".side-lane-scratch/notes.md"),
            ))
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["report_only_unexpected_paths"],
                         [".side-lane-scratch/notes.md"])

    def test_a_screenshot_outside_the_scratch_directory_is_refused(self) -> None:
        """Fail closed and name the path instead of accepting it silently.

        An uncommitted file outside the report and scratch tree does not
        survive the worktree and is not the artifact this lane was dispatched
        to produce; the operator is told where such artifacts belong.
        """
        code, stdout, stderr, _launch, _worktree = self._drive(
            report="# Findings\n", delivery=LaneDelivery(
                committed=False,
                uncommitted=(self.REPORT_NAME, "screenshots/after.png"),
            ))
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["report_state"], "current")
        self.assertEqual(summary["report_only_unexpected_paths"],
                         ["screenshots/after.png"])
        self.assertIn("screenshots/after.png", stderr)
        self.assertIn(".side-lane-scratch", stderr)

    def test_a_failed_lane_inspection_is_not_claimed_as_delivered(self) -> None:
        code, stdout, stderr, _launch, _worktree = self._drive(
            report="# Findings\n", no_publish=False,
            delivery_error=WorktreeError("git status failed: no such worktree"))
        self.assertEqual(code, cli.LANE_DELIVERY_UNVERIFIED)
        summary = _only_summary(stdout)
        self.assertIsNone(summary["delivered"])
        self.assertIsNone(summary["verified"])
        self.assertIn("git status failed", summary["delivery_unverified"])
        self.assertIn("not claimed", stderr)
        self.publish_lane.assert_not_called()

    def test_a_nonzero_worker_exit_is_still_retained(self) -> None:
        code, stdout, _stderr, _launch, _worktree = self._drive(
            report="# Findings\n", returncode=4)
        self.assertEqual(code, 4)
        self.assertEqual(_only_summary(stdout)["provider_exit_status"], 4)

    def test_a_report_path_outside_the_lane_is_not_judgeable(self) -> None:
        """A path the lane does not contain decides nothing — fail closed."""
        worktree = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, worktree, ignore_errors=True)
        self.assertEqual(
            cli._report_only_lane_artifacts(worktree, worktree / self.REPORT_NAME),
            frozenset({self.REPORT_NAME}),
        )
        self.assertIsNone(
            cli._report_only_lane_artifacts(worktree, Path("/elsewhere") / self.REPORT_NAME))
        self.assertIsNone(
            cli._report_only_lane_artifacts(
                worktree, worktree / "nested" / self.REPORT_NAME))

    def test_the_ordinary_execute_contract_is_unchanged(self) -> None:
        """Without the opt-in, the report artifact is just an uncommitted file."""
        code, stdout, _stderr, _launch, _worktree = self._drive(
            report_only=False, report="# Findings\n",
            delivery=LaneDelivery(committed=False,
                                  uncommitted=(self.REPORT_NAME,)))
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertNotIn("report_only_unexpected_paths", summary)

    # --- the machine-readable verdict covers the whole run -------------------
    #
    # `summary["delivered"]` is what a downstream consumer reads — the model
    # qualification step among them — and it never sees this run's exit code.
    # A lane this run has already failed must therefore not be reported as
    # delivered, however good the report artifact is on its own.

    def test_real_report_only_summary_is_false_when_the_worker_exits_nonzero(self) -> None:
        def worker(worktree, _repo):
            self._write_report(worktree)
            return 4

        code, stdout, errors, publish, _lane, _repo = self._real_lane(worker)
        self.assertEqual(code, 4, errors)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["provider_exit_status"], 4)
        # The report itself was a real one: it is the worker's exit, not the
        # artifact, that the verdict now reflects.
        self.assertEqual(summary["report_state"], "current")
        publish.assert_not_called()

    def test_real_report_only_summary_is_false_when_the_source_check_fails(self) -> None:
        def worker(worktree, _repo):
            self._write_report(worktree)

        code, stdout, errors, _publish, _lane, _repo = self._real_lane(
            worker,
            source_mutations_error=WorktreeError("git status failed: gone"),
        )
        self.assertEqual(code, cli.LANE_DELIVERY_UNVERIFIED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["report_state"], "current")
        self.assertIn("git status failed", summary["source_check_unverified"])
        self.assertIn("not claimed clean", errors)

    def test_real_report_only_verify_failure_is_not_delivered(self) -> None:
        def worker(worktree, _repo):
            self._write_report(worktree)

        failing = mock.MagicMock(
            return_value=VerifyResult("make test", 2, "FAILED (failures=1)"))
        code, stdout, errors, _publish, _lane, _repo = self._real_lane(
            worker, verify="make test", verify_lane=failing)
        self.assertEqual(code, cli.LANE_VERIFY_FAILED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertFalse(summary["verified"])
        self.assertIn("FAILED (failures=1)", errors)

    def test_real_report_only_verify_does_not_run_after_a_worker_failure(self) -> None:
        """A run already failed for the worker is not worth a shell command."""

        def worker(worktree, _repo):
            self._write_report(worktree)
            return 5

        verify = mock.MagicMock()
        code, stdout, _errors, _publish, _lane, _repo = self._real_lane(
            worker, verify="make test", verify_lane=verify)
        self.assertEqual(code, 5)
        verify.assert_not_called()
        summary = _only_summary(stdout)
        self.assertIsNone(summary["verified"])
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["report_only_verification_changes"], [])

    def test_real_report_only_verify_does_not_run_when_the_checkout_changed(self) -> None:
        def worker(worktree, repo):
            self._write_report(worktree)
            (repo / "stray.md").write_text("outside\n", encoding="utf-8")

        verify = mock.MagicMock()
        code, stdout, _errors, _publish, _lane, _repo = self._real_lane(
            worker, verify="make test", verify_lane=verify)
        self.assertEqual(code, cli.LANE_SOURCE_MUTATED)
        verify.assert_not_called()
        summary = _only_summary(stdout)
        self.assertIsNone(summary["verified"])
        self.assertFalse(summary["delivered"])

    # --- --verify is a command, and it can change what was just judged -------
    #
    # It runs an arbitrary shell command inside the lane, so it can commit,
    # leave files, rewrite the report the verdict was reached on, or write into
    # the coordinator checkout. The lane that gets accepted must be the lane
    # that was judged, so each of these is a real command against a real lane.

    def test_real_report_only_verify_that_touches_nothing_is_delivered(self) -> None:
        def worker(worktree, _repo):
            self._write_report(worktree)

        code, stdout, errors, _publish, _lane, _repo = self._real_lane(
            worker, verify="printf 'ok\\n'")
        self.assertEqual(code, 0, errors)
        summary = _only_summary(stdout)
        self.assertTrue(summary["delivered"])
        self.assertTrue(summary["verified"])
        self.assertEqual(summary["verify_exit"], 0)
        self.assertEqual(summary["report_only_verification_changes"], [])

    def test_real_report_only_verify_that_rewrites_the_report_is_refused(self) -> None:
        def worker(worktree, _repo):
            self._write_report(worktree)

        code, stdout, errors, _publish, _lane, _repo = self._real_lane(
            worker, verify=f"printf 'rewritten by verify\\n' > {self.REPORT_NAME}")
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        # The command exited 0; it is what it changed, not how it exited, that
        # disqualifies the lane.
        self.assertTrue(summary["verified"])
        self.assertEqual(len(summary["report_only_verification_changes"]), 1)
        self.assertIn("report artifact",
                      summary["report_only_verification_changes"][0])
        self.assertIn("--verify changed what this report-only run had judged",
                      errors)

    def test_real_report_only_verify_that_leaves_a_file_is_refused(self) -> None:
        def worker(worktree, _repo):
            self._write_report(worktree)

        code, stdout, _errors, _publish, _lane, _repo = self._real_lane(
            worker, verify="printf 'out\\n' > build-output.txt")
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["report_only_verification_changes"],
                         ["it left build-output.txt in the lane"])

    def test_real_report_only_verify_that_commits_is_refused(self) -> None:
        def worker(worktree, _repo):
            self._write_report(worktree)

        git = "git -c user.email=dev@example.com -c user.name=Dev"
        code, stdout, _errors, publish, _lane, _repo = self._real_lane(
            worker, verify=f"{git} add -A && {git} commit -m verified")
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["report_only_verification_changes"],
                         ["it committed work on the lane branch"])
        publish.assert_not_called()

    def test_real_report_only_verify_that_mutates_the_checkout_is_refused(self) -> None:
        def worker(worktree, _repo):
            self._write_report(worktree)

        code, stdout, errors, _publish, _lane, _repo = self._real_lane(
            worker,
            verify=lambda repo: f"printf 'x\\n' > '{repo / 'verify-stray.md'}'",
        )
        # A changed coordinator checkout is exit 6 whoever changed it, and the
        # summary names the path rather than hiding it behind the lane refusal.
        self.assertEqual(code, cli.LANE_SOURCE_MUTATED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertTrue(summary["source_mutated"])
        self.assertEqual(summary["source_changes"], ["verify-stray.md"])
        self.assertIn("it changed the coordinator checkout at verify-stray.md",
                      summary["report_only_verification_changes"])
        self.assertIn("verify-stray.md", errors)

    # --- the scratch exemption is a status, not a prefix ---------------------
    #
    # `.side-lane-scratch/` is git-excluded in a lane, so git normally says
    # nothing about it at all. These drive real git on a repository that
    # *tracks* a file there, which is the case a prefix-only rule would wave
    # through: git reports the change, and it is a change to a tracked path.

    SCRATCH_TRACKED = ".side-lane-scratch/tracked-note.md"

    def test_real_source_renamed_into_report_is_refused(self) -> None:
        def worker(worktree, _repo):
            subprocess.run(self.GIT + ("-C", str(worktree), "mv",
                           self.SCRATCH_TRACKED, self.REPORT_NAME),
                           check=True, capture_output=True)

        code, stdout, errors, _publish, _lane, _repo = self._real_lane(
            worker, tracked_scratch=self.SCRATCH_TRACKED)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED, errors)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertIn(self.REPORT_NAME, summary["report_only_unexpected_paths"])

    def test_copy_status_at_report_path_is_refused(self) -> None:
        delivery = LaneDelivery(
            committed=False, uncommitted=(self.REPORT_NAME,),
            changed=(ChangedPath("C ", self.REPORT_NAME),))
        self.assertEqual(cli._report_only_unexpected_paths(
            delivery, frozenset({self.REPORT_NAME})), (self.REPORT_NAME,))

    def test_real_tracked_change_under_scratch_is_refused(self) -> None:
        def worker(worktree, _repo):
            self._write_report(worktree)
            (worktree / self.SCRATCH_TRACKED).write_text(
                "edited by the worker\n", encoding="utf-8")

        code, stdout, errors, _publish, _lane, _repo = self._real_lane(
            worker, tracked_scratch=self.SCRATCH_TRACKED)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED, errors)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["report_state"], "current")
        self.assertEqual(summary["report_only_unexpected_paths"],
                         [self.SCRATCH_TRACKED])
        self.assertIn(self.SCRATCH_TRACKED, errors)

    def test_real_deletion_of_a_tracked_path_under_scratch_is_refused(self) -> None:
        def worker(worktree, _repo):
            self._write_report(worktree)
            (worktree / self.SCRATCH_TRACKED).unlink()

        code, stdout, _errors, _publish, _lane, _repo = self._real_lane(
            worker, tracked_scratch=self.SCRATCH_TRACKED)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["report_only_unexpected_paths"],
                         [self.SCRATCH_TRACKED])

    def test_real_staged_addition_under_scratch_is_refused(self) -> None:
        added = ".side-lane-scratch/added.md"

        def worker(worktree, _repo):
            self._write_report(worktree)
            (worktree / added).write_text("staged by the worker\n", encoding="utf-8")
            subprocess.run(self.GIT + ("-C", str(worktree), "add", "-f", added),
                           check=True, capture_output=True)

        code, stdout, _errors, _publish, _lane, _repo = self._real_lane(worker)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["report_only_unexpected_paths"], [added])

    def test_real_untracked_screenshot_in_scratch_is_accepted(self) -> None:
        """The legitimate case, on real git: a new file where the rules say.

        Git excludes the lane's scratch tree, so the accepted lane is the
        evidence: the screenshot is really on disk and the run was not failed
        for a path the governance itself told the worker to use.
        """
        screenshot = Path(".side-lane-scratch") / "screenshots" / "after.png"

        def worker(worktree, _repo):
            self._write_report(worktree)
            (worktree / screenshot).parent.mkdir(parents=True, exist_ok=True)
            (worktree / screenshot).write_bytes(b"\x89PNG\r\n\x1a\n")

        code, stdout, errors, _publish, lane, _repo = self._real_lane(worker)
        self.assertEqual(code, 0, errors)
        summary = _only_summary(stdout)
        self.assertTrue(summary["delivered"])
        self.assertEqual(summary["report_only_unexpected_paths"], [])
        self.assertTrue((lane.worktree / screenshot).exists())

    # --- the report deliverable without the Claude-only repair ---------------
    #
    # Two concerns were fused in one flag: "the report is the deliverable" and
    # "a Stop hook plus a USD cap buys one more turn to write it". Only the
    # second is Claude's, and only the second bounds spend.
    # `--report-deliverable` selects the first alone, so a report lane can run
    # on any host and on any route — including the provider-key routes that
    # carry no `max_budget_usd`. `--report-only` implies it, so the Claude+cap
    # path is byte-identical.

    def test_parser_accepts_the_deliverable_flag(self) -> None:
        args = cli.make_parser().parse_args([
            "run", "--host", "devin", "--mode", "execute", "--provider", "claude",
            "--model", "claude-sonnet-5", "--repo", ".", "--lane-name", "task",
            "--prompt", "Research", "--report-deliverable",
        ])
        self.assertIs(args.report_deliverable, True)
        self.assertIs(args.report_only, False)

    def test_the_deliverable_flag_needs_no_budget_and_arms_no_hook(self) -> None:
        """The verdict, without the cap or the hook that flag alone owns."""

        code, stdout, errors, launch, _worktree = self._drive(
            report_only=False, report_deliverable=True,
            model_config={"runtime_model": "claude-sonnet-5",
                          "protocol": "native-claude"},
            report="# Findings\n- item\n")
        self.assertEqual(code, 0, errors)
        self.assertTrue(_only_summary(stdout)["delivered"])
        self.assertIs(launch.call_args.kwargs["report_only"], False)

    def test_the_deliverable_verdict_launches_on_every_host(self) -> None:
        """Host-neutral: what a bare `--report-only` refuses codex/devin for is
        the Stop hook, not the verdict."""

        for host in sorted(HOST_LAUNCH_TARGETS):
            with self.subTest(host=host):
                code, stdout, errors, launch, _worktree = self._drive(
                    host=host, report_only=False, report_deliverable=True,
                    model_config={"runtime_model": "claude-sonnet-5",
                                  "protocol": "native-claude"},
                    report="# Findings\n- item\n")
                self.assertEqual(code, 0, errors)
                self.assertTrue(_only_summary(stdout)["delivered"])
                if host != "claude":
                    # No other adapter is even offered the opt-in.
                    self.assertNotIn("report_only", launch.call_args.kwargs)

    def test_report_only_still_requires_the_budget_the_hook_buys(self) -> None:
        """The spend guard is unchanged, and it is the opt-in's own."""

        base = {"runtime_model": "claude-sonnet-5", "protocol": "native-claude"}
        code, _stdout, errors, _launch, _worktree = self._drive(
            report_only=False, report_deliverable=True, model_config=base,
            report="# Findings\n")
        self.assertEqual(code, 0, errors)
        with self.assertRaises(cli.SideLaneError) as caught:
            self._drive(report_only=True, model_config=base, report="# Findings\n")
        self.assertIn("max_budget_usd", str(caught.exception))

    def test_report_only_still_selects_the_same_report_deliverable(self) -> None:
        code, stdout, errors, launch, _worktree = self._drive(
            report_only=True, report_deliverable=True, report="# Findings\n")
        self.assertEqual(code, 0, errors)
        self.assertTrue(_only_summary(stdout)["delivered"])
        self.assertIs(launch.call_args.kwargs["report_only"], True)

    def test_the_deliverable_flag_is_refused_in_review_mode(self) -> None:
        repo = self.repo()
        with (
            mock.patch("side_lane.cli.create_worktree") as create,
            mock.patch("side_lane.adapters.claude.launch") as launch,
        ):
            with self.assertRaises(cli.SideLaneError) as caught:
                cli._launch(
                    self._args(report_only=False, report_deliverable=True,
                               mode="review"),
                    cli.load_config(), repo, "Review")
        self.assertIn("execute", str(caught.exception))
        create.assert_not_called()
        launch.assert_not_called()

    def test_a_publication_request_on_a_report_run_is_refused(self) -> None:
        """A report run never publishes, so it cannot be granted a push.

        `git-push` is the argv's only publication request. A report lane leaves
        an uncommitted artifact and no commit to make remote-contained, so the
        grant would be inert authority handed to a worker for a run that will
        never exercise it. Refused before a lane exists, on both flags.
        """

        for label, overrides in (
            ("--report-only", {"report_only": True}),
            ("--report-deliverable", {"report_deliverable": True}),
        ):
            with self.subTest(flag=label):
                repo = self.repo()
                values = {"report_only": False, "capability": ["git-push"]}
                values.update(overrides)
                with (
                    mock.patch("side_lane.cli.create_worktree") as create,
                    mock.patch("side_lane.adapters.claude.launch") as launch,
                ):
                    with self.assertRaises(cli.SideLaneError) as caught:
                        cli._launch(self._args(**values), cli.load_config(),
                                    repo, "Research")
                self.assertIn("git-push", str(caught.exception))
                create.assert_not_called()
                launch.assert_not_called()

    def test_a_deliverable_lane_is_never_published(self) -> None:
        code, stdout, errors, _launch, _worktree = self._drive(
            report_only=False, report_deliverable=True, no_publish=False,
            model_config={"runtime_model": "claude-sonnet-5",
                          "protocol": "native-claude"},
            report="# Findings\n")
        self.assertEqual(code, 0, errors)
        summary = _only_summary(stdout)
        self.assertTrue(summary["delivered"])
        self.publish_lane.assert_not_called()
        self.assertIsNone(summary["published"])

    def test_a_deliverable_lane_that_commits_is_not_delivered(self) -> None:
        code, stdout, errors, _launch, _worktree = self._drive(
            report_only=False, report_deliverable=True, report="# Findings\n",
            delivery=LaneDelivery(committed=True, uncommitted=()))
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        self.assertFalse(_only_summary(stdout)["delivered"])
        self.assertIn("committed", errors)

    def test_a_deliverable_lane_with_an_unusable_report_is_not_delivered(self) -> None:
        for label, report in (("missing", None), ("blank", "  \n\t ")):
            with self.subTest(label=label):
                code, stdout, _errors, _launch, _worktree = self._drive(
                    report_only=False, report_deliverable=True, report=report)
                self.assertEqual(code, cli.LANE_NOT_DELIVERED)
                summary = _only_summary(stdout)
                self.assertFalse(summary["delivered"])
                self.assertEqual(summary["report_state"], "unusable")

    def test_a_native_failure_cannot_be_a_deliverable_report(self) -> None:
        """A written report does not excuse a non-zero worker exit."""

        code, stdout, errors, _launch, _worktree = self._drive(
            report_only=False, report_deliverable=True, returncode=7,
            report="# Findings\n- item\n")
        self.assertEqual(code, 7)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["report_state"], "current")
        self.assertEqual(errors, "")

    # --- the browser-report artifact namespace -------------------------------
    #
    # A browser lane saves screenshots and page dumps at the lane root beside
    # its report, and the cloud worker already admits exactly that set. The
    # runner's verdict learned the same namespace, gated on the run's own
    # `playwright` grant and bounded by the same name rule and the same caps, so
    # the two halves cannot disagree about what an allowed artifact is. Without
    # the capability there is no exemption at all: it is that run's exception,
    # never a general root pass.

    PLAYWRIGHT_EVIDENCE = {"playwright": {"state": "present", "basis": "test"}}

    def _artifact_lane(self, worker, *, capability=("playwright",),
                       deliverable=True, **kwargs):
        """Drive a real lane with the browser namespace enabled or not."""

        overrides = dict(kwargs.pop("args_overrides", {}))
        overrides.setdefault("capability", list(capability))
        overrides.setdefault("report_only", not deliverable)
        overrides.setdefault("report_deliverable", deliverable)
        return self._real_lane(
            worker, args_overrides=overrides,
            capability_evidence=(self.PLAYWRIGHT_EVIDENCE if capability else None),
            **kwargs)

    def test_the_namespace_predicate_matches_the_shared_contract_table(self) -> None:
        """The runner's half of one table the cloud worker asserts too.

        The fixture is the cross-repo anchor: changing the namespace or its caps
        on one side leaves the other side's assertion failing, so the two halves
        cannot drift apart the way they did before this repair.
        """

        table = _report_artifact_name_table()
        self.assertEqual(cli.BROWSER_REPORT_ARTIFACT_RE.pattern, table["pattern"])
        self.assertEqual(cli.BROWSER_ARTIFACT_MAX_FILES, table["max_files"])
        self.assertEqual(cli.BROWSER_ARTIFACT_MAX_BYTES, table["max_bytes"])
        self.assertEqual(cli.BROWSER_ARTIFACT_MAX_TOTAL_BYTES,
                         table["max_total_bytes"])
        for name in table["valid"]:
            with self.subTest(name=name, verdict="valid"):
                self.assertTrue(cli._is_browser_report_artifact_name(name), name)
        for name in table["invalid"]:
            with self.subTest(name=name, verdict="invalid"):
                self.assertFalse(cli._is_browser_report_artifact_name(name), name)

    def test_a_playwright_lane_may_leave_named_artifacts(self) -> None:
        artifacts = (
            "SIDE_LANE_REPORT-c1-authenticated-home.png",
            "SIDE_LANE_REPORT-c1-authenticated-home.yml",
            "SIDE_LANE_REPORT-network-nonstatic.txt",
        )

        def worker(worktree, _repo):
            self._write_report(worktree)
            for name in artifacts:
                (worktree / name).write_bytes(b"artifact\n")

        for deliverable in (True, False):
            with self.subTest(report_deliverable=deliverable):
                code, stdout, errors, publish, _lane, _repo = self._artifact_lane(
                    worker, deliverable=deliverable)
                self.assertEqual(code, 0, errors)
                summary = _only_summary(stdout)
                self.assertTrue(summary["delivered"])
                self.assertEqual(summary["report_only_unexpected_paths"], [])
                self.assertEqual(summary["report_state"], "current")
                publish.assert_not_called()

    def test_without_the_playwright_grant_the_namespace_is_source_work(self) -> None:
        """The capability is the gate, not the filenames."""

        def worker(worktree, _repo):
            self._write_report(worktree)
            (worktree / "SIDE_LANE_REPORT-c1-home.png").write_bytes(b"artifact\n")

        code, stdout, errors, publish, _lane, _repo = self._artifact_lane(
            worker, capability=())
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["report_only_unexpected_paths"],
                         ["SIDE_LANE_REPORT-c1-home.png"])
        self.assertIn("SIDE_LANE_REPORT-c1-home.png", errors)
        publish.assert_not_called()

    def test_a_tracked_change_at_a_namespace_name_is_source_work(self) -> None:
        """The exemption is the status, exactly as it is for scratch.

        A repository may track a file at a namespace name. Git reports a
        modification, staging, deletion, rename, or copy of a tracked path with
        its own code rather than `??`, and accepting the name alone would let a
        worker edit a tracked source file and still be accepted.
        """

        artifact = "SIDE_LANE_REPORT-c1-home.png"
        for label, status in (
            ("modified", " M"),
            ("staged", "M "),
            ("added to the index", "A "),
            ("deleted", " D"),
            ("renamed", "R "),
            ("copied", "C "),
        ):
            with self.subTest(label=label):
                code, stdout, stderr, _launch, _worktree = self._drive(
                    report_only=False, report_deliverable=True,
                    capability=("playwright",),
                    capability_evidence=self.PLAYWRIGHT_EVIDENCE,
                    worker_writes=((artifact, "artifact\n"),),
                    report="# Findings\n", delivery=LaneDelivery(
                        committed=False,
                        uncommitted=(self.REPORT_NAME, artifact),
                        changed=(ChangedPath(UNTRACKED, self.REPORT_NAME),
                                 ChangedPath(status, artifact)),
                    ))
                self.assertEqual(code, cli.LANE_NOT_DELIVERED)
                summary = _only_summary(stdout)
                self.assertFalse(summary["delivered"])
                self.assertEqual(summary["report_only_unexpected_paths"], [artifact])
                self.assertIn(artifact, stderr)

    def test_a_symlinked_artifact_is_refused(self) -> None:
        def worker(worktree, _repo):
            self._write_report(worktree)
            scratch = worktree / ".side-lane-scratch"
            scratch.mkdir(exist_ok=True)
            (scratch / "real.png").write_bytes(b"png\n")
            (worktree / "SIDE_LANE_REPORT-linked.png").symlink_to(
                scratch / "real.png")

        code, stdout, _errors, _publish, _lane, _repo = self._artifact_lane(worker)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["report_only_unexpected_paths"],
                         ["SIDE_LANE_REPORT-linked.png"])

    def test_an_artifact_over_the_byte_cap_is_refused(self) -> None:
        def worker(worktree, _repo):
            self._write_report(worktree)
            with (worktree / "SIDE_LANE_REPORT-huge.png").open("wb") as handle:
                handle.truncate(cli.BROWSER_ARTIFACT_MAX_BYTES + 1)

        code, stdout, _errors, _publish, _lane, _repo = self._artifact_lane(worker)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        self.assertEqual(_only_summary(stdout)["report_only_unexpected_paths"],
                         ["SIDE_LANE_REPORT-huge.png"])

    def test_the_file_count_and_aggregate_caps_bound_the_namespace(self) -> None:
        """Over either cap the exception stops applying, and only at the bound.

        Names are ordered, so which of an over-long set falls outside the cap is
        deterministic: the 41st of 41 files, and the 6th 20 MiB file — the one
        that would take the total past 100 MiB. Everything up to the cap is
        still this run's artifact and the lane is refused for the rest.
        """

        cases = (
            ("file count", 41, 1),
            ("aggregate bytes", 6, cli.BROWSER_ARTIFACT_MAX_BYTES),
        )
        for label, count, size in cases:
            with self.subTest(label=label):
                names = [
                    "SIDE_LANE_REPORT-%02d.png" % index for index in range(count)
                ]

                def worker(worktree, _repo, names=tuple(names), size=size):
                    self._write_report(worktree)
                    for name in names:
                        with (worktree / name).open("wb") as handle:
                            handle.truncate(size)

                code, stdout, _errors, _publish, _lane, _repo = (
                    self._artifact_lane(worker))
                self.assertEqual(code, cli.LANE_NOT_DELIVERED)
                self.assertEqual(
                    _only_summary(stdout)["report_only_unexpected_paths"],
                    [names[-1]])

    def test_a_fresh_report_is_still_required_beside_the_artifacts(self) -> None:
        def worker(worktree, _repo):
            (worktree / "SIDE_LANE_REPORT-c1-home.png").write_bytes(b"artifact\n")

        code, stdout, errors, publish, _lane, _repo = self._artifact_lane(worker)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["report_state"], "unusable")
        self.assertIn(self.REPORT_NAME, errors)
        publish.assert_not_called()

    def test_an_inherited_report_beside_artifacts_is_still_stale(self) -> None:
        def worker(worktree, _repo):
            (worktree / "SIDE_LANE_REPORT-c1-home.png").write_bytes(b"artifact\n")

        code, stdout, _errors, _publish, _lane, _repo = self._artifact_lane(
            worker, tracked_report=self.INHERITED)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["report_state"], "stale")

    def test_verify_that_rewrites_an_artifact_is_refused(self) -> None:
        """Artifact identity is rechecked after `--verify`, like report state.

        A rewrite in place leaves the path and its untracked status exactly as
        they were, so a name-set comparison would absorb it silently and accept
        a lane the run never judged.
        """

        artifact = "SIDE_LANE_REPORT-c1-home.png"

        def worker(worktree, _repo):
            self._write_report(worktree)
            (worktree / artifact).write_bytes(b"png\n")

        code, stdout, errors, _publish, _lane, _repo = self._artifact_lane(
            worker, verify="printf 'rewritten by verify\\n' > " + artifact)
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertTrue(summary["verified"])
        self.assertFalse(summary["delivered"])
        self.assertEqual(len(summary["report_only_verification_changes"]), 1)
        self.assertIn("report artifacts",
                      summary["report_only_verification_changes"][0])
        self.assertIn("--verify changed what this report-only run had judged",
                      errors)

    @unittest.skipUnless(
        hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_NONBLOCK"),
        "requires safe artifact-open primitives",
    )
    def test_verify_that_leaves_an_artifact_alone_still_delivers(self) -> None:
        """A successful `--verify` does not itself fail an artifact lane.

        The before state used to be built without the accepted artifact
        identities the after state carries, so the two could never be equal:
        every lane that left a browser artifact and ran a `--verify` that
        touched nothing was refused for a change the command had not made.
        """

        artifact = "SIDE_LANE_REPORT-c1-home.png"

        def worker(worktree, _repo):
            self._write_report(worktree)
            (worktree / artifact).write_bytes(b"png\n")

        code, stdout, errors, _publish, _lane, _repo = self._artifact_lane(
            worker, verify="printf 'verify ok\\n'")
        self.assertEqual(code, 0, errors)
        summary = _only_summary(stdout)
        self.assertTrue(summary["verified"])
        self.assertTrue(summary["delivered"])
        self.assertEqual(summary["report_only_verification_changes"], [])

    def test_an_accepted_artifact_nobody_could_read_is_unverifiable(self) -> None:
        """An unreadable artifact is refused, not accepted as unchanged.

        The comparison is between two identities, and two unreadable ones are
        equal: without the refusal this state would be read as "the command
        changed nothing" and the lane accepted on an artifact no look in the
        run could read. A ``None`` on one side only is no better — the run
        still cannot say what it judged — so every mixed and matching pair is
        refused rather than one of them slipping past as a difference that
        happened to be noticed.
        """

        artifact = "SIDE_LANE_REPORT-c1-home.png"
        report_path = Path("/lane") / report_stop_hook.REPORT_NAME
        read_identity = "sha256:read:size:4"

        def state(identity):
            return cli._ReportOnlyState(
                committed=False,
                unexpected=(),
                source_changes=(),
                report_identity="sha256:report:size:1",
                artifacts=((artifact, identity),),
            )

        for label, before_identity, after_identity in (
            ("unreadable in both looks", None, None),
            ("unreadable before only", None, read_identity),
            ("unreadable after only", read_identity, None),
        ):
            with self.subTest(label=label):
                changes = cli._report_only_verification_changes(
                    state(before_identity), state(after_identity),
                    report_path=report_path,
                )
                self.assertEqual(len(changes), 1, changes)
                self.assertIn("cannot be read as a plain file", changes[0])
                self.assertIn(artifact, changes[0])
        with self.subTest(label="read and unchanged in both looks"):
            self.assertEqual(
                cli._report_only_verification_changes(
                    state(read_identity), state(read_identity),
                    report_path=report_path,
                ),
                (),
            )

    def test_a_build_that_cannot_open_safely_refuses_the_artifact_lane(self) -> None:
        """No safe-open primitives means no artifact verdict, not a pass.

        With the flags absent, every accepted artifact's identity is ``None``
        in both looks, so this is the end-to-end shape of the unverifiable
        state above: the artifact is admitted by name, status, and size, and
        no read of it is possible. The lane is refused and the summary carries
        the reason rather than a delivery this run never judged.
        """

        artifact = "SIDE_LANE_REPORT-c1-home.png"

        def worker(worktree, _repo):
            self._write_report(worktree)
            (worktree / artifact).write_bytes(b"png\n")

        with mock.patch.object(cli, "_ARTIFACT_NOFOLLOW_OPEN", None):
            code, stdout, errors, _publish, _lane, _repo = self._artifact_lane(
                worker, verify="printf 'verify ok\\n'")
        self.assertEqual(code, cli.LANE_NOT_DELIVERED)
        summary = _only_summary(stdout)
        self.assertTrue(summary["verified"])
        self.assertFalse(summary["delivered"])
        changes = summary["report_only_verification_changes"]
        self.assertEqual(len(changes), 1, changes)
        self.assertIn("cannot be read as a plain file", changes[0])
        self.assertIn(artifact, changes[0])
        self.assertIn("--verify changed what this report-only run had judged",
                      errors)

    @unittest.skipUnless(
        hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_NONBLOCK"),
        "requires safe artifact-open primitives",
    )
    def test_a_same_length_rewrite_past_the_sampled_prefix_is_refused(self) -> None:
        """The artifact identity reads the whole admitted file, not a prefix.

        The report's own identity rule samples a bounded prefix, which is the
        right bargain for the report and the wrong one for this namespace: an
        artifact is admitted up to ``BROWSER_ARTIFACT_MAX_BYTES``, so a rewrite
        in place that keeps the length and lands past that prefix leaves a
        sampled identity — and a whole name-set comparison — unchanged, and the
        lane would be accepted on a state the run never judged. Hashing the
        whole admitted file is what distinguishes the two, and the identical
        accepted artifact beside it is what proves the refusal is the change
        and not the size.
        """

        artifact = "SIDE_LANE_REPORT-c1-long.png"
        offset = report_stop_hook.MAX_IDENTITY_BYTES
        self.assertLess(offset, cli.BROWSER_ARTIFACT_MAX_BYTES)

        def worker(worktree, _repo):
            self._write_report(worktree)
            with (worktree / artifact).open("wb") as handle:
                handle.truncate(cli.BROWSER_ARTIFACT_MAX_BYTES)

        def rewrite(path):
            return (
                "python3 -c \"f=open('%s','r+b');f.seek(%d);f.write(b'X');"
                "f.close()\"" % (path, offset)
            )

        for label, verify, delivered in (
            ("rewritten in place past the sampled prefix", rewrite(artifact), False),
            ("untouched at the artifact cap", "printf 'verify ok\\n'", True),
        ):
            with self.subTest(label=label):
                code, stdout, errors, _publish, _lane, _repo = self._artifact_lane(
                    worker, verify=verify)
                summary = _only_summary(stdout)
                self.assertTrue(summary["verified"])
                self.assertEqual(summary["delivered"], delivered, errors)
                self.assertEqual(
                    code, 0 if delivered else cli.LANE_NOT_DELIVERED, errors)
                changes = summary["report_only_verification_changes"]
                if delivered:
                    self.assertEqual(changes, [])
                else:
                    self.assertEqual(len(changes), 1)
                    self.assertIn("report artifacts", changes[0])
                    self.assertIn(artifact, changes[0])
                    self.assertIn("--verify changed what this report-only run "
                                  "had judged", errors)

    def test_a_browser_report_lane_that_mutates_the_checkout_still_fails(self) -> None:
        def worker(worktree, repo):
            self._write_report(worktree)
            (worktree / "SIDE_LANE_REPORT-c1-home.png").write_bytes(b"artifact\n")
            (repo / "stray-report.md").write_text("outside the lane\n",
                                                  encoding="utf-8")

        code, stdout, errors, _publish, _lane, _repo = self._artifact_lane(worker)
        self.assertEqual(code, cli.LANE_SOURCE_MUTATED)
        summary = _only_summary(stdout)
        self.assertFalse(summary["delivered"])
        self.assertEqual(summary["source_changes"], ["stray-report.md"])
        self.assertIn("stray-report.md", errors)


class ArtifactIdentitySafeOpenTests(unittest.TestCase):
    """``_artifact_identity`` reads the file ``lstat`` confirmed, or nothing.

    The path can be swapped between the caller's ``lstat`` and the open — that
    is the whole reason the look cannot be ``open(path, "rb")`` — so these
    tests drive the swap for real: the name is replaced after the stat was
    taken, and only the patched ``lstat`` still describes the file that was
    there. What the open then does with the swapped name is the subject.
    """

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="artifact-safe-open-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.artifact = self.directory / "SIDE_LANE_REPORT-c1-home.png"

    def _stale_lstat(self, info):
        """The ``lstat`` a caller took, for a name that has changed since."""

        return mock.patch.object(cli.os, "lstat", return_value=info)

    def _recording_open(self, seen, *, require):
        """``os.open`` that records its flags, and refuses a dangerous one.

        The refusal is what keeps this test from hanging the suite: a real
        read-only open of a FIFO without the safe flag waits for a writer, so
        a regression that drops the flag has to fail here instead of blocking.
        """

        real_open = os.open

        def open_with_flags(path, flags, *args, **kwargs):
            seen.append(flags)
            if not all(flags & flag for flag in require):
                raise AssertionError(
                    f"os.open called without {require!r}: {flags!r} would "
                    "follow a swapped link or block on a swapped FIFO"
                )
            return real_open(path, flags, *args, **kwargs)

        return mock.patch.object(cli.os, "open", side_effect=open_with_flags)

    @unittest.skipUnless(
        hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_NONBLOCK"),
        "requires safe artifact-open primitives",
    )
    def test_a_symlink_swapped_in_after_lstat_is_not_followed(self) -> None:
        """The swapped name is refused, never read through to its target.

        ``lstat`` confirmed the plain file that was there; by the time the
        open runs, the name is a link to a file this lane did not deliver. An
        identity for that file would be an identity of foreign content.
        """

        self.artifact.write_bytes(b"artifact\n")
        info = os.lstat(self.artifact)
        elsewhere = self.directory / "elsewhere.png"
        elsewhere.write_bytes(b"content this lane did not deliver\n")
        self.artifact.unlink()
        self.artifact.symlink_to(elsewhere)

        seen: list[int] = []
        with self._stale_lstat(info), self._recording_open(
            seen, require=(os.O_NOFOLLOW,)
        ):
            self.assertIsNone(cli._artifact_identity(self.artifact))
        self.assertEqual(len(seen), 1)

    @unittest.skipUnless(
        hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_NONBLOCK") and hasattr(os, "mkfifo"),
        "requires safe artifact-open primitives",
    )
    def test_a_fifo_swapped_in_after_lstat_neither_blocks_nor_is_read(self) -> None:
        """A swapped FIFO is refused without waiting for a writer.

        The read-only open of a FIFO is the operation that blocks, so the flag
        that prevents it is required here, and the descriptor that comes back
        is a FIFO rather than the regular file ``lstat`` described.
        """

        self.artifact.write_bytes(b"artifact\n")
        info = os.lstat(self.artifact)
        self.artifact.unlink()
        os.mkfifo(self.artifact)

        seen: list[int] = []
        with self._stale_lstat(info), self._recording_open(
            seen, require=(os.O_NOFOLLOW, os.O_NONBLOCK)
        ):
            self.assertIsNone(cli._artifact_identity(self.artifact))
        self.assertEqual(len(seen), 1)

    def test_open_flags_this_build_lacks_fail_closed(self) -> None:
        """Without either safe flag the artifact is not opened at all.

        The fallback is the unchecked ``open`` this helper replaces — it would
        follow the swapped name — so an absent flag has to mean ``None``
        rather than an open with whatever is left.
        """

        self.artifact.write_bytes(b"artifact\n")
        for name in ("_ARTIFACT_NOFOLLOW_OPEN", "_ARTIFACT_NONBLOCK_OPEN"):
            with self.subTest(missing=name):
                with mock.patch.object(cli, name, None), mock.patch.object(
                    cli.os, "open",
                    side_effect=AssertionError("opened without the safe flags"),
                ):
                    self.assertIsNone(cli._artifact_identity(self.artifact))

    @unittest.skipUnless(
        hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_NONBLOCK"),
        "requires safe artifact-open primitives",
    )
    def test_the_descriptor_must_be_the_file_lstat_was_taken_of(self) -> None:
        """Same content is not the same file: the descriptor is identity-checked.

        The check is the device and inode ``lstat`` reported, so a name that
        now points at another file — even one holding the same bytes — is not
        what the caller judged and cannot be what it read.
        """

        self.artifact.write_bytes(b"artifact\n")
        other = self.directory / "same-bytes.png"
        other.write_bytes(b"artifact\n")

        self.assertIsNone(cli._open_artifact(self.artifact, os.lstat(other)))
        descriptor = cli._open_artifact(self.artifact, os.lstat(self.artifact))
        self.assertIsNotNone(descriptor)
        if descriptor is not None:
            os.close(descriptor)

    @unittest.skipUnless(
        hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_NONBLOCK"),
        "requires safe artifact-open primitives",
    )
    def test_the_descriptor_is_closed_when_the_checked_file_is_not_what_opened(
        self,
    ) -> None:
        """The refusal path releases the descriptor it opened.

        The close is recorded and the descriptor is then shown to be closed,
        so a leak — the one thing a fail-closed open path must not do — cannot
        pass as a mere ``None``.
        """

        self.artifact.write_bytes(b"artifact\n")
        other = self.directory / "other.png"
        other.write_bytes(b"other\n")
        closed: list[int] = []
        real_close = os.close

        def record(descriptor):
            closed.append(descriptor)
            return real_close(descriptor)

        with mock.patch.object(cli.os, "close", side_effect=record):
            self.assertIsNone(cli._open_artifact(self.artifact, os.lstat(other)))
        self.assertEqual(len(closed), 1)
        with self.assertRaises(OSError):
            os.fstat(closed[0])

    @unittest.skipUnless(
        hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_NONBLOCK"),
        "requires safe artifact-open primitives",
    )
    def test_an_unchanged_artifact_keeps_one_identity_and_a_rewrite_another(
        self,
    ) -> None:
        """The identity is the whole file, so unchanged is equal and changed is not.

        The report's own identity samples a prefix; this one does not, because
        the namespace admits files larger than that sample.
        """

        self.artifact.write_bytes(b"artifact\n")
        first = cli._artifact_identity(self.artifact)
        self.assertEqual(first, cli._artifact_identity(self.artifact))
        self.assertTrue(first.startswith("sha256:"))
        self.assertTrue(first.endswith(":size:9"))

        self.artifact.write_bytes(b"Artifact\n")
        self.assertNotEqual(first, cli._artifact_identity(self.artifact))
