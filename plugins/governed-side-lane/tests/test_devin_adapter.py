import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock

from side_lane import devin_command_policy
from side_lane.adapters import devin
from side_lane.mcp_run_config import McpRunServer


class DevinAdapterTests(unittest.TestCase):
    def repo(self, root: Path, name: str) -> Path:
        path = root / name
        subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
        return path

    def config(self, model: str = "swe-2-medium"):
        provider = {"gateway": "native-devin", "auth_method": "oauth", "billable": False}
        route = {"runtime_model": model, "protocol": "native-devin",
            "identity_contract": {"requested_model": model, "resolved_model": model,
                                  "settings_precedence": "verified"},
            "qualification": {"verified": True, "verified_on": "2026-09-11",
                              "source": "mocked local report"}, "timeout_seconds": 600}
        return provider, route

    def test_launch_default_timeout_and_explicit_override(self) -> None:
        for override, expected in ((None, 1800), (2400, 2400)):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                provider, route = self.config()
                route.pop("timeout_seconds")
                if override is not None:
                    route["timeout_seconds"] = override
                process = mock.Mock(pid=41, returncode=0)
                def popen(command, **kwargs):
                    Path(command[command.index("--export") + 1]).write_text(
                        json.dumps({"steps": [{"model_name": "swe-2-medium"}]}))
                    process.communicate.return_value = ("done", "")
                    return process
                devin.launch(executable="devin", repo=self.repo(root, "repo"),
                    worktree=self.repo(root, "lane"), provider="devin", model="swe-2-medium",
                    provider_config=provider, model_config=route, prompt="task", popen=popen,
                    user_config_path=root / "missing.json")
                process.communicate.assert_called_once_with(timeout=expected)

    def test_command_pins_model_exports_atif_and_has_no_sandbox(self) -> None:
        provider, route = self.config("moonshot.cn/anthropic/kimi-k2.7-code")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            command = devin.build_command(executable="devin", repo=repo, worktree=lane,
                provider="devin", model="moonshot.cn/anthropic/kimi-k2.7-code",
                provider_config=provider, model_config=route, prompt="Implement it",
                export_path=root / "receipt.json", config_path=root / "config.json")
        self.assertEqual(command[command.index("--model") + 1], "moonshot.cn/anthropic/kimi-k2.7-code")
        self.assertTrue(command[command.index("--export") + 1].endswith("receipt.json"))
        self.assertEqual(command[command.index("--permission-mode") + 1], "accept-edits")
        self.assertEqual(command[command.index("--respect-workspace-trust") + 1], "false")
        self.assertIn("# Approved task", command[-1])
        self.assertNotIn("--sandbox", command)
        inherited = {"PATH": "/bin", "DEVIN_SANDBOX": "1", "DEVIN_MODEL": "wrong",
                     "CLAUDE_CODE_OAUTH_TOKEN": "secret", "ANTHROPIC_BASE_URL": "https://wrong",
                     "OPENAI_API_KEY": "secret", "ZAI_API_KEY": "secret",
                     "GLM_BASE_URL": "https://wrong", "MOONSHOT_API_KEY": "secret",
                     "GEMINI_API_KEY": "secret", "GOOGLE_API_KEY": "secret",
                     "SIDE_LANE_CREDENTIAL_OTHER": "other-secret",
                     "SIDE_LANE_CREDENTIALS_DIR": "/private/credentials"}
        self.assertEqual(devin.build_environment(inherited), {"PATH": "/bin"})

    def test_launch_reads_attested_model_and_usage_only(self) -> None:
        model = "swe-2-high"
        provider, route = self.config(model)
        process = mock.Mock(pid=41, returncode=0)
        def popen(command, **_kwargs):
            export = Path(command[command.index("--export") + 1])
            export.write_text(json.dumps({"agent": {"model_name": "SWE-2 High"},
                "steps": [{"model_name": model, "extra": {"generation_model": model}}],
                "final_metrics": {"total_prompt_tokens": 12,
                                  "total_completion_tokens": 3,
                                  "total_cached_tokens": 2, "total_steps": 1}}))
            process.communicate.return_value = ("done", "")
            return process
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            result = devin.launch(executable="devin", repo=repo, worktree=lane,
                provider="devin", model=model, provider_config=provider,
                model_config=route, prompt="Implement it", popen=popen)
            self.assertTrue(Path(result.provider_artifact).is_file())
        self.assertEqual(result.resolved_model, model)
        self.assertEqual(result.usage, {"total_prompt_tokens": 12,
            "total_completion_tokens": 3, "total_cached_tokens": 2, "total_steps": 1})
        self.assertFalse(result.billable)

    def test_runtime_config_disables_subagents_and_preserves_command_prefixes(self) -> None:
        config = devin._runtime_config("swe-2-medium", ("shell", "playwright"))
        self.assertIn("Exec(cd)", config["permissions"]["allow"])
        self.assertFalse(config["subagents_enabled"])
        self.assertEqual(config["agent"]["model"], "swe-2-medium")
        self.assertIn("Exec(python3)", config["permissions"]["allow"])
        for interpreter in ("python", "python3", "python3.11"):
            self.assertIn(f"Exec(.venv/bin/{interpreter})", config["permissions"]["allow"])
        self.assertIn("Exec(git status)", config["permissions"]["allow"])
        self.assertIn("Exec(git branch -a)", config["permissions"]["allow"])
        self.assertIn("Exec(env)", config["permissions"]["allow"])
        self.assertIn("Exec(git commit)", config["permissions"]["allow"])
        self.assertNotIn("Exec(git)", config["permissions"]["allow"])
        self.assertFalse(any("*" in rule for rule in config["permissions"]["allow"]
                             if rule.startswith("Exec(")))
        self.assertIn("mcp__playwright__browser_navigate", config["permissions"]["allow"])
        self.assertIn("mcp__playwright__browser_click", config["permissions"]["allow"])
        self.assertIn("mcp__playwright__browser_find", config["permissions"]["allow"])
        self.assertNotIn("mcp__playwright__*", config["permissions"]["allow"])

    def test_runtime_config_grants_pythondontwritebytecode_pregrant_for_shell_interpreters(self) -> None:
        config = devin._runtime_config("swe-2-medium", ("shell", "playwright"))
        allow = config["permissions"]["allow"]
        for interpreter in ("python", "python3", "python3.11", "python3.12"):
            self.assertIn(f"Exec(PYTHONDONTWRITEBYTECODE=1 {interpreter})", allow)
        for interpreter in ("python", "python3", "python3.11"):
            self.assertIn(f"Exec(PYTHONDONTWRITEBYTECODE=1 .venv/bin/{interpreter})", allow)
        # The pregrant is derived only from the shell capability.
        no_shell = devin._runtime_config("swe-2-medium", ("playwright",))
        self.assertFalse(any(rule.startswith("Exec(PYTHONDONTWRITEBYTECODE=1")
                             for rule in no_shell["permissions"]["allow"]))
        # No other command category receives the pregrant.
        self.assertFalse(any(rule == "Exec(PYTHONDONTWRITEBYTECODE=1 cd)"
                             for rule in allow))
        for prefix in ("git", "echo", "uv"):
            self.assertFalse(any(rule.startswith(f"Exec(PYTHONDONTWRITEBYTECODE=1 {prefix}")
                                 for rule in allow))

    def test_runtime_config_grants_exact_slack_read_tools_only(self) -> None:
        config = devin._runtime_config("swe-2-medium", ("slack-read",))
        allow = config["permissions"]["allow"]
        slack = sorted(
            rule for rule in allow if rule.startswith("mcp__slack__")
        )
        self.assertEqual(
            slack,
            ["mcp__slack__slack_read_channel", "mcp__slack__slack_read_thread"],
        )
        self.assertNotIn("mcp__slack__*", allow)

    def test_runtime_config_grants_exact_cm_services_tools_per_capability(self) -> None:
        asana = devin._runtime_config("swe-2-medium", ("asana-read",))["permissions"]["allow"]
        self.assertEqual(
            {rule for rule in asana if rule.startswith("mcp__cm-services__")},
            {"mcp__cm-services__asana_get_task", "mcp__cm-services__asana_get_project",
             "mcp__cm-services__asana_list_project_tasks"},
        )
        drive = devin._runtime_config("swe-2-medium", ("drive-read",))["permissions"]["allow"]
        self.assertEqual(
            {rule for rule in drive if rule.startswith("mcp__cm-services__")},
            {"mcp__cm-services__drive_file_info", "mcp__cm-services__drive_sheet_tabs",
             "mcp__cm-services__drive_sheet_get", "mcp__cm-services__drive_doc_get"},
        )
        algolia = devin._runtime_config("swe-2-medium", ("algolia-read",))["permissions"]["allow"]
        self.assertEqual(
            {rule for rule in algolia if rule.startswith("mcp__cm-services__")},
            {"mcp__cm-services__algolia_get_settings"},
        )
        # Disjoint on the shared server: an asana grant never unlocks drive
        # tools, and no capability-free or unrelated lane touches the server.
        self.assertFalse(any("drive_" in rule for rule in asana
                             if rule.startswith("mcp__")))
        self.assertFalse(any("asana_" in rule for rule in drive
                             if rule.startswith("mcp__")))
        self.assertFalse(any("algolia_" in rule for rule in asana
                             if rule.startswith("mcp__")))
        self.assertFalse(any("algolia_" in rule for rule in drive
                             if rule.startswith("mcp__")))
        plain = devin._runtime_config("swe-2-medium", ("shell",))["permissions"]["allow"]
        self.assertFalse(any(rule.startswith("mcp__cm-services__") for rule in plain))
        both = devin._runtime_config("swe-2-medium", ("asana-read", "drive-read"))["permissions"]["allow"]
        self.assertNotIn("mcp__cm-services__*", both)
        self.assertEqual(
            len([rule for rule in both if rule.startswith("mcp__cm-services__")]), 7)
        # gateway-read follows the family: exact Gateway run tools only.
        gateway = devin._runtime_config("swe-2-medium", ("gateway-read",))["permissions"]["allow"]
        self.assertEqual(
            {rule for rule in gateway if rule.startswith("mcp__cm-services__")},
            {"mcp__cm-services__gateway_run_status",
             "mcp__cm-services__gateway_run_report"},
        )
        self.assertFalse(any("asana_" in rule for rule in gateway))
        # The granted set is the canonical capability partition, not a second
        # hand-maintained list: no non-cm-services grant is silently dropped.
        for capability in ("playwright", "gitnexus", "codegraph", "slack-read", "aws-read"):
            with self.subTest(capability=capability):
                rules = devin._runtime_config("swe-2-medium", (capability,))["permissions"]["allow"]
                self.assertTrue(
                    any(rule.startswith("mcp__") for rule in rules),
                    f"{capability} grants no mcp rule on Devin",
                )

    def test_runtime_config_preserves_git_push_denies_and_inherited_hooks(self) -> None:
        inherited_hook = {"matcher": "^edit$", "hooks": [{"type": "command", "command": "check"}]}
        config = devin._runtime_config("swe-2-medium", ("git-push",),
            {"hooks": {"PreToolUse": [inherited_hook]}}, "python policy.py rules.json")
        self.assertIn("Exec(git push)", config["permissions"]["allow"])
        self.assertNotIn("Exec(git)", config["permissions"]["allow"])
        self.assertIn("Exec(git push --force)", config["permissions"]["deny"])
        self.assertNotIn("Exec(git push * --force)", config["permissions"]["deny"])
        self.assertEqual(config["hooks"]["PreToolUse"][0], inherited_hook)
        policy_hook = config["hooks"]["PreToolUse"][1]
        self.assertEqual(policy_hook["matcher"], "^(exec|write|edit|str_replace)$")
        self.assertEqual(policy_hook["hooks"][0]["command"], "python policy.py rules.json")

    def test_runtime_config_grants_dash_c_spelling_of_allowed_git_commands(self) -> None:
        config = devin._runtime_config("swe-2-medium", ("shell",), worktree=Path("/lane"))
        allow = config["permissions"]["allow"]
        self.assertIn("Exec(git status)", allow)
        self.assertIn("Exec(git -C /lane status)", allow)
        self.assertIn("Exec(git -C . status)", allow)
        self.assertIn("Exec(git -C /lane commit)", allow)
        self.assertNotIn("Exec(git -C /lane)", allow)
        self.assertNotIn("Exec(git -C .)", allow)
        self.assertNotIn("Exec(git)", allow)
        self.assertFalse(any("*" in rule for rule in allow if rule.startswith("Exec(")))
        # Every `-C` grant sits after every canonical git grant in list order.
        canonical = [index for index, rule in enumerate(allow)
                     if rule.startswith("Exec(git ") and not rule.startswith("Exec(git -C ")]
        dash_c = [index for index, rule in enumerate(allow) if rule.startswith("Exec(git -C ")]
        self.assertTrue(canonical and dash_c)
        self.assertLess(max(canonical), min(dash_c))

    def test_runtime_config_dash_c_grant_quotes_a_worktree_path_with_spaces(self) -> None:
        # Devin matches grants against the command as spelled; a path that
        # needs quoting must appear in the grant exactly as the model must
        # type it, and a plain path stays unquoted.
        config = devin._runtime_config("swe-2-medium", ("shell",),
                                       worktree=Path("/tmp/my lanes/lane"))
        allow = config["permissions"]["allow"]
        self.assertIn("Exec(git -C '/tmp/my lanes/lane' status)", allow)
        self.assertNotIn("Exec(git -C /tmp/my lanes/lane status)", allow)
        plain = devin._runtime_config("swe-2-medium", ("shell",), worktree=Path("/lane"))
        self.assertIn("Exec(git -C /lane status)", plain["permissions"]["allow"])

    def test_runtime_config_dash_c_grants_the_quoted_spelling_of_a_plain_path(self) -> None:
        # Reproduced 2026-09-19: a lane whose worktree path has no spaces was
        # granted only the unquoted spelling, and `git -C "<lane>" ...` then
        # matched no grant and was prompted, which ends a non-interactive run.
        config = devin._runtime_config("swe-2-medium", ("shell",), worktree=Path("/lane"))
        allow = config["permissions"]["allow"]
        self.assertIn("Exec(git -C /lane status)", allow)
        self.assertIn('Exec(git -C "/lane" status)', allow)
        self.assertIn('Exec(git -C "/lane" commit)', allow)
        self.assertNotIn('Exec(git -C "/lane")', allow)

    def test_path_spellings_are_equivalent_to_the_literal_path(self) -> None:
        for literal in ("/lane", "/tmp/my lanes/lane", "/tmp/a'b", '/tmp/a"b', "/tmp/a$b",
                        "/tmp/a`b", "/tmp/a\\b"):
            path = Path(literal)
            spellings = devin._path_spellings(path)
            with self.subTest(literal=literal):
                self.assertTrue(spellings)
                self.assertEqual(spellings[0], shlex.quote(literal))
                for spelling in spellings:
                    # Each granted spelling must denote exactly this path and
                    # carry no composition, substitution or expansion syntax:
                    # that is what keeps the extra grants from widening.
                    self.assertEqual(shlex.split(spelling), [literal])
                    self.assertNotIn(";", spelling)
                    self.assertNotIn("|", spelling)
                    self.assertNotIn("\n", spelling)
                self.assertEqual(len(set(spellings)), len(spellings))

    def test_path_spellings_withhold_a_spelling_a_shell_would_expand(self) -> None:
        # A literal `$` is inert inside single quotes but would expand inside
        # double quotes, so only the single-quoted spelling may be granted.
        self.assertEqual(devin._path_spellings(Path("/tmp/a$b")), ("'/tmp/a$b'",))
        self.assertEqual(devin._path_spellings(Path('/tmp/a`b')), ("'/tmp/a`b'",))
        self.assertEqual(devin._path_spellings(Path('/tmp/a"b')), ("'/tmp/a\"b'",))
        self.assertEqual(devin._path_spellings(Path("/tmp/a\\b")), ("'/tmp/a\\b'",))

    def test_runtime_config_dash_c_grants_stay_bounded_to_git_subcommands(self) -> None:
        config = devin._runtime_config("swe-2-medium", ("shell", "git-push"),
                                       worktree=Path("/tmp/my lanes/lane"))
        allow = config["permissions"]["allow"]
        dash_c = [rule for rule in allow if rule.startswith("Exec(git -C ")]
        self.assertTrue(dash_c)
        for rule in dash_c:
            tokens = shlex.split(rule[len("Exec("):-1])
            self.assertEqual(tokens[:2], ["git", "-C"])
            # The target is always the lane worktree or the CLI's own working
            # directory; the spelling varies, the directory never does.
            self.assertIn(tokens[2], {"/tmp/my lanes/lane", "."})
            # Every grant names a git subcommand, so a bare `git -C <path>` is
            # never emitted, and no grant is widened to a wildcard.
            self.assertGreater(len(tokens), 3)
            self.assertNotIn("*", rule)

    def test_runtime_config_without_worktree_has_no_dash_c_grants(self) -> None:
        config = devin._runtime_config("swe-2-medium", ("shell",))
        self.assertFalse(any("-C" in rule for rule in config["permissions"]["allow"]))

    def test_runtime_config_dash_c_never_touches_deny(self) -> None:
        config = devin._runtime_config("swe-2-medium", ("git-push",), worktree=Path("/lane"))
        self.assertIn("Exec(git push --force)", config["permissions"]["deny"])
        self.assertFalse(any("-C" in rule for rule in config["permissions"]["deny"]))

    def test_launch_policy_rules_carry_the_lane_worktree(self) -> None:
        model = "swe-2-medium"
        provider, route = self.config(model)
        process = mock.Mock(pid=41, returncode=0)
        captured: dict = {}
        def popen(command, **_kwargs):
            config = json.loads(Path(command[command.index("--config") + 1]).read_text())
            captured["config"] = config
            hook = next(entry for entry in config["hooks"]["PreToolUse"]
                        if "devin_command_policy" in entry["hooks"][0]["command"])
            captured["rules_path"] = hook["hooks"][0]["command"].split()[-1]
            Path(command[command.index("--export") + 1]).write_text(
                json.dumps({"steps": [{"model_name": model}]}))
            process.communicate.return_value = ("done", "")
            return process
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lane = self.repo(root, "lane")
            devin.launch(executable="devin", repo=self.repo(root, "repo"),
                worktree=lane, provider="devin", model=model,
                provider_config=provider, model_config=route, prompt="Implement it",
                popen=popen, capabilities=("shell",), user_config_path=root / "missing.json")
            rules = json.loads(Path(captured["rules_path"]).read_text())
        self.assertEqual(rules["worktree"], str(lane.resolve()))
        self.assertTrue(all(isinstance(rule, str) for rule in rules["allowed"]))
        self.assertIn(f"Exec(git -C {lane.resolve()} status)",
                      captured["config"]["permissions"]["allow"])
        # The quoted spelling of the same lane must be granted too: it is the
        # spelling a worker actually typed when the run was rejected. The hook
        # still normalises either spelling to the canonical rule, so the deny
        # side of the policy is unaffected by the extra grants.
        self.assertIn(f'Exec(git -C "{lane.resolve()}" status)',
                      captured["config"]["permissions"]["allow"])
        self.assertIsNone(devin_command_policy.evaluate_event(
            {"tool_name": "exec", "tool_input": {
                "command": f'git -C "{lane.resolve()}" status'}},
            rules["allowed"], rules["denied"], worktree=rules["worktree"]))

    def test_launch_installs_write_containment_hook_with_no_command_capabilities(self) -> None:
        # A capability-free execute lane (no shell/workspace-write/git-push)
        # still uses Devin's write/edit/str_replace tools, so the containment
        # hook must be installed even though the Bash allowlist is empty.
        model = "swe-2-medium"
        provider, route = self.config(model)
        process = mock.Mock(pid=41, returncode=0)
        captured: dict = {}
        def popen(command, **_kwargs):
            config = json.loads(Path(command[command.index("--config") + 1]).read_text())
            hook = next(entry for entry in config["hooks"]["PreToolUse"]
                        if "devin_command_policy" in entry["hooks"][0]["command"])
            captured["rules_path"] = hook["hooks"][0]["command"].split()[-1]
            Path(command[command.index("--export") + 1]).write_text(
                json.dumps({"steps": [{"model_name": model}]}))
            process.communicate.return_value = ("done", "")
            return process
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lane = self.repo(root, "lane")
            devin.launch(executable="devin", repo=self.repo(root, "repo"),
                worktree=lane, provider="devin", model=model,
                provider_config=provider, model_config=route, prompt="Implement it",
                popen=popen, capabilities=(), user_config_path=root / "missing.json")
            rules = json.loads(Path(captured["rules_path"]).read_text())
        self.assertEqual(rules["worktree"], str(lane.resolve()))
        self.assertEqual(rules["allowed"], [])
        # An outside write is still blocked by the installed hook.
        from side_lane import devin_command_policy as policy
        decision = policy.evaluate_event(
            {"tool_name": "write", "tool_input": {"file_path": "/tmp/outside.py"}},
            rules["allowed"], rules["denied"], worktree=rules["worktree"])
        self.assertEqual(decision["decision"], "block")

    def test_billable_is_explicit_and_passed_through(self) -> None:
        model = "swe-2-medium"
        provider, route = self.config(model)
        provider["billable"] = True
        process = mock.Mock(pid=41, returncode=0)
        captured = {}
        def popen(command, **_kwargs):
            config_path = Path(command[command.index("--config") + 1])
            captured.update(json.loads(config_path.read_text()))
            Path(command[command.index("--export") + 1]).write_text(
                json.dumps({"steps": [{"model_name": model}]}))
            process.communicate.return_value = ("done", "")
            return process
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = devin.launch(executable="devin", repo=self.repo(root, "repo"),
                worktree=self.repo(root, "lane"), provider="devin", model=model,
                provider_config=provider, model_config=route, prompt="Implement it", popen=popen,
                capabilities=("shell",), user_config_path=root / "missing.json")
        self.assertTrue(result.billable)
        policy_command = captured["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn("devin_command_policy.py", policy_command)
        self.assertIn("devin-command-policy.json", policy_command)
        for invalid in (None, 0, "false"):
            bad_provider = dict(provider, billable=invalid)
            with self.assertRaisesRegex(devin.DevinAdapterError, "explicit boolean billable"):
                devin._validate_route("devin", model, bad_provider, route, "execute")

    def test_runtime_config_merges_safe_jsonc_settings_and_preserves_restrictions(self) -> None:
        raw = '''{
          // retained nonsecret behavior
          "theme_mode": "dark",
          "read_config_from": ["user"],
          "hooks": {"after": ["notify"]},
          "proxy": "https://secret.invalid",
          "permissions": {"allow": ["Exec(*)"], "deny": ["Exec(rm)"], "ask": ["Exec(curl)"],},
        }'''
        inherited = devin._parse_jsonc(raw)
        config = devin._runtime_config("swe-2-medium", (), inherited)
        self.assertEqual(config["theme_mode"], "dark")
        self.assertEqual(config["read_config_from"], ["user"])
        self.assertEqual(config["hooks"], {"after": ["notify"]})
        self.assertNotIn("proxy", config)
        self.assertNotIn("Exec(*)", config["permissions"]["allow"])
        self.assertEqual(config["permissions"]["deny"], ["Exec(rm)"])
        self.assertEqual(config["permissions"]["ask"], ["Exec(curl)"])
        with self.assertRaisesRegex(devin.DevinAdapterError, "permissions.deny"):
            devin._runtime_config("swe-2-medium", (), {"permissions": {"deny": "Exec(rm)"}})
        with self.assertRaisesRegex(devin.DevinAdapterError, "cannot parse"):
            devin._parse_jsonc('{"theme_mode": }')

    def test_launch_fails_closed_on_missing_mismatched_or_multiple_atif_models(self) -> None:
        model = "swe-2-medium"
        provider, route = self.config(model)
        payloads = (({"steps": []}, "identity-unverified"),
                    ({"steps": [{"model_name": "swe-2-high"}]}, "identity-mismatch"),
                    ({"steps": [{"model_name": model}, {"model_name": "swe-2-high"}]},
                     "identity-unverified"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            for payload, error in payloads:
                process = mock.Mock(pid=41, returncode=0)
                def popen(command, **_kwargs):
                    Path(command[command.index("--export") + 1]).write_text(json.dumps(payload))
                    process.communicate.return_value = ("done", "")
                    return process
                result = devin.launch(executable="devin", repo=repo, worktree=lane,
                    provider="devin", model=model, provider_config=provider,
                    model_config=route, prompt="Implement it", popen=popen,
                    user_config_path=root / "missing.json")
                self.assertEqual(result.returncode, 65)
                self.assertIn(error, result.stderr)

    def test_launch_git_excludes_the_generated_local_mcp_file(self) -> None:
        # The generated `.devin/mcp_config.local.json` lives inside the lane
        # worktree for the whole run; without an exclude entry a mid-run
        # `git add -A` would stage it. The entry must land before the file is
        # written (restore only runs after the worker exits).
        provider, route = self.config()
        process = mock.Mock(pid=41, returncode=0)
        servers = {"aws": McpRunServer(
            name="aws", url="https://bridge.example.invalid/mcp",
            headers=(("Authorization", "Bearer", "CLAUDE_TAG_AWS_MCP_TOKEN"),),
        )}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lane = self.repo(root, "lane")
            def popen(command, **_kwargs):
                Path(command[command.index("--export") + 1]).write_text(
                    json.dumps({"steps": [{"model_name": "swe-2-medium"}]}))
                process.communicate.return_value = ("done", "")
                return process
            result = devin.launch(executable="devin",
                repo=self.repo(root, "repo"), worktree=lane, provider="devin",
                model="swe-2-medium", provider_config=provider, model_config=route,
                prompt="task",
                env={"PATH": "/bin", "HOME": str(root),
                     "CLAUDE_TAG_AWS_MCP_TOKEN": "placeholder"},
                popen=popen, user_config_path=root / "missing.json",
                run_mcp_servers=servers)
            self.assertEqual(result.returncode, 0)
            exclude = lane / ".git" / "info" / "exclude"
            self.assertIn("/.devin/mcp_config.local.json",
                          exclude.read_text(encoding="utf-8").splitlines())
            self.assertFalse((lane / ".devin" / "mcp_config.local.json").exists())
            ignored = subprocess.run(
                ["git", "-C", str(lane), "check-ignore", "-q",
                 ".devin/mcp_config.local.json"], capture_output=True)
            self.assertEqual(ignored.returncode, 0)

    @mock.patch("side_lane.adapters.devin.os.killpg")
    def test_timeout_terminates_the_process_group(self, killpg: mock.Mock) -> None:
        process = mock.Mock(pid=42, returncode=None)
        process.communicate.side_effect = [subprocess.TimeoutExpired("devin", 1), ("partial", "timed out")]
        code, stdout, stderr = devin._run(("devin",), cwd=Path("."), env={}, timeout=1,
            popen=mock.Mock(return_value=process))
        self.assertEqual((code, stdout, stderr), (124, "partial", "timed out"))
        killpg.assert_called_once_with(42, devin.signal.SIGTERM)

    def test_unqualified_model_fails_before_process_creation(self) -> None:
        provider, route = self.config()
        route.pop("qualification")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            popen = mock.Mock()
            with self.assertRaisesRegex(devin.DevinAdapterError, "unqualified"):
                devin.launch(executable="devin", repo=repo, worktree=lane,
                    provider="devin", model="swe-2-medium", provider_config=provider,
                    model_config=route, prompt="Implement it", popen=popen)
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
