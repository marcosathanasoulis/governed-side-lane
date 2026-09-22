"""Coordinator-granted documentation domains: validation, rendering, no widening.

The property under test is asymmetric on purpose. A granted domain must
*work* — a worker that has to read vendor documentation has to be able to
fetch it, or the host prompts and a non-interactive run ends — and it must
change nothing else: no other host, no bare ``Fetch``/``WebFetch``, no grant
implied by shell or workspace authority, and no grant in a review lane.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from side_lane import cli, web_domains, worktrees
from side_lane.adapters import claude, codex, devin

# Hermetic suite: an inherited SIDE_LANE_MODELS_PATH or
# SIDE_LANE_ROUTING_CATALOG_PATH would silently re-point the config and
# catalog defaults these tests exercise, so both are removed at import.
# Tests that want an override set it explicitly (mock.patch.dict).
for _var in ("SIDE_LANE_MODELS_PATH", "SIDE_LANE_ROUTING_CATALOG_PATH"):
    os.environ.pop(_var, None)


#: A real host the failure this change repairs actually needed.
GOOGLE = "cloud.google.com"


def init_repo(path: Path) -> Path:
    subprocess.run(
        ["git", "init", "-b", "main", str(path)], check=True, capture_output=True
    )
    return path


def worktree_dir(root: Path, name: str) -> Path:
    """A directory that only has to satisfy the adapters' worktree check."""

    path = root / name
    path.mkdir()
    (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
    return path


class DomainValidationTests(unittest.TestCase):
    def test_domain_list_is_bounded_before_deduplication(self) -> None:
        with self.assertRaises(web_domains.WebDomainError):
            web_domains.parse_web_domains(["cloud.google.com"] * 11)
        self.assertEqual(web_domains.parse_web_domains(["cloud.google.com"] * 10), ("cloud.google.com",))

    def test_no_request_is_an_empty_grant(self) -> None:
        self.assertEqual(web_domains.parse_web_domains(None), ())
        self.assertEqual(web_domains.parse_web_domains([]), ())

    def test_the_reporting_failure_this_repairs_is_grantable(self) -> None:
        self.assertEqual(web_domains.parse_web_domains([GOOGLE]), (GOOGLE,))

    def test_ordinary_public_hosts_are_grantable(self) -> None:
        for host in (
            "docs.python.org",
            "developer.mozilla.org",
            "example.co.uk",
            "a.b.c.d.example.com",
            "xn--bcher-kva.com",
            "my-host.example.com",
        ):
            with self.subTest(host=host):
                self.assertEqual(web_domains.validate_host(host), host)

    def test_duplicates_collapse_and_the_order_is_deterministic(self) -> None:
        parsed = web_domains.parse_web_domains(["b.example.com", "a.example.com",
                                                "b.example.com"])
        self.assertEqual(parsed, ("a.example.com", "b.example.com"))

    def test_a_non_list_input_is_rejected(self) -> None:
        for value in ("cloud.google.com", 7, {"a": 1}):
            with self.subTest(value=value):
                with self.assertRaises(web_domains.WebDomainError):
                    web_domains.parse_web_domains(value)

    def test_non_string_and_empty_entries_are_rejected(self) -> None:
        for value in (None, 7, "", "   ", b"cloud.google.com"):
            with self.subTest(value=value):
                with self.assertRaises(web_domains.WebDomainError):
                    web_domains.parse_web_domains([value])

    def test_a_url_is_not_a_hostname(self) -> None:
        # The exact value from the failed run: a full URL, fragment included.
        for value in (
            "https://cloud.google.com/",
            "http://cloud.google.com/x",
            "cloud.google.com/scheduler/docs/reference/rpc",
            "cloud.google.com#job",
            "cloud.google.com?x=1",
        ):
            with self.subTest(value=value):
                with self.assertRaises(web_domains.WebDomainError):
                    web_domains.validate_host(value)

    def test_scheme_port_and_userinfo_are_rejected(self) -> None:
        for value in ("cloud.google.com:443", "https://cloud.google.com:443",
                      "user@cloud.google.com", "user:pw@cloud.google.com",
                      "//cloud.google.com"):
            with self.subTest(value=value):
                with self.assertRaises(web_domains.WebDomainError):
                    web_domains.validate_host(value)

    def test_a_wildcard_is_never_grantable(self) -> None:
        for value in ("*.google.com", "cloud.*.com", "cloud.google.co*",
                      "cloud.google.com/*", "cloud.google.com*"):
            with self.subTest(value=value):
                with self.assertRaises(web_domains.WebDomainError):
                    web_domains.validate_host(value)

    def test_rule_delimiters_and_quotes_are_rejected(self) -> None:
        for value in ("Fetch(cloud.google.com)", "cloud.google.com)",
                      "cloud.google.com'", 'cloud.google.com"',
                      "cloud.google.com`", "cloud.google.com;rm -rf /"):
            with self.subTest(value=value):
                with self.assertRaises(web_domains.WebDomainError):
                    web_domains.validate_host(value)

    def test_ip_literals_are_rejected(self) -> None:
        for value in (
            "127.0.0.1", "0.0.0.0", "10.0.0.1", "192.168.1.1", "169.254.169.254",
            "8.8.8.8", "[::1]", "2001:db8::1", "fe80::1",
            # Alternate IPv4 spellings a resolver still accepts: a numeric
            # top-level label is refused whichever form produced it.
            "0177.0.0.1", "0x7f.0.0.1", "1.2.3.04",
        ):
            with self.subTest(value=value):
                with self.assertRaises(web_domains.WebDomainError):
                    web_domains.validate_host(value)

    def test_localhost_and_private_or_reserved_names_are_rejected(self) -> None:
        for value in (
            "localhost", "foo.localhost", "localhost.localdomain",
            "printer.local", "db.internal", "wiki.intranet", "nas.lan",
            "x.home", "host.corp", "thing.private", "svc.onion",
            "a.test", "foo.invalid", "example.example",
            "1.0.0.127.in-addr.arpa",
        ):
            with self.subTest(value=value):
                with self.assertRaises(web_domains.WebDomainError):
                    web_domains.validate_host(value)

    def test_a_single_label_names_no_public_origin(self) -> None:
        for value in ("com", "google", "cloud"):
            with self.subTest(value=value):
                with self.assertRaises(web_domains.WebDomainError):
                    web_domains.validate_host(value)

    def test_malformed_domain_syntax_is_rejected(self) -> None:
        for value in (
            "cloud.google.com.", ".cloud.google.com", "cloud..google.com",
            "-cloud.google.com", "cloud-.google.com",
            "cloud_google.com", "cloud.google.c_m", "cloud.google.com ",
            " cloud.google.com", "cloud.google.com\t", "cloud.google.com\n",
            "cloud.google.com,", "a" * 64 + ".com",
        ):
            with self.subTest(value=value):
                with self.assertRaises(web_domains.WebDomainError):
                    web_domains.validate_host(value)

    def test_uppercase_and_non_ascii_are_rejected_rather_than_rewritten(self) -> None:
        for value in ("Cloud.Google.com", "CLOUD.GOOGLE.COM", "münchen.de",
                      "☃.com", "xn--bcher-kva.example"):
            with self.subTest(value=value):
                with self.assertRaises(web_domains.WebDomainError):
                    web_domains.validate_host(value)

    def test_an_over_long_hostname_is_rejected(self) -> None:
        with self.assertRaises(web_domains.WebDomainError):
            web_domains.validate_host(".".join(["abcde"] * 60) + ".com")

    def test_a_non_string_value_raises_on_the_renderers_own_check(self) -> None:
        for renderer in (web_domains.devin_rule, web_domains.claude_rule):
            with self.subTest(renderer=renderer.__name__):
                with self.assertRaises(web_domains.WebDomainError):
                    renderer(None)


class RuleRenderingTests(unittest.TestCase):
    def test_devin_rule_is_the_documented_host_scoped_prefix(self) -> None:
        self.assertEqual(
            web_domains.devin_rule(GOOGLE), "Fetch(https://cloud.google.com/*)"
        )

    def test_claude_rule_is_the_documented_domain_scoped_form(self) -> None:
        self.assertEqual(
            web_domains.claude_rule(GOOGLE), "WebFetch(domain:cloud.google.com)"
        )

    def test_rendered_rules_never_become_a_global_tool_grant(self) -> None:
        hosts = (GOOGLE, "docs.python.org")
        rules = (*web_domains.devin_rules(hosts), *web_domains.claude_rules(hosts))
        self.assertEqual(
            rules,
            (
                "Fetch(https://cloud.google.com/*)",
                "Fetch(https://docs.python.org/*)",
                "WebFetch(domain:cloud.google.com)",
                "WebFetch(domain:docs.python.org)",
            ),
        )
        for rule in rules:
            with self.subTest(rule=rule):
                self.assertNotIn(rule, ("Fetch", "WebFetch"))
                self.assertTrue(rule.startswith(("Fetch(https://", "WebFetch(domain:")))

    def test_the_renderers_re_validate_the_host_they_are_given(self) -> None:
        for value in ("*.google.com", "127.0.0.1", "localhost", "http://x.com/"):
            with self.subTest(value=value):
                with self.assertRaises(web_domains.WebDomainError):
                    web_domains.devin_rules((value,))
                with self.assertRaises(web_domains.WebDomainError):
                    web_domains.claude_rules((value,))

    def test_an_empty_grant_renders_no_rule_and_no_note(self) -> None:
        self.assertEqual(web_domains.devin_rules(()), ())
        self.assertEqual(web_domains.claude_rules(()), ())
        self.assertEqual(web_domains.scope_note(()), "")

    def test_the_note_names_the_hosts_and_is_honest_about_its_limits(self) -> None:
        note = web_domains.scope_note((GOOGLE,))
        self.assertIn("https://cloud.google.com/*", note)
        self.assertIn("not a network sandbox", note)
        self.assertIn("redirect", note)
        self.assertIn("credentials", note)
        self.assertNotIn("docs.python.org", note)


class DevinDomainTests(unittest.TestCase):
    def allow(self, worktree, capabilities=(), web_domains_value=()):
        return devin._runtime_config(
            "swe-2-medium", capabilities, worktree=worktree,
            web_domains=web_domains_value,
        )["permissions"]["allow"]

    def test_a_requested_host_renders_one_exact_fetch_rule(self) -> None:
        allow = self.allow(Path("/lane"), (), (GOOGLE,))
        fetches = [rule for rule in allow if rule.startswith("Fetch(")]
        self.assertEqual(fetches, ["Fetch(https://cloud.google.com/*)"])

    def test_an_unrequested_host_has_no_rule_at_all(self) -> None:
        for value in ((), ("docs.python.org",)):
            with self.subTest(value=value):
                allow = self.allow(Path("/lane"), (), value)
                self.assertFalse(any(GOOGLE in rule for rule in allow))

    def test_no_web_rule_without_an_explicit_request(self) -> None:
        # Shell authority is never inferred into network reach: this is the
        # regression the canonical capability grants must never acquire.
        for capabilities in ((), ("shell",), ("workspace-write",), ("git-push",),
                             ("shell", "workspace-write", "git-push")):
            with self.subTest(capabilities=capabilities):
                allow = self.allow(Path("/lane"), capabilities)
                self.assertFalse(any(rule.startswith(("Fetch(", "WebFetch(")) for rule in allow))

    def test_a_wildcard_or_ip_never_reaches_the_config(self) -> None:
        for value in ("*.google.com", "127.0.0.1", "localhost", "https://x.com/"):
            with self.subTest(value=value):
                with self.assertRaises(web_domains.WebDomainError):
                    self.allow(Path("/lane"), (), (value,))

    def test_the_grant_does_not_widen_any_other_rule(self) -> None:
        baseline = self.allow(Path("/lane"), ("shell",))
        granted = self.allow(Path("/lane"), ("shell",), (GOOGLE,))
        self.assertEqual([rule for rule in granted if not rule.startswith("Fetch(")],
                         baseline)
        self.assertEqual(
            [rule for rule in granted if rule.startswith("Write(")],
            ["Write(/lane/**)"],
        )


class DevinLaunchDomainTests(unittest.TestCase):
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

    def launch(self, root: Path, hosts):
        repo = init_repo(root / "repo")
        lane = init_repo(root / "lane")
        provider, route = self.route()
        captured: dict = {}

        def popen(command, **kwargs):
            captured["config"] = json.loads(
                Path(command[command.index("--config") + 1]).read_text(encoding="utf-8")
            )
            captured["prompt"] = command[-1]
            captured["command"] = command
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
            web_domains=hosts,
        )
        return captured

    def test_the_grant_reaches_config_and_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            captured = self.launch(root, (GOOGLE,))
        allow = captured["config"]["permissions"]["allow"]
        self.assertIn("Fetch(https://cloud.google.com/*)", allow)
        self.assertIn("https://cloud.google.com/*", captured["prompt"])
        self.assertIn("not a network sandbox", captured["prompt"])
        self.assertIn("# Approved task", captured["prompt"])

    @staticmethod
    def stable_argv(command: Sequence[str]) -> list[str]:
        """The argv with the run-directory paths removed.

        ``--config``/``--export`` name a per-run temporary directory, so two
        otherwise identical launches never share those values.
        """

        stable: list[str] = []
        skip = False
        for item in command:
            if skip:
                skip = False
                continue
            if item in ("--config", "--export"):
                skip = True
                continue
            stable.append(item)
        return stable

    def test_the_grant_changes_nothing_but_the_rule_and_the_note(self) -> None:
        """Same argv, same flags, same model: only the grant and its note differ."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            granted = self.launch(root / "granted", (GOOGLE,))
            plain = self.launch(root / "plain", ())
        # The trailing element is the task text, which carries the grant note.
        self.assertEqual(self.stable_argv(granted["command"][:-1]),
                         self.stable_argv(plain["command"][:-1]))
        # Strip the note exactly as build_command appended it (its own blank
        # line plus the note), and the two prompts are byte-identical.
        inserted = "\n\n" + web_domains.scope_note((GOOGLE,))
        self.assertIn(inserted, granted["prompt"])
        self.assertEqual(granted["prompt"].replace(inserted, "", 1), plain["prompt"])
        self.assertNotEqual(granted["prompt"], plain["prompt"])

    def test_a_lane_without_a_domain_grants_and_says_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            captured = self.launch(root, ())
        allow = captured["config"]["permissions"]["allow"]
        self.assertFalse(any(rule.startswith("Fetch(") for rule in allow))
        self.assertNotIn(web_domains.SCOPE_HEADING, captured["prompt"])

    def test_inherited_deny_and_ask_rules_still_survive_the_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "user-config.json").write_text(
                json.dumps({"permissions": {"allow": [], "deny": ["Fetch(https://evil.example/*)"],
                                            "ask": ["Exec(curl)"]}}),
                encoding="utf-8",
            )
            captured = self.launch(root, (GOOGLE,))
        permissions = captured["config"]["permissions"]
        self.assertEqual(permissions["ask"], ["Exec(curl)"])
        self.assertEqual(permissions["deny"], ["Fetch(https://evil.example/*)"])

    def test_review_mode_never_receives_the_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = init_repo(root / "repo"), init_repo(root / "lane")
            provider, route = self.route()
            with self.assertRaises(devin.DevinAdapterError):
                devin.launch(
                    executable="devin", repo=repo, worktree=lane, provider="devin",
                    model="swe-2-medium", provider_config=provider,
                    model_config=route, prompt="Review it", mode="review",
                    popen=mock.Mock(), web_domains=(GOOGLE,),
                )


class ClaudeDomainTests(unittest.TestCase):
    native = {"gateway": "native-claude", "auth_method": "oauth", "billable": False}
    model_config = {"runtime_model": "claude-sonnet-5", "protocol": "native-claude"}

    def test_the_grant_is_appended_as_one_domain_rule(self) -> None:
        base = claude.allowed_tools("execute", ())
        self.assertEqual(
            claude.allowed_tools("execute", (), (GOOGLE,)),
            base + ("WebFetch(domain:cloud.google.com)",),
        )

    def test_no_web_rule_without_an_explicit_request(self) -> None:
        for capabilities in ((), ("shell",), ("workspace-write",), ("git-push",),
                             ("playwright",)):
            with self.subTest(capabilities=capabilities):
                tools = claude.allowed_tools("execute", capabilities)
                self.assertFalse(any(tool.startswith("WebFetch") for tool in tools))
                self.assertNotIn("WebFetch", tools)

    def test_review_mode_grants_no_web_tool_even_when_asked(self) -> None:
        # No implicit review grant: the review argv stays the strict form, so
        # the flag is refused rather than silently dropped.
        self.assertEqual(claude.allowed_tools("review", (), (GOOGLE,)), ())
        self.assertEqual(claude.disallowed_tools("review", ()), ())

    def test_the_grant_does_not_change_any_other_tool_rule(self) -> None:
        base = claude.allowed_tools("execute", ("shell", "git-push"))
        granted = claude.allowed_tools("execute", ("shell", "git-push"), (GOOGLE,))
        self.assertEqual(
            [tool for tool in granted if not tool.startswith("WebFetch")], list(base)
        )
        # The grant never touches the deny side: nothing about a documentation
        # domain reaches `--disallowedTools`.
        denies = claude.disallowed_tools("execute", ("git-push",))
        self.assertTrue(denies)
        self.assertFalse(any("Fetch" in rule for rule in denies))

    def test_an_invalid_domain_fails_the_render_closed(self) -> None:
        with self.assertRaises(web_domains.WebDomainError):
            claude.allowed_tools("execute", (), ("*.google.com",))

    def test_build_command_emits_the_rule_and_names_the_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = worktree_dir(root, "repo"), worktree_dir(root, "lane")
            command = claude.build_command(
                executable="claude", repo=repo, worktree=lane, provider="claude",
                model="claude-sonnet-5", provider_config=self.native,
                model_config=self.model_config, prompt="task",
                web_domains=(GOOGLE,),
            )
        grants = [command[index + 1] for index, item in enumerate(command)
                  if item == "--allowedTools"]
        self.assertIn("WebFetch(domain:cloud.google.com)", grants)
        self.assertNotIn("WebFetch", grants)
        prompt = command[command.index("--append-system-prompt") + 1]
        self.assertIn("https://cloud.google.com/*", prompt)
        self.assertIn("not a network sandbox", prompt)

    def test_build_command_without_the_flag_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = worktree_dir(root, "repo"), worktree_dir(root, "lane")
            command = claude.build_command(
                executable="claude", repo=repo, worktree=lane, provider="claude",
                model="claude-sonnet-5", provider_config=self.native,
                model_config=self.model_config, prompt="task",
            )
        self.assertFalse(any(item.startswith("WebFetch") for item in command))
        prompt = command[command.index("--append-system-prompt") + 1]
        self.assertNotIn(web_domains.SCOPE_HEADING, prompt)

    def test_review_mode_refuses_the_flag_at_the_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = worktree_dir(root, "repo"), worktree_dir(root, "lane")
            with self.assertRaises(claude.ClaudeAdapterError):
                claude.build_command(
                    executable="claude", repo=repo, worktree=lane, provider="claude",
                    model="claude-sonnet-5", provider_config=self.native,
                    model_config=self.model_config, prompt="task", mode="review",
                    web_domains=(GOOGLE,),
                )

    def test_launch_refuses_a_review_lane_and_never_starts_a_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = init_repo(root / "repo"), init_repo(root / "lane")
            runner = mock.Mock()
            with self.assertRaises(claude.ClaudeAdapterError):
                claude.launch(
                    executable="claude", repo=repo, worktree=lane, provider="claude",
                    model="claude-sonnet-5", provider_config=self.native,
                    model_config=self.model_config, prompt="task", mode="review",
                    runner=runner, web_domains=(GOOGLE,),
                )
            runner.assert_not_called()


class CodexDomainTests(unittest.TestCase):
    """The one host that cannot express the grant refuses it honestly."""

    def test_build_codex_command_refuses_the_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = worktree_dir(root, "repo"), worktree_dir(root, "lane")
            with self.assertRaisesRegex(codex.CodexAdapterError, "not supported on the Codex host"):
                codex.build_codex_command(
                    "codex", repo, lane, "codex", "gpt-5.6",
                    {"gateway": "native-codex", "auth_method": "oauth", "billable": False},
                    {"runtime_model": "gpt-5.6", "protocol": "native-codex"},
                    "task", web_domains=(GOOGLE,),
                )

    def test_run_codex_refuses_the_flag_before_any_process_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = init_repo(root / "repo"), init_repo(root / "lane")
            runner = mock.Mock()
            with self.assertRaises(codex.CodexAdapterError):
                codex.run_codex(
                    executable="codex", repo=repo, worktree=lane, provider="codex",
                    model="gpt-5.6",
                    provider_config={"gateway": "native-codex", "auth_method": "oauth",
                                     "billable": False},
                    model_config={"runtime_model": "gpt-5.6", "protocol": "native-codex"},
                    prompt="task", runner=runner, web_domains=(GOOGLE,),
                )
            runner.assert_not_called()


class CliSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(
            os.environ,
            {"SIDE_LANE_CODEX_EXECUTABLE": "", "SIDE_LANE_CLAUDE_EXECUTABLE": ""},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = init_repo(self.root / "repo")
        (self.repo / "CLAUDE.md").write_text("# Rules\n", encoding="utf-8")
        (self.repo / "AGENTS.md").write_text(
            "You must read [CLAUDE.md](./CLAUDE.md); it is the authoritative "
            "source of truth.\n",
            encoding="utf-8",
        )

    def argv(self, *extra: str) -> list[str]:
        return [
            "run", "--host", "devin", "--mode", "execute", "--provider", "devin",
            "--model", "swe-2-medium", "--repo", str(self.repo),
            "--lane-name", "web", "--prompt", "Implement it", *extra,
        ]

    def run_cli(self, argv: list[str]):
        """Drive the real parser and `run`, stopping at `_launch`."""

        with (
            mock.patch("side_lane.cli.load_config", return_value=cli.load_config()),
            mock.patch("side_lane.cli._launch", return_value=0) as launch,
        ):
            try:
                code = cli.run(argv)
            except Exception as exc:  # re-raised to the caller for assertions
                return launch, exc, None
        return launch, None, code

    def test_the_parser_default_is_no_grant(self) -> None:
        parser = cli.make_parser()
        self.assertEqual(
            web_domains.parse_web_domains(parser.parse_args(self.argv()).web_domain), ()
        )
        parsed = parser.parse_args(self.argv("--web-domain", "a.example.com",
                                             "--web-domain", "b.example.com"))
        self.assertEqual(parsed.web_domain, ["a.example.com", "b.example.com"])

    def test_a_valid_grant_reaches_launch(self) -> None:
        launch, error, _code = self.run_cli(self.argv("--web-domain", GOOGLE))
        self.assertIsNone(error)
        self.assertEqual(launch.call_args.kwargs["web_domains"], (GOOGLE,))

    def test_an_invalid_domain_is_rejected_before_launch(self) -> None:
        for value in ("*.google.com", "127.0.0.1", "localhost", "https://cloud.google.com/",
                      "cloud.google.com:443", "user@cloud.google.com", "com",
                      "printer.local", "CLOUD.GOOGLE.COM"):
            with self.subTest(value=value):
                launch, error, _code = self.run_cli(self.argv("--web-domain", value))
                self.assertIsInstance(error, web_domains.WebDomainError)
                launch.assert_not_called()

    def test_an_invalid_domain_never_creates_a_worktree(self) -> None:
        with mock.patch("side_lane.worktrees.create_worktree") as create:
            _launch, error, _code = self.run_cli(self.argv("--web-domain", "*.google.com"))
        self.assertIsInstance(error, web_domains.WebDomainError)
        create.assert_not_called()

    def test_strict_review_mode_rejects_the_flag(self) -> None:
        argv = [item for item in self.argv("--web-domain", GOOGLE)]
        argv[argv.index("--mode") + 1] = "review"
        # A review lane's own prompt gate runs first for an edit-shaped task,
        # so this uses a review-shaped one and still reaches the refusal.
        argv[argv.index("--prompt") + 1] = "Review the change for correctness"
        launch, error, _code = self.run_cli(argv)
        self.assertIsInstance(error, cli.SideLaneError)
        self.assertIn("supported only in execute mode", str(error))
        launch.assert_not_called()

    def test_review_mode_never_reaches_a_launch_for_any_prompt(self) -> None:
        # Either the review prompt gate or the web-domain refusal stops it;
        # what must never happen is a review lane launched with the grant.
        for prompt in ("Implement it", "Review the change for correctness"):
            with self.subTest(prompt=prompt):
                argv = [item for item in self.argv("--web-domain", GOOGLE)]
                argv[argv.index("--mode") + 1] = "review"
                argv[argv.index("--prompt") + 1] = prompt
                launch, error, _code = self.run_cli(argv)
                self.assertIsInstance(error, cli.SideLaneError)
                launch.assert_not_called()

    def test_the_codex_host_rejects_the_flag(self) -> None:
        argv = self.argv("--web-domain", GOOGLE)
        argv[argv.index("--host") + 1] = "codex"
        launch, error, _code = self.run_cli(argv)
        self.assertIsInstance(error, cli.SideLaneError)
        self.assertIn("Codex host", str(error))
        launch.assert_not_called()


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
        return worktrees.create_worktree(repo, "web")

    def write(self, lane, **kwargs) -> dict:
        path = worktrees.write_audit(
            lane, host="devin", mode="execute", provider="devin",
            model="swe-2-medium", prompt="Implement it", exit_status=0,
            status="## lane", **kwargs
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def test_audit_records_the_granted_domains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = self.lane(Path(directory).resolve())
            payload = self.write(lane, web_domains=[GOOGLE, "docs.python.org"])
            self.assertEqual(payload["web_domains"], [GOOGLE, "docs.python.org"])
            # Additive: the field is new, the schema version is not.
            self.assertEqual(payload["schema_version"], 2)

    def test_a_lane_without_domains_records_an_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = self.write(self.lane(Path(directory).resolve()))
            self.assertEqual(payload["web_domains"], [])

    def test_the_audit_field_is_parity_with_the_rendered_rules(self) -> None:
        # The audit and the worker-facing rules are rendered from one parsed
        # tuple, so what the record says was granted is what the worker got.
        parsed = web_domains.parse_web_domains([GOOGLE])
        allow = devin._runtime_config(
            "swe-2-medium", (), worktree=Path("/lane"), web_domains=parsed
        )["permissions"]["allow"]
        self.assertEqual([rule for rule in allow if rule.startswith("Fetch(")],
                         list(web_domains.devin_rules(parsed)))


class GovernanceDocumentationTests(unittest.TestCase):
    """The shipped canonical governance must describe this grant honestly.

    A reviewer reads ``config/lane-governance.md``, not this module, so the
    limits the implementation keeps — execute-only, no capability implies it,
    a permission match rather than a sandbox, and a host that refuses it — have
    to be stated there too.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.governance = (
            Path(__file__).parents[1] / "config" / "lane-governance.md"
        ).read_text(encoding="utf-8")

    def test_the_flag_and_both_rule_spellings_are_documented(self) -> None:
        self.assertIn("`--web-domain HOST`", self.governance)
        self.assertIn("`Fetch(https://HOST/*)`", self.governance)
        self.assertIn("`WebFetch(domain:HOST)`", self.governance)

    def test_the_limits_are_documented(self) -> None:
        self.assertIn("This grant is execute-only", self.governance)
        self.assertIn("not a network sandbox", self.governance)
        self.assertIn("no capability unlocks it", self.governance)

    def test_the_unsupported_host_is_named_rather_than_implied(self) -> None:
        self.assertIn("Codex", self.governance)
        self.assertIn("danger-full-access", self.governance)
        self.assertIn("refuses `--web-domain`", self.governance)


if __name__ == "__main__":
    unittest.main()
