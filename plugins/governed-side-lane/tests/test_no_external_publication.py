"""Task no-external-publication: the refusal, its seams, and its audit.

The defect this covers: a lane ran with the runner's ``--no-publish``, the
assignment said external publication was out of scope, and the worker pushed
its branch anyway. ``--no-publish`` governs exactly one thing — whether the
*runner* pushes a delivered branch — and the run recorded ``published: null``,
which a reader can take as "nothing was published".

The guard under test is separate from that option — and, because it refuses
external publication outright, it also suppresses the runner's own push, so a
caller does not have to remember ``--no-publish`` as well. These tests pin the
three things that make it more than a sentence in a prompt:

* the canonical document declares the refusal once, and the denied rules and
  the refused capability both derive from that declaration rather than from a
  list kept in Python;
* each host renders what it can actually do — a deny seam on Claude and Devin,
  instruction only on Codex — and says so instead of claiming containment;
* the run records the authority, what the host could enforce, and that the
  worker's own publication was not verified, beside the runner's own outcome
  rather than folded into it.

Everything here is offline: no host binary, no credential, no network.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from side_lane import cli, worktrees
from side_lane.adapters import claude, codex, devin
from side_lane import governance
from side_lane.governance import GovernanceError, lane_system_prompt
from side_lane.results import LaneResult
from side_lane.worktrees import LaneDelivery, VerifyResult


# The document the code actually reads, not a path this test guesses: the
# private and public checkouts lay the tree out differently.
GOVERNANCE = governance.GOVERNANCE_PATH
DECLARATION = "Publication refusal never grants these capabilities: `git-push`\n"
DENIED_RULES = ("Bash(git push)", "Bash(git push *)")


def normalized(text: str) -> str:
    """Collapse whitespace, so a reflowed canonical paragraph still matches."""

    return " ".join(text.split())


class PublicationDeclarationTests(unittest.TestCase):
    """The canonical document is the only owner of the refusal's contents."""

    def test_the_document_states_the_rule_and_its_seams(self) -> None:
        document = GOVERNANCE.read_text(encoding="utf-8")
        self.assertIn("## Publication refusal", document)
        self.assertIn(DECLARATION, document)
        self.assertIn("### no-external-publication (denied)", document)
        text = normalized(document)
        # The rule, the distinction from the runner's own option, and the
        # per-host limits are all in the document the worker is handed.
        self.assertIn("Make no external publication", text)
        self.assertIn("it is not the runner's `--no-publish`", text)
        self.assertIn("`git -C <path> push`", text)
        self.assertIn("not a sandbox", text)
        self.assertIn("Codex carries the instruction only", text)
        self.assertIn("Neither record is evidence that no publication happened", text)

    def test_the_denied_rules_are_a_reserved_bucket_not_a_capability(self) -> None:
        from side_lane.governance import (
            PUBLICATION_DENIED_HEADING,
            tool_policy,
        )
        policy = tool_policy()
        self.assertEqual(policy.no_publication_denied, DENIED_RULES)
        # Reserved: no grant unlocks them, so the heading never becomes a
        # capability a route could name.
        self.assertNotIn(PUBLICATION_DENIED_HEADING, policy.capabilities)
        self.assertNotIn("no-external-publication", policy.allowed)
        self.assertNotIn("no-external-publication", policy.denied)

    def test_the_refused_capabilities_derive_from_the_declaration(self) -> None:
        from side_lane.governance import (
            publication_refused_capabilities,
            publication_refusal_capability_conflicts,
        )
        base = GOVERNANCE.read_text(encoding="utf-8")
        self.assertEqual(publication_refused_capabilities(), ("git-push",))
        # A valid declaration naming something else proves derivation: the
        # refusal follows the document, not a code-side list.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.md"
            path.write_text(
                base.replace(
                    DECLARATION,
                    "Publication refusal never grants these capabilities: "
                    "`workflow-write`\n",
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                publication_refused_capabilities(path), ("workflow-write",)
            )
            self.assertEqual(
                publication_refusal_capability_conflicts(("git-push",), path), ()
            )

    def test_a_broken_declaration_fails_closed(self) -> None:
        from side_lane.governance import publication_refused_capabilities
        base = GOVERNANCE.read_text(encoding="utf-8")
        cases = (
            ("missing line", base.replace(DECLARATION, ""), "exactly one line"),
            ("duplicated line", base.replace(DECLARATION, DECLARATION + DECLARATION),
             "exactly one line"),
            ("no name", base.replace(
                DECLARATION, "Publication refusal never grants these capabilities:\n"),
             "declares no publication refusal capability"),
            ("unbackticked entry", base.replace(
                DECLARATION,
                "Publication refusal never grants these capabilities: git-push\n"),
             "malformed entry"),
            ("non-identifier name", base.replace(
                DECLARATION,
                "Publication refusal never grants these capabilities: `Git Push`\n"),
             "invalid capability name"),
            ("repeated name", base.replace(
                DECLARATION,
                "Publication refusal never grants these capabilities: "
                "`git-push`, `git-push`\n"),
             "duplicate capability name"),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.md"
            for label, text, pattern in cases:
                with self.subTest(declaration=label):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(GovernanceError, pattern):
                        publication_refused_capabilities(path)

    def test_the_denied_bucket_is_required_and_unique(self) -> None:
        from side_lane.governance import tool_policy
        base = GOVERNANCE.read_text(encoding="utf-8")
        heading = "### no-external-publication (denied)"
        bucket = heading + "\n\n- `Bash(git push)`\n- `Bash(git push *)`\n\n"
        self.assertIn(bucket, base)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.md"
            path.write_text(base.replace(bucket, ""), encoding="utf-8")
            with self.assertRaisesRegex(GovernanceError, "missing the `no-external"):
                tool_policy(path)
            path.write_text(base.replace(bucket, bucket + bucket), encoding="utf-8")
            with self.assertRaisesRegex(GovernanceError, "more than once"):
                tool_policy(path)

    def test_only_the_publication_grant_is_refused(self) -> None:
        from side_lane.governance import publication_refusal_capability_conflicts
        # Canonical order, whatever order the caller names them in.
        self.assertEqual(
            publication_refusal_capability_conflicts(
                ("workflow-write", "shell", "git-push", "workspace-write")),
            ("git-push",),
        )
        # Everything else stays: a separate workflow write, the artifact's own
        # write, and every read capability.
        for granted in ((), ("shell",), ("workspace-write",), ("workflow-write",),
                        ("gcloud-read", "slack-read", "gitnexus")):
            with self.subTest(granted=granted):
                self.assertEqual(
                    publication_refusal_capability_conflicts(granted), ()
                )


class PublicationContractTests(unittest.TestCase):
    """What the worker is told, and when the contract is refused."""

    def prompt(self, **kwargs) -> str:
        with tempfile.TemporaryDirectory() as directory:
            return lane_system_prompt("execute", Path(directory), **kwargs)

    def test_the_section_renders_only_for_a_publication_refusing_lane(self) -> None:
        self.assertIn("## Publication refusal", self.prompt(no_external_publication=True))
        self.assertNotIn("## Publication refusal", self.prompt())
        # Independent of the report contract: a report lane never publishes by
        # contract, while this states that the task forbids publication
        # whatever the lane's own deliverable is.
        self.assertNotIn("## Publication refusal", self.prompt(report_deliverable=True))
        both = self.prompt(report_deliverable=True, no_external_publication=True)
        self.assertIn("## Report deliverable", both)
        self.assertIn("## Publication refusal", both)
        # The refusal is read after the report contract it is distinct from.
        self.assertLess(
            both.index("## Report deliverable"), both.index("## Publication refusal")
        )

    def test_the_rendered_section_claims_no_more_than_it_has(self) -> None:
        text = normalized(self.prompt(no_external_publication=True))
        self.assertIn("Do not run `git push` in any spelling or form", text)
        self.assertIn("do not have another command, tool, or connected system do it", text)
        self.assertIn("command-string rules", text)
        self.assertIn("approval-boundary seam, not a sandbox", text)
        self.assertIn("no deny seam", text)
        self.assertIn("unverified", text)
        # The two authorities are named apart, so neither can stand for the other.
        self.assertIn("not the runner's `--no-publish`", text)
        self.assertNotIn("--no-external-publication is skipped", text)

    def test_review_mode_refuses_the_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(GovernanceError, "execute mode only"):
                lane_system_prompt(
                    "review", Path(directory), no_external_publication=True
                )


class ClaudePublicationTests(unittest.TestCase):
    native = {"gateway": "native-claude", "auth_method": "oauth", "billable": False}
    model_config = {"runtime_model": "claude-sonnet-5", "protocol": "native-claude"}

    def worktree(self, root: Path, name: str) -> Path:
        path = root / name
        path.mkdir()
        (path / ".git").write_text("gitdir: /example\n", encoding="utf-8")
        return path

    def test_the_denials_are_a_property_of_the_lane_not_a_grant(self) -> None:
        # Rendered even for a lane granted nothing, and never in review mode,
        # whose argv carries no permission rule this could narrow.
        self.assertEqual(
            claude.disallowed_tools("execute", (), no_external_publication=True),
            DENIED_RULES,
        )
        self.assertEqual(
            claude.disallowed_tools("review", (), no_external_publication=True), ()
        )
        self.assertEqual(claude.disallowed_tools("execute", ()), ())

    def test_the_denials_hold_whatever_the_allow_rules_grant(self) -> None:
        """Deny wins in Claude Code, so a broad grant cannot reopen the push.

        The `shell` and `workspace-write` classes carry `git push` spellings in
        the canonical allowlist, and a bare `Bash` grant (the local-developer
        class) would carry every spelling at once: the guard still emits the
        denials beside them.
        """
        for capabilities in ((), ("shell",), ("workspace-write",),
                             ("shell", "workspace-write", "playwright")):
            with self.subTest(capabilities=capabilities):
                self.assertEqual(
                    claude.disallowed_tools(
                        "execute", capabilities, no_external_publication=True
                    ),
                    DENIED_RULES,
                )

    def test_an_execute_lane_carries_the_denials_and_the_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = self.worktree(root, "repo"), self.worktree(root, "lane")
            argv = list(claude.build_command(
                executable="claude", repo=repo, worktree=lane, provider="claude",
                model="claude-sonnet-5", provider_config=self.native,
                model_config=self.model_config, prompt="Do it", mode="execute",
                capabilities=("shell",), no_external_publication=True,
            ))
        rendered = " ".join(argv)
        for rule in DENIED_RULES:
            self.assertIn(rule, rendered)
        # Paired with `--disallowedTools`, not merely appended somewhere.
        self.assertEqual(argv.count("--disallowedTools"), 2)
        self.assertIn("## Publication refusal", rendered)
        self.assertIn("Make no external publication", rendered)

    def test_an_ordinary_execute_lane_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = self.worktree(root, "repo"), self.worktree(root, "lane")
            argv = list(claude.build_command(
                executable="claude", repo=repo, worktree=lane, provider="claude",
                model="claude-sonnet-5", provider_config=self.native,
                model_config=self.model_config, prompt="Do it", mode="execute",
                capabilities=("shell",),
            ))
        self.assertNotIn("Bash(git push)", argv)
        self.assertNotIn("## Publication refusal", " ".join(argv))

    def test_a_dead_push_grant_is_refused_not_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = self.worktree(root, "repo"), self.worktree(root, "lane")
            with self.assertRaisesRegex(claude.ClaudeAdapterError, "git-push"):
                claude.build_command(
                    executable="claude", repo=repo, worktree=lane, provider="claude",
                    model="claude-sonnet-5", provider_config=self.native,
                    model_config=self.model_config, prompt="Do it", mode="execute",
                    capabilities=("shell", "git-push"),
                    no_external_publication=True,
                )
        with self.assertRaisesRegex(claude.ClaudeAdapterError, "git-push"):
            claude.reject_publication_capabilities(("git-push",), True)
        # Without the guard the same grant is ordinary.
        claude.reject_publication_capabilities(("git-push",), False)

    def test_the_guard_is_execute_only_on_the_adapter_too(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = self.worktree(root, "repo"), self.worktree(root, "lane")
            with self.assertRaisesRegex(claude.ClaudeAdapterError, "execute mode"):
                claude.build_command(
                    executable="claude", repo=repo, worktree=lane, provider="claude",
                    model="claude-sonnet-5", provider_config=self.native,
                    model_config={"runtime_model": "claude-sonnet-5",
                                  "protocol": "native-claude-readonly"},
                    prompt="Review it", mode="review", no_external_publication=True,
                )


class DevinPublicationTests(unittest.TestCase):
    model = "swe-2-medium"

    def test_the_native_deny_list_carries_the_canonical_denials(self) -> None:
        guard = devin._runtime_config(self.model, (), no_external_publication=True)
        plain = devin._runtime_config(self.model, ())
        self.assertIn("Exec(git push)", guard["permissions"]["deny"])
        self.assertNotIn("Exec(git push)", plain["permissions"]["deny"])
        # Additive: an inherited deny rule is preserved beside the new one, and
        # an ordinary lane's config is untouched.
        inherited = {"permissions": {"deny": ["Exec(git push --force)"]}}
        merged = devin._runtime_config(
            self.model, (), inherited, no_external_publication=True
        )
        self.assertIn("Exec(git push --force)", merged["permissions"]["deny"])
        self.assertIn("Exec(git push)", merged["permissions"]["deny"])

    def test_a_dead_push_grant_is_refused_on_this_host_too(self) -> None:
        with self.assertRaisesRegex(devin.DevinAdapterError, "git-push"):
            devin.reject_publication_capabilities(("git-push",), True)
        # Every other grant, and any lane without the guard, is untouched.
        self.assertIsNone(devin.reject_publication_capabilities(("shell",), True))
        self.assertIsNone(devin.reject_publication_capabilities(("git-push",), False))

    def test_launch_installs_both_controls(self) -> None:
        """The native deny list and the PreToolUse policy, from one declaration.

        The hook normalises `git -C <worktree>` before matching, which is the
        spelling the native `Exec(...)` prefix rule alone would miss.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = root / "repo", root / "lane"
            for path in (repo, lane):
                path.mkdir()
                (path / ".git").write_text("gitdir: /example\n", encoding="utf-8")
            # No host process runs: the per-run files are written before it.
            with mock.patch.object(devin, "_run", return_value=(0, "{}", "")), \
                 mock.patch.object(devin, "_load_user_config", return_value=None):
                devin.launch(
                    executable="/opt/hosts/devin", repo=repo, worktree=lane,
                    provider="devin", model=self.model,
                    provider_config={"gateway": "native-devin",
                                     "auth_method": "oauth", "billable": False},
                    model_config={
                        "runtime_model": self.model, "protocol": "native-devin",
                        "identity_contract": {
                            "requested_model": self.model,
                            "resolved_model": self.model,
                            "settings_precedence": "verified",
                        },
                        "qualification": {
                            "verified": True, "verified_on": "2026-09-11",
                            "source": "mocked local report",
                        },
                        "timeout_seconds": 600,
                    },
                    prompt="Do it", mode="execute", no_external_publication=True,
                )
            run_dirs = list((lane.parent / ".side-lane-runtime").glob("lane-*"))
            self.assertEqual(len(run_dirs), 1)
            policy = json.loads(
                (run_dirs[0] / "devin-command-policy.json").read_text(encoding="utf-8")
            )
            config_text = (run_dirs[0] / "devin-config.json").read_text(encoding="utf-8")
            config = json.loads(config_text)
        # Both controls carry the same declaration, each in its own host
        # grammar: the hook policy matches Bash rules (normalising `-C`), and
        # the native permission list takes Devin's `Exec(...)` prefix form.
        for rule in DENIED_RULES:
            self.assertIn(rule, policy["denied"])
        self.assertIn("Exec(git push)", config["permissions"]["deny"])
        # The policy hook is armed for the execute lane, and the worktree
        # anchors its `-C` normalisation.
        self.assertIn("devin_command_policy", config_text)
        self.assertEqual(policy["worktree"], str(lane))


class CodexPublicationTests(unittest.TestCase):
    provider = {"gateway": "native-codex", "auth_method": "oauth", "billable": False}
    model_config = {"runtime_model": "gpt-5.4-codex", "protocol": "native-codex"}

    def worktree(self, root: Path, name: str) -> Path:
        path = root / name
        path.mkdir()
        (path / ".git").write_text("gitdir: /example\n", encoding="utf-8")
        return path

    def test_the_instruction_is_carried_without_claiming_a_deny(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = self.worktree(root, "repo"), self.worktree(root, "lane")
            command = codex.build_codex_command(
                "codex", repo, lane, "openai", "gpt-5.4-codex", self.provider,
                self.model_config, "Do it", mode="execute",
                no_external_publication=True,
            )
        self.assertIn("## Publication refusal", command[-1])
        # Honest about this host: the sandbox mode is unchanged, no per-command
        # permission rule is emitted, and the refusal is instruction only —
        # which the runner records rather than dressing up as enforcement.
        self.assertIn("danger-full-access", command)
        self.assertNotIn("Bash(git push)", command)
        self.assertIn("Neither record is evidence that no publication happened",
                      normalized(command[-1]))

    def test_review_mode_refuses_the_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = self.worktree(root, "repo"), self.worktree(root, "lane")
            with self.assertRaisesRegex(codex.CodexAdapterError, "execute mode only"):
                codex.build_codex_command(
                    "codex", repo, lane, "openai", "gpt-5.4-codex", self.provider,
                    {"runtime_model": "gpt-5.4-codex",
                     "protocol": "native-codex-readonly"},
                    "Review it", mode="review", no_external_publication=True,
                )
            with self.assertRaisesRegex(codex.CodexAdapterError, "execute mode only"):
                codex.run_codex(
                    executable="codex", repo=repo, worktree=lane, provider="openai",
                    model="gpt-5.4-codex", provider_config=self.provider,
                    model_config={"runtime_model": "gpt-5.4-codex",
                                  "protocol": "native-codex-readonly"},
                    prompt="Review it", mode="review", no_external_publication=True,
                )

    def test_a_dead_push_grant_is_refused_before_a_lane_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = self.worktree(root, "repo"), self.worktree(root, "lane")
            with self.assertRaisesRegex(codex.CodexAdapterError, "git-push"):
                codex.run_codex(
                    executable="codex", repo=repo, worktree=lane, provider="openai",
                    model="gpt-5.4-codex", provider_config=self.provider,
                    model_config=self.model_config, prompt="Do it", mode="execute",
                    capabilities=("git-push",), no_external_publication=True,
                )


def init_repo(path: Path) -> Path:
    subprocess.run(
        ["git", "init", "-b", "main", str(path)], check=True, capture_output=True
    )
    return path


class PublicationAuditTests(unittest.TestCase):
    """The runner's own outcome and the task's authority are recorded apart."""

    model = "swe-2-medium"

    def repo(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = init_repo(Path(temporary.name).resolve() / "repo")
        (path / "CLAUDE.md").write_text("# Rules\n", encoding="utf-8")
        (path / "AGENTS.md").write_text(
            "You must read [CLAUDE.md](./CLAUDE.md); it is the authoritative "
            "source of truth.\n",
            encoding="utf-8",
        )
        return path

    def config(self) -> dict:
        config = cli.load_config()
        config["providers"]["devin"] = {
            "gateway": "native-devin", "auth_method": "oauth", "billable": False,
            "routes": {"execute": {"devin": {
                "protocol": "native-devin", "models": [self.model],
                "model_configs": {self.model: {
                    "identity_contract": {
                        "requested_model": self.model,
                        "resolved_model": self.model,
                        "settings_precedence": "verified",
                    },
                    "qualification": {
                        "verified": True, "verified_on": "2026-09-11",
                        "source": "mocked local report",
                    },
                    "timeout_seconds": 600,
                }},
            }}},
        }
        return config

    def run_lane(self, args, repo: Path, publish: mock.Mock | None = None,
                 worker_exit: int = 0,
                 delivery_error: "Exception | None" = None,
                 verify: mock.Mock | None = None) -> tuple[dict, mock.Mock]:
        """Run one lane end to end with the host and the worktree stubbed.

        ``publish`` lets a caller hold the runner's push function so a test can
        assert it was never reached; omitted, a fresh stub is used.
        ``worker_exit`` is the provider's own exit status — a worker can leave
        a committed, clean branch and still exit nonzero. ``delivery_error``
        makes the delivery inspection itself fail, which stops the run before
        any push decision. ``verify`` lets a caller hold the ``--verify``
        stub, so one mock both stands in for the command and carries the
        afterwards question of whether the runner reached it; omitted, a fresh
        passing stub is used.
        """
        worktree = repo.parent / "devin-worktree"
        lane = mock.Mock(worktree=worktree, branch="side-lane/publication")
        result = LaneResult(
            ("devin",), worker_exit, worktree, "devin", "devin", "native-devin",
            self.model, "oauth", False, "{}", "", requested_model=self.model,
            resolved_model=self.model,
        )
        delivery = mock.Mock(
            return_value=LaneDelivery(committed=True, uncommitted=())
        )
        if delivery_error is not None:
            delivery.side_effect = delivery_error
        if verify is None:
            verify = mock.Mock(return_value=VerifyResult("true", 0, ""))
        stdout = io.StringIO()
        with (
            mock.patch("side_lane.cli._require_host_executable",
                       return_value="/opt/hosts/devin"),
            mock.patch("side_lane.cli.create_worktree", return_value=lane),
            mock.patch("side_lane.cli.lane_delivery", delivery),
            mock.patch("side_lane.cli.require_native_oauth"),
            mock.patch("side_lane.cli.verify_lane", verify),
            mock.patch("side_lane.adapters.devin.launch", return_value=result),
            mock.patch("side_lane.cli.git_status", return_value="## lane"),
            mock.patch("side_lane.cli.write_audit",
                       return_value=repo / ".git/audit.json") as audit,
            mock.patch(
                "side_lane.cli.publish_lane_branch",
                publish if publish is not None else mock.Mock(return_value="ref"),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            expected = worker_exit
            if delivery_error is not None:
                # Fail closed: an unverifiable tree is its own exit status,
                # not the worker's.
                expected = worker_exit or cli.LANE_DELIVERY_UNVERIFIED
            self.assertEqual(
                cli._launch(args, self.config(), repo, "Do it"), expected
            )
        # The summary is the last JSON object printed; the worker's own stdout
        # is echoed before it, so start at its opening brace.
        lines = stdout.getvalue().splitlines()
        start = next(
            index for index, line in enumerate(lines) if line.rstrip() == "{"
        )
        return json.loads("\n".join(lines[start:])), audit

    def args(self, **overrides) -> mock.Mock:
        base = dict(
            host="devin", mode="execute", provider="devin", model=self.model,
            capability=[], lane_name="publication", skill=[],
            approve_billable_route=False, worktree_root=None, verify=None,
            no_publish=False, no_external_publication=False,
            report_only=False, report_deliverable=False,
        )
        base.update(overrides)
        return mock.Mock(**base)

    def test_the_record_separates_authority_host_control_and_verification(self) -> None:
        repo = self.repo()
        summary, audit = self.run_lane(
            self.args(no_external_publication=True, no_publish=True), repo
        )
        expected = {
            "task_authority": "--no-external-publication",
            "host_enforcement": cli.PUBLICATION_ENFORCEMENT_BY_HOST["devin"],
            "worker_publication_verified": "not-checked",
        }
        self.assertEqual(summary["publication"], {
            **expected, "runner": "skipped", "runner_skip_reason": "--no-publish",
        })
        # The same record reaches the audit, so a reader of either sees it.
        self.assertEqual(audit.call_args.kwargs["publication"], expected)

    def test_the_runner_skip_alone_is_not_a_publication_refusal(self) -> None:
        """`--no-publish` says nothing about the worker, and is recorded as such.

        This is the defect's second half: a run whose runner skipped its push
        must not read as a run where nothing could have been published.
        """
        repo = self.repo()
        summary, audit = self.run_lane(self.args(no_publish=True), repo)
        self.assertIsNone(summary["publication"]["task_authority"])
        self.assertIsNone(summary["publication"]["host_enforcement"])
        self.assertEqual(summary["publication"]["worker_publication_verified"],
                         "not-checked")
        self.assertEqual(summary["publication"]["runner"], "skipped")
        self.assertEqual(summary["publication"]["runner_skip_reason"], "--no-publish")
        self.assertIsNone(summary["published"])
        # The audit still records the block, naming no authority rather than
        # omitting the question: a reader of an ordinary lane's audit sees that
        # no guard was carried, not a field they must interpret as absence.
        self.assertEqual(audit.call_args.kwargs["publication"], {
            "task_authority": None,
            "host_enforcement": None,
            "worker_publication_verified": "not-checked",
        })

    def test_the_guard_alone_never_lets_the_runner_push(self) -> None:
        """The guard suppresses the runner's own push, without `--no-publish`.

        The contract correction: the guard is the task's refusal of external
        publication, and the runner's own push of a delivered branch is one of
        those publications. A caller must not have to remember a second option,
        so the push function must never be reached with the guard alone.
        """
        repo = self.repo()
        publish = mock.Mock(return_value="ref")
        summary, _ = self.run_lane(
            self.args(no_external_publication=True), repo, publish
        )
        publish.assert_not_called()
        self.assertIsNone(summary["published"])
        self.assertNotIn("published_ref", summary)
        self.assertEqual(summary["publication"]["runner"], "skipped")
        self.assertEqual(summary["publication"]["runner_skip_reason"],
                         "--no-external-publication")
        # The skip is attributed to the guard, never to the option nobody gave.
        self.assertEqual(summary["publication"]["task_authority"],
                         "--no-external-publication")

    def test_the_runner_only_option_keeps_its_own_reason(self) -> None:
        """`--no-publish` still skips the push and is recorded as itself."""
        repo = self.repo()
        publish = mock.Mock(return_value="ref")
        summary, _ = self.run_lane(self.args(no_publish=True), repo, publish)
        publish.assert_not_called()
        self.assertEqual(summary["publication"]["runner"], "skipped")
        self.assertEqual(summary["publication"]["runner_skip_reason"], "--no-publish")
        self.assertIsNone(summary["publication"]["task_authority"])

    def test_an_ordinary_lane_still_records_what_the_runner_did(self) -> None:
        repo = self.repo()
        publish = mock.Mock(return_value="ref")
        summary, _ = self.run_lane(self.args(), repo, publish)
        publish.assert_called_once()
        self.assertIs(summary["published"], True)
        self.assertEqual(summary["publication"]["runner"], "published")
        self.assertIsNone(summary["publication"]["runner_skip_reason"])
        self.assertIsNone(summary["publication"]["task_authority"])

    def test_a_failed_worker_skips_the_push_without_denying_the_branch(self):
        """A nonzero worker exit can still leave a committed, clean branch.

        `delivered` is the lane tree's own commit-plus-clean answer and does not
        depend on the worker's exit code. Recording the runner as
        "not-delivered" on that outcome contradicts the delivery record beside
        it and hides why the runner skipped its own push — while the branch
        still must not be published automatically for a failed run.
        """
        repo = self.repo()
        publish = mock.Mock(return_value="ref")
        summary, _ = self.run_lane(self.args(), repo, publish, worker_exit=3)
        publish.assert_not_called()
        self.assertIs(summary["delivered"], True)
        self.assertEqual(summary["publication"]["runner"], "skipped")
        self.assertEqual(summary["publication"]["runner_skip_reason"],
                         "provider-failed")

    def test_an_uninspectable_tree_still_records_the_authority_it_carried(self):
        """An inspection failure must not erase the publication record.

        This run stopped before the push decision, so it has no runner
        publication outcome — but the authority it carried, and the fact that
        no publication by anyone was verified, are known before the tree is
        ever asked about. Omitting the record here left an explicit
        ``--no-external-publication`` run's summary indistinguishable from one
        that carried no guard: exactly the confusion this record exists to
        prevent. Neither verdict is claimed, and the record says only what the
        runner did — it skipped, because the inspection failed.
        """
        repo = self.repo()
        publish = mock.Mock(return_value="ref")
        summary, _ = self.run_lane(
            self.args(no_external_publication=True), repo, publish,
            delivery_error=worktrees.WorktreeError("git status failed"),
        )
        # The guard still suppressed the runner's push, and nothing claimed a
        # delivery or a verification that never happened.
        publish.assert_not_called()
        self.assertIsNone(summary["delivered"])
        self.assertIsNone(summary["verified"])
        self.assertEqual(summary["delivery_unverified"], "git status failed")
        self.assertEqual(summary["publication"], {
            "task_authority": "--no-external-publication",
            "host_enforcement": cli.PUBLICATION_ENFORCEMENT_BY_HOST["devin"],
            "worker_publication_verified": "not-checked",
            "runner": "skipped",
            "runner_skip_reason": "delivery-unverified",
        })

    def test_an_uninspectable_tree_without_the_guard_records_it_too(self) -> None:
        """The failure path is not only for guard-carrying runs.

        An ordinary execute lane whose tree cannot be inspected carries no
        authority, and the record must say that rather than be absent — the
        same contract the normal path keeps.
        """
        repo = self.repo()
        summary, _ = self.run_lane(
            self.args(), repo,
            delivery_error=worktrees.WorktreeError("git status failed"),
        )
        self.assertEqual(summary["publication"], {
            "task_authority": None,
            "host_enforcement": None,
            "worker_publication_verified": "not-checked",
            "runner": "skipped",
            "runner_skip_reason": "delivery-unverified",
        })

    def test_review_mode_refuses_the_guard(self) -> None:
        repo = self.repo()
        # The claude route exists in review mode, so the run reaches the guard
        # rather than failing earlier on an unsupported route.
        args = self.args(
            host="claude", mode="review", provider="claude",
            model="claude-sonnet-5", no_external_publication=True,
        )
        with self.assertRaisesRegex(cli.SideLaneError, "execute mode"):
            cli._launch(args, cli.load_config(), repo, "Review it")

    def test_a_dead_push_grant_is_refused_before_the_worktree(self) -> None:
        repo = self.repo()
        with mock.patch("side_lane.cli.create_worktree") as create:
            with self.assertRaisesRegex(cli.SideLaneError, "git-push"):
                cli._launch(
                    self.args(capability=["git-push"], no_external_publication=True),
                    self.config(), repo, "Do it",
                )
        create.assert_not_called()

    def test_the_parser_default_is_no_guard(self) -> None:
        parser = cli.make_parser()
        base = [
            "run", "--host", "devin", "--mode", "execute", "--provider", "devin",
            "--model", self.model, "--repo", "/tmp/repo",
            "--lane-name", "publication", "--prompt", "Do it",
        ]
        self.assertIs(parser.parse_args(base).no_external_publication, False)
        self.assertIs(parser.parse_args(base).no_publish, False)
        self.assertIs(
            parser.parse_args(base + ["--no-external-publication"])
            .no_external_publication,
            True,
        )

    def test_the_flag_is_documented_apart_from_the_runner_option(self) -> None:
        parser = cli.make_parser()
        run = next(
            action.choices["run"]
            for action in parser._actions
            if getattr(action, "choices", None) and "run" in action.choices
        )
        helps = {
            action.dest: action.help
            for action in run._actions
            if action.help and action.dest in {"no_publish", "no_external_publication"}
        }
        self.assertEqual(set(helps), {"no_publish", "no_external_publication"})
        self.assertIn("does not forbid the worker to push the branch itself",
                      helps["no_publish"])
        guard = normalized(helps["no_external_publication"])
        self.assertIn("not --no-publish", guard)
        self.assertIn("not a sandbox", guard)
        self.assertIn("git -C <path>", guard)
        self.assertIn("execute lanes only", guard.lower())

    def test_the_verify_help_names_the_guard_refusal(self) -> None:
        """The refusal is discoverable from `--help`, not only from the source."""
        parser = cli.make_parser()
        run = next(
            action.choices["run"]
            for action in parser._actions
            if getattr(action, "choices", None) and "run" in action.choices
        )
        helps = {
            action.dest: normalized(action.help)
            for action in run._actions
            if action.help and action.dest in {"verify", "no_external_publication"}
        }
        self.assertEqual(set(helps), {"verify", "no_external_publication"})
        self.assertIn("--no-external-publication", helps["verify"])
        self.assertIn("--no-publish is unaffected", helps["verify"])
        self.assertIn("refuses --verify", helps["no_external_publication"])

    def test_verify_is_refused_with_the_guard_before_anything_runs(self) -> None:
        """The caller's command runs before this runner's own push decision.

        Copilot on PR172: `--verify` is invoked before `--no-external-
        publication` is consulted at all, so `--verify "git push origin HEAD"`
        published *first* and the run then recorded `runner: skipped` — the
        summary claiming the opposite of what had happened. Filtering arbitrary
        shell is not on offer and is not what the guard ever promised, so the
        combination is refused outright, before the worker, the worktree, or
        the command itself exists.
        """
        repo = self.repo()
        verify = mock.Mock(return_value=VerifyResult("git push origin HEAD", 0, ""))
        launch = mock.Mock()
        with (
            mock.patch("side_lane.cli.create_worktree") as create,
            mock.patch("side_lane.cli.verify_lane", verify),
            mock.patch("side_lane.adapters.devin.launch", launch),
        ):
            with self.assertRaises(cli.SideLaneError) as caught:
                cli._launch(
                    self.args(
                        no_external_publication=True,
                        verify="git push origin HEAD",
                    ),
                    self.config(), repo, "Do it",
                )
        message = str(caught.exception)
        self.assertIn("--verify", message)
        self.assertIn("--no-external-publication", message)
        # Refused before dispatch, and before the command could have any side
        # effect at all: no worktree, no worker, no verification.
        create.assert_not_called()
        launch.assert_not_called()
        verify.assert_not_called()

    def test_an_ordinary_lane_still_runs_its_verification(self) -> None:
        """The refusal is scoped to the guard, not to `--verify` itself."""
        repo = self.repo()
        verify = mock.Mock(return_value=VerifyResult("pytest -q", 0, "3 passed"))
        summary, _ = self.run_lane(
            self.args(verify="pytest -q"), repo, verify=verify
        )
        verify.assert_called_once()
        self.assertEqual(verify.call_args.args[1], "pytest -q")
        self.assertIs(summary["verified"], True)
        self.assertEqual(summary["verify_command"], "pytest -q")
        self.assertEqual(summary["verify_exit"], 0)

    def test_no_publish_keeps_its_verify_combination(self) -> None:
        """Legacy behaviour is untouched: `--no-publish` never spoke for the
        worker, so it stays compatible with a caller verification command."""
        repo = self.repo()
        verify = mock.Mock(return_value=VerifyResult("pytest -q", 0, "3 passed"))
        publish = mock.Mock(return_value="ref")
        summary, _ = self.run_lane(
            self.args(no_publish=True, verify="pytest -q"), repo, publish,
            verify=verify,
        )
        verify.assert_called_once()
        publish.assert_not_called()
        self.assertIs(summary["verified"], True)
        self.assertEqual(summary["publication"]["runner"], "skipped")
        self.assertEqual(summary["publication"]["runner_skip_reason"],
                         "--no-publish")


class PublicationAuditFieldTests(unittest.TestCase):
    """The audit schema is additive: version 2, one new optional field."""

    def test_write_audit_records_the_publication_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = init_repo(root / "repo")
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "seed.txt"], check=True,
                           capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.email=t@example.com",
                 "-c", "user.name=T", "commit", "-m", "seed"],
                check=True, capture_output=True,
            )
            lane = worktrees.create_worktree(repo, "publication")
            path = worktrees.write_audit(
                lane, host="devin", mode="execute", provider="devin",
                model="swe-2-medium", prompt="Do it", exit_status=0,
                status="## lane",
                publication={
                    "task_authority": "--no-external-publication",
                    "host_enforcement": "native deny list",
                    "worker_publication_verified": "not-checked",
                },
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["publication"]["task_authority"],
                         "--no-external-publication")

    def test_a_run_without_the_guard_records_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = init_repo(root / "repo")
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "seed.txt"], check=True,
                           capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.email=t@example.com",
                 "-c", "user.name=T", "commit", "-m", "seed"],
                check=True, capture_output=True,
            )
            lane = worktrees.create_worktree(repo, "plain")
            path = worktrees.write_audit(
                lane, host="devin", mode="execute", provider="devin",
                model="swe-2-medium", prompt="Do it", exit_status=0,
                status="## lane",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsNone(payload["publication"])


if __name__ == "__main__":
    unittest.main()