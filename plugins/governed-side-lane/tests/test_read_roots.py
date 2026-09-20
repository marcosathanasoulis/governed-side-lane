"""Coordinator-granted read roots: validation, rendering, audit, no widening.

The property under test is asymmetric on purpose. A read root must *work* —
a worker whose repository instructions point at a directory outside its
worktree has to be able to read it — and it must change nothing else: no
implicit read of any other directory, no broad read wildcard, and no write
grant anywhere except the lane worktree.
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

from side_lane import cli, read_roots, worktrees
from side_lane.adapters import claude, codex, devin
from side_lane.results import LaneResult
from side_lane.worktrees import LaneDelivery


def rule_prefix(rule: str) -> str:
    """The directory an exact ``Read(<dir>/**)`` rule covers."""

    if not (rule.startswith("Read(") and rule.endswith("/**)")):
        raise AssertionError(f"not an exact read rule: {rule!r}")
    return rule[len("Read("):-len("/**)")]


def init_repo(path: Path) -> Path:
    subprocess.run(
        ["git", "init", "-b", "main", str(path)], check=True, capture_output=True
    )
    return path


class ReadRootParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Resolved: `parse_read_roots` canonicalizes, and on macOS the temp
        # directory itself sits behind a symlink (/var -> /private/var).
        self.root = Path(temporary.name).resolve()
        self.shared = self.root / "shared instructions"
        self.shared.mkdir()

    def test_no_request_is_an_empty_grant(self) -> None:
        self.assertEqual(read_roots.parse_read_roots(None), ())
        self.assertEqual(read_roots.parse_read_roots([]), ())

    def test_existing_directory_is_canonicalized_and_sorted(self) -> None:
        second = self.root / "another"
        second.mkdir()
        link = self.root / "link-to-shared"
        link.symlink_to(self.shared)
        parsed = read_roots.parse_read_roots([str(link), str(second)])
        self.assertEqual(parsed, (second, self.shared))
        self.assertTrue(all(root.is_absolute() for root in parsed))

    def test_duplicates_collapse(self) -> None:
        link = self.root / "link-to-shared"
        link.symlink_to(self.shared)
        parsed = read_roots.parse_read_roots([str(self.shared), str(link)])
        self.assertEqual(parsed, (self.shared,))

    def test_relative_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(read_roots.ReadRootError, "absolute"):
            read_roots.parse_read_roots(["shared/instructions"])

    def test_missing_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(read_roots.ReadRootError, "existing directory"):
            read_roots.parse_read_roots([str(self.root / "nope")])

    def test_file_is_not_a_read_root(self) -> None:
        target = self.root / "file.txt"
        target.write_text("not a directory\n", encoding="utf-8")
        with self.assertRaisesRegex(read_roots.ReadRootError, "not a directory"):
            read_roots.parse_read_roots([str(target)])

    def test_filesystem_root_is_rejected(self) -> None:
        with self.assertRaisesRegex(read_roots.ReadRootError, "filesystem-wide"):
            read_roots.parse_read_roots([str(Path(self.root.anchor))])

    def test_glob_syntax_and_rule_delimiters_are_rejected(self) -> None:
        for character in ("*", "?", "[", "]", "{", "}", "(", ")"):
            with self.subTest(character=character):
                with self.assertRaisesRegex(
                    read_roots.ReadRootError, "permission rule cannot carry"
                ):
                    read_roots.parse_read_roots([str(self.root / f"a{character}b")])

    def test_control_characters_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            read_roots.ReadRootError, "permission rule cannot carry"
        ):
            read_roots.parse_read_roots([f"{self.root}/a\nb"])

    def test_a_symlink_target_carrying_glob_syntax_is_rejected(self) -> None:
        # A literal `*` is legal in a POSIX filename, so the string the
        # coordinator typed can pass while the canonical path it resolves to
        # cannot be rendered as a rule.
        wildcard = self.root / "a*b"
        wildcard.mkdir()
        link = self.root / "innocent"
        link.symlink_to(wildcard)
        with self.assertRaisesRegex(
            read_roots.ReadRootError, "permission rule cannot carry"
        ):
            read_roots.parse_read_roots([str(link)])

    def test_non_string_and_empty_entries_are_rejected(self) -> None:
        for value in ([1], [""], ["   "], "shared"):
            with self.subTest(value=value):
                with self.assertRaises(read_roots.ReadRootError):
                    read_roots.parse_read_roots(value)

    def test_render_gate_rejects_what_it_cannot_express(self) -> None:
        self.assertEqual(read_roots.read_rule(self.shared), f"Read({self.shared}/**)")
        for value in ("shared", "/", f"{self.root}/a*b", f"{self.root}/a)b"):
            with self.subTest(value=value):
                with self.assertRaises(read_roots.ReadRootError):
                    read_roots.read_rule(Path(value))

    def test_scope_note_is_empty_without_roots_and_names_each_root(self) -> None:
        self.assertEqual(read_roots.scope_note(()), "")
        note = read_roots.scope_note((self.shared,))
        self.assertIn(str(self.shared), note)
        self.assertIn("read-only", note)
        self.assertIn("lane worktree", note)


class DevinReadScopeTests(unittest.TestCase):
    def allow(self, worktree, read_roots_value=()):
        return devin._runtime_config(
            "swe-2-medium", (), worktree=worktree, read_roots=read_roots_value
        )["permissions"]["allow"]

    def test_requested_root_is_granted_read_only(self) -> None:
        allow = self.allow(Path("/lane"), (Path("/shared"),))
        self.assertIn("Read(/lane/**)", allow)
        self.assertIn("Read(/shared/**)", allow)

    def test_unrequested_path_has_no_rule_at_all(self) -> None:
        allow = self.allow(Path("/lane"), (Path("/shared"),))
        granted = {rule_prefix(rule) for rule in allow if rule.startswith("Read(")}
        self.assertEqual(granted, {"/lane", "/shared"})
        self.assertFalse(any("/other" in rule for rule in allow))

    def test_no_broad_read_wildcard_is_emitted(self) -> None:
        for read_roots_value in ((), (Path("/shared"),)):
            with self.subTest(read_roots=read_roots_value):
                allow = self.allow(Path("/lane"), read_roots_value)
                self.assertNotIn("Read(**)", allow)
                for rule in allow:
                    if rule.startswith("Read("):
                        # Each read rule names one exact directory, so the tree
                        # glob over that directory is the entire grant.
                        self.assertEqual(rule, f"Read({rule_prefix(rule)}/**)")

    def test_writes_stay_inside_the_lane_worktree(self) -> None:
        allow = devin._runtime_config(
            "swe-2-medium", ("shell",), worktree=Path("/lane"),
            read_roots=(Path("/shared"),),
        )["permissions"]["allow"]
        self.assertEqual(
            [rule for rule in allow if rule.startswith("Write(")],
            ["Write(/lane/**)"],
        )
        self.assertNotIn("Write(**)", allow)
        self.assertNotIn("Write(/shared/**)", allow)

    def test_a_root_equal_to_the_worktree_is_not_duplicated(self) -> None:
        allow = self.allow(Path("/lane"), (Path("/lane"),))
        self.assertEqual(allow.count("Read(/lane/**)"), 1)

    def test_a_path_with_spaces_renders_literally(self) -> None:
        allow = self.allow(Path("/my lanes/lane"), (Path("/shared instructions"),))
        self.assertIn("Read(/shared instructions/**)", allow)
        self.assertIn("Write(/my lanes/lane/**)", allow)

    def test_an_unrenderable_root_fails_the_config_closed(self) -> None:
        for value in (Path("/a*b"), Path("/a)b"), Path("relative"), Path("/")):
            with self.subTest(value=value):
                with self.assertRaises(read_roots.ReadRootError):
                    self.allow(Path("/lane"), (value,))

    def test_without_a_worktree_no_file_tool_rule_is_granted(self) -> None:
        allow = devin._runtime_config(
            "swe-2-medium", (), read_roots=(Path("/shared"),)
        )["permissions"]["allow"]
        self.assertFalse(any(rule.startswith(("Read(", "Write(")) for rule in allow))


class DevinLaunchScopeTests(unittest.TestCase):
    """Real launches: the grant reaches the config and the instructions."""

    def route(self, model: str = "swe-2-medium"):
        provider = {"gateway": "native-devin", "auth_method": "oauth", "billable": False}
        route = {
            "runtime_model": model,
            "protocol": "native-devin",
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
        return provider, route

    def launch(self, root: Path, shared: Path | None) -> dict:
        repo = init_repo(root / "repo")
        lane = init_repo(root / "lane")
        provider, route = self.route()
        captured: dict = {}

        def popen(command, **kwargs):
            captured["config"] = json.loads(
                Path(command[command.index("--config") + 1]).read_text(encoding="utf-8")
            )
            captured["prompt"] = command[-1]
            captured["lane"] = lane
            Path(command[command.index("--export") + 1]).write_text(
                json.dumps({"steps": [{"model_name": "swe-2-medium"}]}),
                encoding="utf-8",
            )
            process = mock.Mock(pid=41, returncode=0)
            process.communicate.return_value = ("done", "")
            return process

        devin.launch(
            executable="devin", repo=repo, worktree=lane, provider="devin",
            model="swe-2-medium", provider_config=provider, model_config=route,
            prompt="Implement it", popen=popen,
            user_config_path=root / "user-config.json",
            read_roots=() if shared is None else (shared,),
        )
        return captured

    def test_the_grant_reaches_config_and_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            shared = root / "shared-instructions"
            shared.mkdir()
            captured = self.launch(root, shared)
            lane = captured["lane"]
            allow = captured["config"]["permissions"]["allow"]
            self.assertIn(f"Read({shared}/**)", allow)
            self.assertIn(f"Read({lane}/**)", allow)
            self.assertEqual(
                [rule for rule in allow if rule.startswith("Write(")],
                [f"Write({lane}/**)"],
            )
            self.assertIn(str(shared), captured["prompt"])
            self.assertIn("# Approved task", captured["prompt"])

    def test_the_hosts_own_config_is_read_but_never_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            shared = root / "shared-instructions"
            shared.mkdir()
            # JSONC, an inherited deny and ask rule, and a trailing comma: the
            # shape the adapter must parse and carry forward without editing.
            original = (
                '{\n  // the host\'s own file\n'
                '  "version": 1,\n'
                '  "permissions": {"allow": [], "deny": ["Exec(rm)"], '
                '"ask": ["Exec(curl)"]},\n'
                "}\n"
            )
            (root / "user-config.json").write_text(original, encoding="utf-8")
            captured = self.launch(root, shared)
            permissions = captured["config"]["permissions"]
            self.assertEqual(permissions["deny"], ["Exec(rm)"])
            self.assertEqual(permissions["ask"], ["Exec(curl)"])
            self.assertEqual(
                (root / "user-config.json").read_text(encoding="utf-8"), original
            )

    def test_a_lane_without_a_root_grants_no_extra_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            captured = self.launch(root, None)
            reads = [
                rule for rule in captured["config"]["permissions"]["allow"]
                if rule.startswith("Read(")
            ]
            self.assertEqual(reads, [f"Read({captured['lane']}/**)"])
            self.assertNotIn("read-only scope", captured["prompt"])


class OtherHostScopeTests(unittest.TestCase):
    """The other adapters take the same interface without gaining a write."""

    native = {"gateway": "native-claude", "auth_method": "oauth", "billable": False}

    def worktree(self, root: Path, name: str) -> Path:
        path = root / name
        path.mkdir()
        (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
        return path

    def test_claude_execute_names_the_root_and_adds_no_directory_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = self.worktree(root, "repo"), self.worktree(root, "lane")
            command = claude.build_command(
                executable="claude", repo=repo, worktree=lane, provider="claude",
                model="claude-sonnet-5", provider_config=self.native,
                model_config={
                    "runtime_model": "claude-sonnet-5", "protocol": "native-claude"
                },
                prompt="task", read_roots=(root / "shared",),
            )
        # The Claude host exposes no read-only extra-directory control; its
        # only extra-directory flag is a workspace (write) grant, so it is not
        # emitted, and the grant is named in the worker's instructions instead.
        self.assertNotIn("--add-dir", command)
        self.assertIn(
            str(root / "shared"),
            command[command.index("--append-system-prompt") + 1],
        )

    def test_claude_review_refuses_read_roots_rather_than_widening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = self.worktree(root, "repo"), self.worktree(root, "lane")
            with self.assertRaisesRegex(claude.ClaudeAdapterError, "execute-only"):
                claude.build_command(
                    executable="claude", repo=repo, worktree=lane, provider="claude",
                    model="claude-sonnet-5", provider_config=self.native,
                    model_config={
                        "runtime_model": "claude-sonnet-5",
                        "protocol": "native-claude-readonly",
                    },
                    prompt="review", mode="review", read_roots=(root / "shared",),
                )

    def test_codex_execute_names_the_root_and_adds_no_directory_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = self.worktree(root, "repo"), self.worktree(root, "lane")
            command = codex.build_codex_command(
                "codex", repo, lane, "openai", "gpt-5.4-codex",
                {"gateway": "native-codex", "auth_method": "oauth", "billable": False},
                {"runtime_model": "gpt-5.4-codex", "protocol": "native-codex"},
                "task", read_roots=(root / "shared",),
            )
        # Codex's only directory control is a write grant (`--add-dir` /
        # `sandbox_workspace_write.writable_roots`), so it is not emitted.
        self.assertNotIn("--add-dir", command)
        self.assertIn(str(root / "shared"), command[-1])

    def test_codex_review_refuses_read_roots_rather_than_widening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = self.worktree(root, "repo"), self.worktree(root, "lane")
            with self.assertRaisesRegex(codex.CodexAdapterError, "execute-only"):
                codex.build_codex_command(
                    "codex", repo, lane, "openai", "gpt-5.4-codex",
                    {"gateway": "native-codex", "auth_method": "oauth",
                     "billable": False},
                    {"runtime_model": "gpt-5.4-codex",
                     "protocol": "native-codex-readonly"},
                    "review", mode="review", read_roots=(root / "shared",),
                )


class ReadRootCliTests(unittest.TestCase):
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

    def devin_config(self, model: str = "swe-2-medium") -> dict:
        config = cli.load_config()
        config["providers"]["devin"] = {
            "gateway": "native-devin", "auth_method": "oauth", "billable": False,
            "routes": {"execute": {"devin": {
                "protocol": "native-devin", "models": [model],
                "model_configs": {model: {
                    "identity_contract": {
                        "requested_model": model, "resolved_model": model,
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

    def test_cli_rejects_an_unsafe_read_root_before_launching(self) -> None:
        repo = self.repo()
        for value, message in (
            ("relative/path", "absolute"),
            (str(repo / "missing"), "existing directory"),
            (str(Path(repo.anchor)), "filesystem-wide"),
            (str(repo / "a*b"), "permission rule cannot carry"),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(read_roots.ReadRootError, message):
                    cli.run([
                        "run", "--host", "devin", "--mode", "execute",
                        "--provider", "devin", "--model", "swe-2-medium",
                        "--repo", str(repo), "--lane-name", "reads",
                        "--prompt", "Implement it", "--read-root", value,
                    ])

    def test_read_root_flows_to_the_adapter_and_the_audit(self) -> None:
        config = self.devin_config()
        model = "swe-2-medium"
        repo = self.repo()
        shared = repo.parent / "shared-instructions"
        shared.mkdir()
        worktree = repo.parent / "devin-worktree"
        lane = mock.Mock(worktree=worktree, branch="side-lane/devin-reads")
        result = LaneResult(
            ("devin",), 0, worktree, "devin", "devin", "native-devin", model,
            "oauth", False, '{"model":"swe-2-medium"}', "",
            requested_model=model, resolved_model=model,
        )
        args = mock.Mock(
            host="devin", mode="execute", provider="devin", model=model,
            capability=[], lane_name="devin-reads", skill=[], approve_billable_route=False,
            worktree_root=None, verify=None,
        )
        with (
            mock.patch("side_lane.cli._require_host_executable",
                       return_value="/opt/hosts/devin"),
            mock.patch("side_lane.cli.create_worktree", return_value=lane),
            mock.patch("side_lane.cli.lane_delivery",
                       return_value=LaneDelivery(committed=True, uncommitted=())),
            mock.patch("side_lane.cli.require_native_oauth"),
            mock.patch("side_lane.adapters.devin.launch",
                       return_value=result) as launch,
            mock.patch("side_lane.cli.git_status", return_value="## lane"),
            mock.patch("side_lane.cli.write_audit",
                       return_value=repo / ".git/audit.json") as audit,
            mock.patch("side_lane.cli.publish_lane_branch", return_value="ref"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                cli._launch(args, config, repo, "Implement it", read_roots=(shared,)),
                0,
            )
        self.assertEqual(launch.call_args.kwargs["read_roots"], (shared,))
        self.assertEqual(audit.call_args.kwargs["read_roots"], [str(shared)])

    def test_review_mode_refuses_a_read_root(self) -> None:
        repo = self.repo()
        args = mock.Mock(
            host="claude", mode="review", provider="claude", model="claude-sonnet-5",
            capability=[], lane_name="reads", skill=[], approve_billable_route=False,
            worktree_root=None, verify=None,
        )
        with self.assertRaisesRegex(cli.SideLaneError, "execute mode"):
            cli._launch(args, cli.load_config(), repo, "Review it",
                        read_roots=(repo,))

    def test_the_parser_default_is_no_grant(self) -> None:
        parser = cli.make_parser()
        base = [
            "run", "--host", "devin", "--mode", "execute", "--provider", "devin",
            "--model", "swe-2-medium", "--repo", "/tmp/repo",
            "--lane-name", "reads", "--prompt", "Implement it",
        ]
        self.assertEqual(
            read_roots.parse_read_roots(parser.parse_args(base).read_root), ()
        )
        parsed = parser.parse_args(base + ["--read-root", "/a", "--read-root", "/b"])
        self.assertEqual(parsed.read_root, ["/a", "/b"])


class AuditRecordTests(unittest.TestCase):
    def lane(self, root: Path):
        repo = init_repo(root / "repo")
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "seed.txt"], check=True,
                       capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@example.com",
             "-c", "user.name=T", "commit", "-m", "seed"],
            check=True, capture_output=True,
        )
        # Not disposed: the repository is a temporary directory, and disposing
        # after the caller's `with` block would run against a path that no
        # longer exists.
        return worktrees.create_worktree(repo, "reads")

    def write(self, lane, **kwargs) -> dict:
        path = worktrees.write_audit(
            lane, host="devin", mode="execute", provider="devin",
            model="swe-2-medium", prompt="Implement it", exit_status=0,
            status="## lane", **kwargs
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def test_audit_records_the_granted_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lane = self.lane(root)
            payload = self.write(lane, read_roots=[str(root / "shared")])
            self.assertEqual(payload["read_roots"], [str(root / "shared")])
            # Additive: the field is new, the schema version is not.
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["worktree"], str(lane.worktree))

    def test_a_lane_without_roots_records_an_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = self.write(self.lane(Path(directory).resolve()))
            self.assertEqual(payload["read_roots"], [])


if __name__ == "__main__":
    unittest.main()
