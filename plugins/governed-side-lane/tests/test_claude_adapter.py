import os
from pathlib import Path
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from side_lane import cli, report_stop_hook
from side_lane.adapters import claude


class ClaudeAdapterTests(unittest.TestCase):
    native = {"gateway": "native-claude", "auth_method": "oauth", "billable": False}
    glm = {"gateway": "direct-zai", "auth_method": "provider-key", "billable": True, "base_url": "https://api.z.ai/api/anthropic"}

    def test_launch_default_timeout_and_explicit_override(self) -> None:
        for mode in ("review", "execute"):
            for override, expected in ((None, 1800), (2400, 2400)):
                with self.subTest(mode=mode, override=override), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    config = {"runtime_model": "claude-sonnet-5", "protocol":
                              "native-claude-readonly" if mode == "review" else "native-claude"}
                    if override is not None:
                        config["timeout_seconds"] = override
                    worker = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "done", ""))
                    claude.launch(executable="claude", repo=self.repo(root, "repo"),
                        worktree=self.repo(root, "lane"), provider="claude", model="claude-sonnet-5",
                        provider_config=self.native, model_config=config, prompt="task",
                        mode=mode, env={"PATH": "/bin"}, runner=worker)
                    self.assertEqual(worker.call_args.kwargs["timeout"], expected)

    def test_bounded_process_accepts_subprocess_run_capture_kwargs(self) -> None:
        completed = claude._bounded_process(
            [sys.executable, "-c", "print('captured')"], timeout=5,
            capture_output=True, check=False, text=True,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "captured")
        self.assertEqual(completed.stderr, "")

    def test_bounded_process_timeout_preserves_real_partial_output(self) -> None:
        completed = claude._bounded_process(
            [sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(10)"],
            timeout=0.1, capture_output=True, check=False, text=True,
        )
        self.assertEqual(completed.returncode, 124)
        self.assertIn("partial", completed.stdout)
        self.assertIn("process group stopped", completed.stderr)

    def repo(self, root: Path, name: str) -> Path:
        path = root / name
        path.mkdir()
        (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
        return path

    def test_native_review_is_strict_no_mcp_and_execute_retains_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            review = claude.build_command(executable="claude", repo=repo, worktree=lane,
                provider="claude", model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude-readonly"}, prompt="review", mode="review")
            execute = claude.build_command(executable="claude", repo=repo, worktree=lane,
                provider="claude", model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude"}, prompt="task")
        self.assertIn("--safe-mode", review)
        self.assertNotIn("--bare", review)
        self.assertIn("Read,Glob,Grep", review)
        self.assertIn('{"mcpServers":{}}', review)
        self.assertIn("acceptEdits", execute)
        self.assertIn("user,project,local", execute)
        self.assertIn("Injected canonical side-lane governance", execute[-1])

    def test_routed_execute_sets_supported_context_budget_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            command = claude.build_command(
                executable="claude", repo=repo, worktree=lane,
                provider="omniroute", model="routed-selector",
                provider_config={"gateway": "omniroute-router", "auth_method": "provider-key",
                                 "billable": True, "base_url": "https://omniroute.example"},
                model_config={"runtime_model": "routed-selector",
                              "protocol": "anthropic-compatible",
                              "qualification": {"verified": True, "verified_on": "2026-09-19", "source": "receipt"},
                              "routing_policy_contract": {
                                  "requested_selector": "routed-selector",
                                  "allowed_upstream_models": ["deepseek-flash"],
                                  "settings_precedence": "verified",
                              }},
                prompt="task",
            )
        self.assertEqual(command[command.index("--autocompact") + 1], "100k")

    def test_routed_launch_uses_isolated_home_and_preserves_source_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            home = root / "home"
            home.mkdir()
            config_path = home / ".claude.json"
            original = b'{"other":"preserve","mcpServers":{"cm-services":{"command":"svc"}}}\n'
            config_path.write_bytes(original)
            settings_dir = home / ".claude"
            settings_dir.mkdir()
            settings = b'{"permissions":{"allow":["Bash(git status)"]}}\n'
            (settings_dir / "settings.json").write_bytes(settings)
            (settings_dir / "CLAUDE.md").write_text("global context", encoding="utf-8")
            (settings_dir / "skills").mkdir()
            (settings_dir / "skills" / "probe.md").write_text("skill", encoding="utf-8")
            (settings_dir / "plugins").mkdir()
            observed: dict[str, object] = {}

            def runner(command, **kwargs):
                observed["command"] = command
                observed["env"] = kwargs["env"]
                child_config = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
                trusted = json.loads((child_config / ".claude.json").read_text())
                observed["child_config"] = child_config
                observed["config"] = trusted
                observed["settings"] = json.loads(
                    (child_config / "settings.json").read_text())
                observed["hook_config"] = json.loads(
                    (child_config / "routed-read-pagination.json").read_text())
                observed["context_symlink"] = (child_config / "CLAUDE.md").is_symlink()
                observed["skill"] = (child_config / "skills" / "probe.md").read_text()
                observed["plugins_symlink"] = (child_config / "plugins").is_symlink()
                return subprocess.CompletedProcess(
                    command, 0,
                    '{"type":"result","subtype":"success","model":"deepseek-flash",'
                    '"usage":{"input_tokens":1,"output_tokens":1}}\n',
                    "",
                )

            result = claude.launch(
                executable="claude", repo=repo, worktree=lane,
                provider="omniroute", model="routed-selector",
                provider_config={"gateway": "omniroute-router", "auth_method": "provider-key",
                                 "billable": True, "base_url": "https://omniroute.example"},
                model_config={"runtime_model": "routed-selector",
                              "protocol": "anthropic-compatible",
                              "qualification": {"verified": True, "verified_on": "2026-09-19", "source": "receipt"},
                              "routing_policy_contract": {
                                  "requested_selector": "routed-selector",
                                  "allowed_upstream_models": ["deepseek-flash"],
                                  "settings_precedence": "verified",
                              }},
                prompt="task", env={"HOME": str(home), "PATH": "/bin"},
                secret="router-secret", runner=runner,
            )
            self.assertEqual(result.returncode, 0)
            child_config = observed["child_config"]
            self.assertNotEqual(child_config, home)
            self.assertFalse(Path(child_config).exists())
            self.assertEqual(observed["env"]["HOME"], str(home))
            self.assertEqual(observed["config"]["mcpServers"], {"cm-services": {"command": "svc"}})
            self.assertNotIn("other", observed["config"])
            self.assertEqual(observed["config"]["projects"][str(repo.resolve())]["hasTrustDialogAccepted"], True)
            self.assertEqual(observed["config"]["projects"][str(lane.resolve())]["hasTrustDialogAccepted"], True)
            self.assertEqual(observed["settings"]["permissions"],
                             {"allow": ["Bash(git status)"]})
            entries = observed["settings"]["hooks"]["PreToolUse"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["matcher"], "Read")
            hook_command = entries[0]["hooks"][0]["command"]
            self.assertIn("routed_read_pagination.py", hook_command)
            self.assertEqual(observed["hook_config"], {"limit": 200})
            self.assertTrue(observed["context_symlink"])
            self.assertTrue(observed["plugins_symlink"])
            self.assertEqual(observed["skill"], "skill")
            self.assertEqual(observed["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "16384")
            self.assertEqual(config_path.read_bytes(), original)
            self.assertEqual((settings_dir / "settings.json").read_bytes(), settings)

    def routed_model_config(self) -> dict:
        return {"runtime_model": "routed-selector",
                "protocol": "anthropic-compatible",
                "qualification": {"verified": True, "verified_on": "2026-09-19",
                                  "source": "receipt"},
                "routing_policy_contract": {
                    "requested_selector": "routed-selector",
                    "allowed_upstream_models": ["deepseek-flash"],
                    "settings_precedence": "verified",
                }}

    def routed_provider_config(self) -> dict:
        return {"gateway": "omniroute-router", "auth_method": "provider-key",
                "billable": True, "base_url": "https://omniroute.example"}

    def test_routed_execute_preserves_inherited_pre_tool_use_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            home = root / "home"
            settings_dir = home / ".claude"
            settings_dir.mkdir(parents=True)
            inherited_hook = {"matcher": "^Bash$",
                              "hooks": [{"type": "command", "command": "check"}]}
            (settings_dir / "settings.json").write_text(json.dumps({
                "permissions": {"allow": ["Bash(git status)"]},
                "hooks": {"PreToolUse": [inherited_hook],
                          "PostToolUse": [{"matcher": "x", "hooks": []}]},
            }), encoding="utf-8")
            observed: dict[str, object] = {}

            def runner(command, **kwargs):
                child_config = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
                observed["settings"] = json.loads(
                    (child_config / "settings.json").read_text())
                return subprocess.CompletedProcess(
                    command, 0,
                    '{"type":"result","subtype":"success","model":"deepseek-flash",'
                    '"usage":{"input_tokens":1,"output_tokens":1}}\n',
                    "",
                )

            result = claude.launch(
                executable="claude", repo=repo, worktree=lane,
                provider="omniroute", model="routed-selector",
                provider_config=self.routed_provider_config(),
                model_config=self.routed_model_config(),
                prompt="task", env={"HOME": str(home), "PATH": "/bin"},
                secret="router-secret", runner=runner,
            )
            self.assertEqual(result.returncode, 0)
            settings = observed["settings"]
            self.assertEqual(settings["permissions"],
                             {"allow": ["Bash(git status)"]})
            self.assertEqual(settings["hooks"]["PostToolUse"],
                             [{"matcher": "x", "hooks": []}])
            entries = settings["hooks"]["PreToolUse"]
            self.assertEqual(entries[0], inherited_hook)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[1]["matcher"], "Read")
            self.assertIn("routed_read_pagination.py",
                          entries[1]["hooks"][0]["command"])

    def test_routed_execute_rejects_non_array_pre_tool_use_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            home = root / "home"
            settings_dir = home / ".claude"
            settings_dir.mkdir(parents=True)
            (settings_dir / "settings.json").write_text(
                '{"hooks":{"PreToolUse":"not-an-array"}}', encoding="utf-8")
            worker = mock.Mock(
                return_value=subprocess.CompletedProcess([], 0, "", ""))
            with self.assertRaisesRegex(claude.ClaudeAdapterError,
                                        "hooks.PreToolUse must be an array"):
                claude.launch(
                    executable="claude", repo=repo, worktree=lane,
                    provider="omniroute", model="routed-selector",
                    provider_config=self.routed_provider_config(),
                    model_config=self.routed_model_config(),
                    prompt="task", env={"HOME": str(home), "PATH": "/bin"},
                    secret="router-secret", runner=worker,
                )
            worker.assert_not_called()

    def test_routed_execute_rejects_malformed_inherited_settings(self) -> None:
        # Every malformed shape fails closed at launch preparation rather
        # than silently dropping the inherited settings or the hook.
        for label, settings_text, message in (
                ("hooks_not_a_mapping",
                 '{"hooks":["not-a-mapping"]}',
                 "Claude settings hooks must be a JSON object"),
                ("settings_not_an_object",
                 '["not-an-object"]',
                 "Claude settings must be a JSON object"),
                ("settings_not_json",
                 '{"hooks": ',
                 "Claude settings are invalid JSON"),
        ):
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
                home = root / "home"
                settings_dir = home / ".claude"
                settings_dir.mkdir(parents=True)
                (settings_dir / "settings.json").write_text(
                    settings_text, encoding="utf-8")
                worker = mock.Mock(
                    return_value=subprocess.CompletedProcess([], 0, "", ""))
                with self.assertRaisesRegex(claude.ClaudeAdapterError, message):
                    claude.launch(
                        executable="claude", repo=repo, worktree=lane,
                        provider="omniroute", model="routed-selector",
                        provider_config=self.routed_provider_config(),
                        model_config=self.routed_model_config(),
                        prompt="task", env={"HOME": str(home), "PATH": "/bin"},
                        secret="router-secret", runner=worker,
                    )
                worker.assert_not_called()

    def test_pagination_hook_entry_carries_timeout_and_resolved_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            home = root / "home"
            home.mkdir()
            observed: dict[str, object] = {}

            def runner(command, **kwargs):
                child_config = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
                observed["settings"] = json.loads(
                    (child_config / "settings.json").read_text())
                return subprocess.CompletedProcess(
                    command, 0,
                    '{"type":"result","subtype":"success","model":"deepseek-flash",'
                    '"usage":{"input_tokens":1,"output_tokens":1}}\n',
                    "",
                )

            claude.launch(
                executable="claude", repo=repo, worktree=lane,
                provider="omniroute", model="routed-selector",
                provider_config=self.routed_provider_config(),
                model_config=self.routed_model_config(),
                prompt="task", env={"HOME": str(home), "PATH": "/bin"},
                secret="router-secret", runner=runner,
            )
            entry = observed["settings"]["hooks"]["PreToolUse"][0]["hooks"][0]
            self.assertEqual(entry["type"], "command")
            self.assertEqual(entry["timeout"], 5)
            # An unresolved or relative module path would make the
            # interpreter exit 2, and a non-zero hook exit blocks the Read.
            module_argument = shlex.split(entry["command"])[1]
            self.assertTrue(Path(module_argument).is_absolute())
            self.assertEqual(module_argument,
                             str(Path(module_argument).resolve()))
            self.assertTrue(Path(module_argument).is_file())

    def test_native_execute_and_review_install_no_pagination_hook(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            for mode, protocol in (("execute", "native-claude"),
                                   ("review", "native-claude-readonly")):
                with self.subTest(mode=mode):
                    observed: dict[str, object] = {}

                    def runner(command, **kwargs):
                        observed["env"] = kwargs["env"]
                        return subprocess.CompletedProcess(command, 0, "done", "")

                    claude.launch(
                        executable="claude", repo=repo, worktree=lane,
                        provider="claude", model="claude-sonnet-5",
                        provider_config=self.native,
                        model_config={"runtime_model": "claude-sonnet-5",
                                      "protocol": protocol},
                        prompt="task", mode=mode, env={"PATH": "/bin"},
                        runner=runner,
                    )
                    self.assertNotIn("CLAUDE_CONFIG_DIR", observed["env"])
            # Routed providers are unqualified for review lanes and fail
            # closed before the routed config home is ever prepared.
            with self.assertRaises(claude.ClaudeAdapterError):
                claude.launch(
                    executable="claude", repo=repo, worktree=lane,
                    provider="omniroute", model="routed-selector",
                    provider_config=self.routed_provider_config(),
                    model_config=self.routed_model_config(),
                    prompt="review", mode="review",
                    env={"PATH": "/bin"}, secret="router-secret",
                    runner=mock.Mock(),
                )

    def test_routed_launch_cleans_isolated_home_when_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            home = root / "home"
            home.mkdir()
            (home / ".claude.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
            with self.assertRaises(claude.ClaudeAdapterError):
                claude.launch(
                    executable="claude", repo=repo, worktree=lane,
                    provider="omniroute", model="routed-selector",
                    provider_config={"gateway": "omniroute-router", "auth_method": "provider-key",
                                     "billable": True, "base_url": "https://omniroute.example"},
                    model_config={"runtime_model": "routed-selector",
                                  "protocol": "anthropic-compatible",
                                  "qualification": {"verified": True, "verified_on": "2026-09-19", "source": "receipt"},
                                  "timeout_seconds": 0,
                                  "routing_policy_contract": {
                                      "requested_selector": "routed-selector",
                                      "allowed_upstream_models": ["deepseek-flash"],
                                      "settings_precedence": "verified",
                                  }},
                    prompt="task", env={"HOME": str(home), "PATH": "/bin"},
                    secret="router-secret", runner=mock.Mock(),
                )
            self.assertEqual(list(root.glob(".side-lane-claude-config-*")), [])

    def test_routed_launch_rejects_malformed_source_mcp_config_without_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            home = root / "home"
            home.mkdir()
            (home / ".claude.json").write_text('{"mcpServers": []}\n', encoding="utf-8")
            worker = mock.Mock()
            with self.assertRaises(claude.ClaudeAdapterError):
                claude.launch(
                    executable="claude", repo=repo, worktree=lane,
                    provider="omniroute", model="routed-selector",
                    provider_config={"gateway": "omniroute-router", "auth_method": "provider-key",
                                     "billable": True, "base_url": "https://omniroute.example"},
                    model_config={"runtime_model": "routed-selector",
                                  "protocol": "anthropic-compatible",
                                  "qualification": {"verified": True, "verified_on": "2026-09-19", "source": "receipt"},
                                  "routing_policy_contract": {
                                      "requested_selector": "routed-selector",
                                      "allowed_upstream_models": ["deepseek-flash"],
                                      "settings_precedence": "verified",
                                  }},
                    prompt="task", env={"HOME": str(home), "PATH": "/bin"},
                    secret="router-secret", runner=worker,
                )
            worker.assert_not_called()
            self.assertEqual(list(root.glob(".side-lane-claude-config-*")), [])

    @unittest.skipUnless(shutil.which("claude"), "pinned Claude CLI is unavailable")
    def test_pinned_claude_reads_mcp_from_config_dir_while_retaining_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "config"
            config_dir.mkdir()
            (config_dir / ".claude.json").write_text(
                json.dumps({"mcpServers": {"probe": {"command": "false"}}, "projects": {}}),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["HOME"] = str(Path(directory) / "host-home")
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)
            result = subprocess.run(
                [shutil.which("claude"), "mcp", "list"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("probe", result.stdout)

    @unittest.skipUnless(shutil.which("claude"), "pinned Claude CLI is unavailable")
    def test_pinned_claude_reads_installed_plugins_from_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "config"
            plugin_path = config_dir / "plugins" / "cache" / "demo"
            plugin_path.mkdir(parents=True)
            (plugin_path / "plugin.json").write_text(
                json.dumps({"name": "demo", "version": "1.0.0"}), encoding="utf-8"
            )
            (config_dir / "plugins" / "installed_plugins.json").write_text(
                json.dumps({"version": 2, "plugins": {"demo@local": [{
                    "scope": "user", "installPath": str(plugin_path), "version": "1.0.0",
                    "installedAt": "2026-01-01T00:00:00Z", "lastUpdated": "2026-01-01T00:00:00Z",
                }]}}), encoding="utf-8"
            )
            env = os.environ.copy()
            env["HOME"] = str(Path(directory) / "host-home")
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)
            result = subprocess.run(
                [shutil.which("claude"), "plugin", "list"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("demo@local", result.stdout)

    def test_playwright_execute_approves_only_the_requested_project_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            execute = claude.build_command(executable="claude", repo=repo, worktree=lane,
                provider="claude", model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude"},
                prompt="task", capabilities=("playwright",))
        settings = json.loads(execute[execute.index("--settings") + 1])
        self.assertEqual(settings["enabledMcpjsonServers"], ["playwright"])
        self.assertNotIn("enableAllProjectMcpServers", settings)
        self.assertIn("WaitForMcpServers", execute)
        self.assertIn('servers: ["playwright"]', execute[-1])

    def test_non_browser_execute_does_not_receive_playwright_startup_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            execute = claude.build_command(executable="claude", repo=repo, worktree=lane,
                provider="claude", model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude"},
                prompt="task", capabilities=("shell",))
        self.assertNotIn("WaitForMcpServers", execute)
        self.assertNotIn("Claude Code Playwright startup", execute[-1])

    def test_playwright_launch_requires_connected_mcp_before_model_runner(self) -> None:
        readiness = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 0, "playwright:\n  Status: ✔ Connected\n", ""))
        worker = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "done", ""))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            result = claude.launch(executable="claude", repo=repo, worktree=lane,
                provider="claude", model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude"},
                prompt="task", capabilities=("playwright",), env={"PATH": "/bin"},
                runner=worker, readiness_runner=readiness)
        self.assertEqual(result.returncode, 0)
        readiness_command = readiness.call_args.args[0]
        self.assertEqual(readiness_command[-3:], ["mcp", "get", "playwright"])
        self.assertEqual(json.loads(readiness_command[2]), {"enabledMcpjsonServers": ["playwright"]})
        self.assertEqual(readiness.call_args.kwargs["timeout"], claude.MCP_READINESS_TIMEOUT_SECONDS)
        worker.assert_called_once()

    def test_playwright_launch_fails_before_model_when_mcp_is_pending(self) -> None:
        readiness = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 0, "playwright:\n  Status: ⏸ Pending approval\n", ""))
        worker = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            with self.assertRaisesRegex(claude.ClaudeAdapterError, "not ready before worker launch"):
                claude.launch(executable="claude", repo=repo, worktree=lane,
                    provider="claude", model="claude-sonnet-5", provider_config=self.native,
                    model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude"},
                    prompt="task", capabilities=("playwright",), env={"PATH": "/bin"},
                    runner=worker, readiness_runner=readiness)
        worker.assert_not_called()

    def test_native_environment_scrubs_keys_and_rejects_secret(self) -> None:
        config = {"runtime_model": "claude-sonnet-5", "protocol": "native-claude"}
        child = claude.build_transport_environment({"PATH": "/bin", "ANTHROPIC_API_KEY": "x",
            "OPENAI_API_KEY": "y", "SIDE_LANE_CREDENTIAL_OTHER": "other-secret",
            "SIDE_LANE_CREDENTIALS_DIR": "/private/credentials"},
            provider="claude", model="claude-sonnet-5", provider_config=self.native, model_config=config, mode="execute")
        self.assertEqual(child, {"PATH": "/bin"})
        with self.assertRaisesRegex(claude.ClaudeAdapterError, "must not receive"):
            claude.build_transport_environment({}, provider="claude", model="claude-sonnet-5",
                provider_config=self.native, model_config=config, mode="execute", secret="never")

    def test_native_environment_scrubs_backend_selectors(self) -> None:
        child = claude.scrub_environment({
            "PATH": "/bin", "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_USE_FOUNDRY": "1",
            "CLAUDE_CODE_EFFORT_LEVEL": "max", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "999999", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "999999",
        })
        self.assertEqual(child, {"PATH": "/bin"})

    def test_direct_candidate_requires_identity_contract_and_pins_nonsecret_settings(self) -> None:
        direct = {"gateway": "direct-kimi", "auth_method": "provider-key", "billable": True,
                  "base_url": "https://api.kimi.com/coding/"}
        config = {"runtime_model": "k3-256k", "protocol": "anthropic-compatible",
                  "identity_contract": {"requested_model": "k3-256k", "resolved_model": "k3-256k",
                                        "settings_precedence": "verified"}, "reasoning_effort": "high",
                  "max_budget_usd": 2}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            command = claude.build_command(executable="claude", repo=repo, worktree=lane,
                provider="kimi", model="k3-256k", provider_config=direct, model_config=config,
                prompt="task", mode="execute")
        settings = command[command.index("--settings") + 1]
        self.assertIn('"enabled":false', settings)
        self.assertIn('"CLAUDE_CODE_SUBAGENT_MODEL":"k3-256k"', settings)
        self.assertEqual(command[command.index("--effort") + 1], "high")
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "2.0")
        with self.assertRaisesRegex(claude.ClaudeAdapterError, "identity contract"):
            claude.build_transport_environment({}, provider="kimi", model="k3-256k",
                provider_config=direct, model_config={"runtime_model": "k3-256k", "protocol": "anthropic-compatible"},
                mode="execute", secret="selected")
        wrong_endpoint = {**direct, "base_url": "http://api.moonshot.ai/anthropic"}
        with self.assertRaisesRegex(claude.ClaudeAdapterError, "endpoint"):
            claude.build_transport_environment({}, provider="kimi", model="k3-256k",
                provider_config=wrong_endpoint, model_config=config, mode="execute", secret="selected")
        runner = mock.Mock()
        with self.assertRaisesRegex(claude.ClaudeAdapterError, "transport qualification"):
            claude.launch(executable="claude", repo="/not-used", worktree="/not-used", provider="kimi",
                model="k3-256k", provider_config=direct, model_config=config, prompt="task",
                secret="selected", runner=runner)
        runner.assert_not_called()

    def test_glm_requires_explicit_secret_and_uses_direct_gateway(self) -> None:
        config = {"runtime_model": "glm-5.3", "protocol": "anthropic-compatible"}
        with self.assertRaisesRegex(claude.ClaudeAdapterError, "credential is absent"):
            claude.build_transport_environment({}, provider="glm", model="glm-5.3", provider_config=self.glm, model_config=config, mode="execute")
        child = claude.build_transport_environment({"OPENROUTER_API_KEY": "old",
            "SIDE_LANE_CREDENTIAL_OTHER": "other-secret",
            "SIDE_LANE_CREDENTIALS_DIR": "/private/credentials"}, provider="glm",
            model="glm-5.3", provider_config=self.glm, model_config=config, mode="execute", secret="selected")
        self.assertEqual(child["ANTHROPIC_AUTH_TOKEN"], "selected")
        self.assertEqual(child["ANTHROPIC_BASE_URL"], "https://api.z.ai/api/anthropic")
        self.assertNotIn("OPENROUTER_API_KEY", child)
        self.assertNotIn("SIDE_LANE_CREDENTIAL_OTHER", child)
        self.assertNotIn("SIDE_LANE_CREDENTIALS_DIR", child)

    def test_mocked_launch_redacts_billable_secret_and_normalizes_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 7, "selected", "selected"))
            result = claude.launch(executable="claude", repo=repo, worktree=lane, provider="glm",
                model="glm-5.3", provider_config=self.glm,
                model_config={"runtime_model": "glm-5.3", "protocol": "anthropic-compatible"},
                prompt="task", secret="selected", runner=runner)
        self.assertEqual(result.gateway, "direct-zai")
        self.assertTrue(result.billable)
        self.assertNotIn("selected", result.stdout + result.stderr)

    def test_qualified_direct_launch_captures_stream_identity_and_usage(self) -> None:
        model = "kimi-k2.7-code"
        provider = {"gateway": "direct-kimi", "auth_method": "provider-key", "billable": True,
                    "base_url": "https://api.moonshot.cn/anthropic"}
        config = {"runtime_model": model, "protocol": "anthropic-compatible",
                  "identity_contract": {"requested_model": model, "resolved_model": model,
                                        "settings_precedence": "verified"},
                  "qualification": {"verified": True, "verified_on": "2026-09-11",
                                    "source": "mocked transport report"}}
        stdout = '\n'.join((json.dumps({"type": "system", "model": model}),
                            json.dumps({"type": "result", "usage": {"input_tokens": 9}})))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            result = claude.launch(executable="claude", repo=repo, worktree=lane,
                provider="kimi", model=model, provider_config=provider, model_config=config,
                prompt="task", secret="selected",
                runner=mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout, "")))
        self.assertEqual(result.resolved_model, model)
        self.assertEqual(result.usage, {"input_tokens": 9})
        self.assertIn("stream-json", result.argv)

    def test_qualified_direct_launch_fails_closed_on_unattested_or_wrong_identity(self) -> None:
        model = "kimi-k2.7-code"
        provider = {"gateway": "direct-kimi", "auth_method": "provider-key", "billable": True,
                    "base_url": "https://api.moonshot.cn/anthropic"}
        config = {"runtime_model": model, "protocol": "anthropic-compatible",
                  "identity_contract": {"requested_model": model, "resolved_model": model,
                                        "settings_precedence": "verified"},
                  "qualification": {"verified": True, "verified_on": "2026-09-11",
                                    "source": "mocked transport report"}}
        cases = ((json.dumps({"type": "result", "usage": {"input_tokens": 1}}),
                  "identity-unverified"),
                 (json.dumps({"type": "system", "model": "another-model"}),
                  "identity-mismatch"),
                 ("\n".join((json.dumps({"model": model}),
                              json.dumps({"model": "another-model"}))),
                  "identity-unverified"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            for stdout, error in cases:
                result = claude.launch(executable="claude", repo=repo, worktree=lane,
                    provider="kimi", model=model, provider_config=provider, model_config=config,
                    prompt="task", secret="selected",
                    runner=mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout, "")))
                self.assertEqual(result.returncode, 65)
                self.assertIn(error, result.stderr)

    def test_native_route_without_identity_contract_preserves_legacy_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            result = claude.launch(executable="claude", repo=repo, worktree=lane,
                provider="claude", model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude-readonly"},
                prompt="review", mode="review",
                runner=mock.Mock(return_value=subprocess.CompletedProcess([], 0, "review complete", "")))
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(result.resolved_model)

    def test_partial_stream_usage_deduplicates_messages_and_final_result_wins(self) -> None:
        partial = "\n".join((
            json.dumps({"message": {"id": "a", "usage": {"input_tokens": 10, "cache_read_input_tokens": 3}}}),
            json.dumps({"message": {"id": "a", "usage": {"input_tokens": 10, "cache_read_input_tokens": 7}}}),
            json.dumps({"message": {"id": "b", "usage": {"input_tokens": 4, "output_tokens": 2}}}),
        ))
        self.assertEqual(claude._stream_metadata(partial)[1], {
            "observation": "partial-stream", "input_tokens": 14,
            "cache_read_input_tokens": 7, "output_tokens": 2,
        })
        complete = partial + "\n" + json.dumps({"type": "result", "usage": {"input_tokens": 99}})
        self.assertEqual(claude._stream_metadata(complete)[1], {"input_tokens": 99})

    def test_glm_quota_pause_is_normalized_without_retry_or_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            runner = mock.Mock(return_value=subprocess.CompletedProcess(
                [], 9, "", "quota limit reached; available again in 3 hours"
            ))
            result = claude.launch(executable="claude", repo=repo, worktree=lane,
                provider="glm", model="glm-5.3", provider_config=self.glm,
                model_config={"runtime_model": "glm-5.3", "protocol": "anthropic-compatible"},
                prompt="task", secret="selected", runner=runner)
        self.assertEqual(result.availability, "temporarily-unavailable")
        runner.assert_called_once()

    @mock.patch("side_lane.adapters.claude.os.killpg")
    @mock.patch("side_lane.adapters.claude.subprocess.Popen")
    def test_bounded_launch_timeout_stops_process_group(self, popen: mock.Mock,
                                                        killpg: mock.Mock) -> None:
        process = popen.return_value
        process.pid = 77
        process.communicate.side_effect = [subprocess.TimeoutExpired("claude", 1),
                                           ("partial", "diagnostic")]
        completed = claude._bounded_process(["claude"], timeout=1)
        self.assertEqual(completed.returncode, 124)
        killpg.assert_called_once_with(77, claude.signal.SIGTERM)



class FirstPartyAnthropicKeyRouteTests(unittest.TestCase):
    provider = {"gateway": "direct-anthropic", "auth_method": "provider-key", "billable": True,
                "base_url": "https://api.anthropic.com"}

    def config(self, model: str, **overrides: object) -> dict:
        values = {"runtime_model": model, "protocol": "anthropic-compatible",
                  "identity_contract": {"requested_model": model, "resolved_model": model,
                                        "settings_precedence": "verified"}}
        values.update(overrides)
        return values

    def test_direct_anthropic_exports_api_key_without_bearer_token(self) -> None:
        child = claude.build_transport_environment(
            {"PATH": "/bin", "ANTHROPIC_API_KEY": "inherited", "ANTHROPIC_AUTH_TOKEN": "inherited"},
            provider="anthropic", model="claude-opus-5", provider_config=self.provider,
            model_config=self.config("claude-opus-5"), mode="execute", secret="selected")
        self.assertEqual(child["ANTHROPIC_API_KEY"], "selected")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", child)
        self.assertEqual(child["ANTHROPIC_BASE_URL"], "https://api.anthropic.com")
        for name in claude.EXACT_MODEL_ENV_NAMES:
            self.assertEqual(child[name], "claude-opus-5")

    def test_other_direct_gateways_keep_bearer_token_and_no_api_key(self) -> None:
        glm = {"gateway": "direct-zai", "auth_method": "provider-key", "billable": True,
               "base_url": "https://api.z.ai/api/anthropic"}
        child = claude.build_transport_environment({}, provider="glm", model="glm-5.3",
            provider_config=glm, model_config={"runtime_model": "glm-5.3", "protocol": "anthropic-compatible"},
            mode="execute", secret="selected")
        self.assertEqual(child["ANTHROPIC_AUTH_TOKEN"], "selected")
        self.assertNotIn("ANTHROPIC_API_KEY", child)
        kimi = {"gateway": "direct-kimi", "auth_method": "provider-key", "billable": True,
                "base_url": "https://api.moonshot.cn/anthropic"}
        child = claude.build_transport_environment({}, provider="kimi", model="kimi-k2.7-code",
            provider_config=kimi, model_config=self.config("kimi-k2.7-code"),
            mode="execute", secret="selected")
        self.assertEqual(child["ANTHROPIC_AUTH_TOKEN"], "selected")
        self.assertNotIn("ANTHROPIC_API_KEY", child)

    def test_direct_anthropic_without_identity_contract_is_rejected(self) -> None:
        with self.assertRaisesRegex(claude.ClaudeAdapterError, "identity contract"):
            claude.build_transport_environment({}, provider="anthropic", model="claude-opus-5",
                provider_config=self.provider,
                model_config={"runtime_model": "claude-opus-5", "protocol": "anthropic-compatible"},
                mode="execute", secret="selected")

    def test_direct_anthropic_review_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(claude.ClaudeAdapterError, "unqualified for review"):
            claude.build_transport_environment({}, provider="anthropic", model="claude-opus-5",
                provider_config=self.provider,
                model_config=self.config("claude-opus-5", protocol="anthropic-compatible-readonly"),
                mode="review", secret="selected")

    def test_launch_redacts_the_secret_exported_as_api_key(self) -> None:
        model = "claude-opus-5"
        config = self.config(model, qualification={"verified": True, "verified_on": "2026-09-17",
                                                   "source": "mocked transport report"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = root / "repo", root / "lane"
            for path in (repo, lane):
                path.mkdir()
                (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
            result = claude.launch(executable="claude", repo=repo, worktree=lane,
                provider="anthropic", model=model, provider_config=self.provider,
                model_config=config, prompt="task", secret="selected",
                runner=mock.Mock(return_value=subprocess.CompletedProcess(
                    [], 7, "leak selected", "leak selected")))
        self.assertNotIn("selected", result.stdout + result.stderr)

    def test_shipped_qualification_gate_admits_verified_models_and_rejects_unverified(self) -> None:
        models = json.loads((Path(__file__).parents[1] / "config/models.json").read_text(encoding="utf-8"))
        for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-fable-5-1"):
            with self.subTest(model=model):
                provider_config, model_config = cli.select_route(
                    models, "claude", "execute", "anthropic", model)
                qualification = model_config["qualification"]
                self.assertIs(qualification["verified"], True)
                self.assertTrue(qualification["verified_on"])
                self.assertTrue(qualification["source"])
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    repo, lane = root / "repo", root / "lane"
                    for path in (repo, lane):
                        path.mkdir()
                        (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
                    stdout = "\n".join((json.dumps({"type": "system", "model": model}),
                                        json.dumps({"type": "result", "usage": {"input_tokens": 1}})))
                    runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout, ""))
                    result = claude.launch(executable="claude", repo=repo, worktree=lane,
                        provider="anthropic", model=model, provider_config=provider_config,
                        model_config=model_config, prompt="task", secret="selected",
                        env={"PATH": "/bin"}, runner=runner)
                runner.assert_called_once()
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.resolved_model, model)
        # The gate itself: the same shipped route with its qualification flipped
        # back to unverified must be refused before any process starts.
        provider_config, model_config = cli.select_route(
            models, "claude", "execute", "anthropic", "claude-fable-5-1")
        unverified = {**model_config, "qualification": {**model_config["qualification"], "verified": False}}
        runner = mock.Mock()
        with self.assertRaisesRegex(claude.ClaudeAdapterError,
                                    "external route lacks verified model transport qualification"):
            claude.launch(executable="claude", repo="/not-used", worktree="/not-used",
                provider="anthropic", model="claude-fable-5-1", provider_config=provider_config,
                model_config=unverified, prompt="task", secret="selected", runner=runner)
        runner.assert_not_called()


class ProjectMcpServerApprovalTests(unittest.TestCase):
    """A capability grant approves exactly its project MCP server — nothing else."""

    native = {"gateway": "native-claude", "auth_method": "oauth", "billable": False}

    def command(self, mode: str, capabilities=()) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixed"
            repo, lane = root / "repo", root / "lane"
            for path in (repo, lane):
                path.mkdir(parents=True)
                (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
            protocol = "native-claude-readonly" if mode == "review" else "native-claude"
            return claude.build_command(executable="claude", repo=repo, worktree=lane,
                provider="claude", model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": protocol},
                prompt="task", mode=mode, capabilities=capabilities)

    def settings_of(self, command: list[str]) -> dict:
        return json.loads(command[command.index("--settings") + 1])

    def system_prompt_of(self, command: list[str]) -> str:
        return command[command.index("--append-system-prompt") + 1]

    def test_each_granted_capability_approves_only_its_exact_project_server(self) -> None:
        for capability, server in (
            ("playwright", "playwright"),
            ("gitnexus", "gitnexus"),
            ("codegraph", "codegraph"),
            ("slack-read", "slack"),
        ):
            with self.subTest(capability=capability):
                settings = self.settings_of(self.command("execute", (capability,)))
                self.assertEqual(settings["enabledMcpjsonServers"], [server])
                self.assertNotIn("enableAllProjectMcpServers", settings)
                self.assertNotIn("disabledMcpjsonServers", settings)

    def test_mixed_grants_approve_each_granted_server_once(self) -> None:
        settings = self.settings_of(self.command(
            "execute", ("slack-read", "gitnexus", "playwright", "codegraph")))
        self.assertEqual(
            settings["enabledMcpjsonServers"],
            ["codegraph", "gitnexus", "playwright", "slack"])
        repeated = self.settings_of(self.command("execute", ("gitnexus", "gitnexus")))
        self.assertEqual(repeated["enabledMcpjsonServers"], ["gitnexus"])

    def test_unrequested_servers_stay_unapproved(self) -> None:
        settings = self.settings_of(self.command("execute", ("shell",)))
        self.assertNotIn("enabledMcpjsonServers", settings)
        granted = self.settings_of(self.command("execute", ("gitnexus",)))
        self.assertEqual(granted["enabledMcpjsonServers"], ["gitnexus"])
        for unrequested in ("slack", "playwright", "codegraph"):
            self.assertNotIn(unrequested, granted["enabledMcpjsonServers"])

    def test_graph_and_slack_grants_do_not_trigger_the_readiness_probe(self) -> None:
        readiness = mock.Mock()
        worker = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "done", ""))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = root / "repo", root / "lane"
            for path in (repo, lane):
                path.mkdir()
                (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
            claude.launch(executable="claude", repo=repo, worktree=lane,
                provider="claude", model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude"},
                prompt="task", capabilities=("gitnexus", "codegraph", "slack-read"),
                env={"PATH": "/bin"}, runner=worker, readiness_runner=readiness)
        readiness.assert_not_called()
        worker.assert_called_once()
        # Playwright keeps its pre-launch probe even alongside other grants.
        probe = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 0, "playwright:\n  Status: ✔ Connected\n", ""))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = root / "repo", root / "lane"
            for path in (repo, lane):
                path.mkdir()
                (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
            claude.launch(executable="claude", repo=repo, worktree=lane,
                provider="claude", model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude"},
                prompt="task", capabilities=("playwright", "slack-read"),
                env={"PATH": "/bin"}, runner=worker, readiness_runner=probe)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(probe.call_args.args[0][-3:], ["mcp", "get", "playwright"])
        approved = self.settings_of(list(worker.call_args.args[0]))
        self.assertEqual(
            approved["enabledMcpjsonServers"], ["playwright", "slack"])

    def test_cm_services_capabilities_use_the_fixed_user_scope_server(self) -> None:
        # asana-read/drive-read map to the fixed user-global cm-services
        # registration, which is not a project .mcp.json entry — so no
        # enabledMcpjsonServers approval is emitted for it.
        for capabilities in (("asana-read",), ("drive-read",), ("gcloud-read",), ("database-read",), ("algolia-read",), ("asana-read", "drive-read", "gcloud-read", "database-read", "algolia-read")):
            with self.subTest(capabilities=capabilities):
                settings = self.settings_of(self.command("execute", capabilities))
                self.assertNotIn("cm-services", settings.get("enabledMcpjsonServers", []))
        # A project-scoped grant alongside still approves only its own server.
        settings = self.settings_of(self.command("execute", ("asana-read", "gitnexus")))
        self.assertEqual(settings["enabledMcpjsonServers"], ["gitnexus"])

    def test_cm_services_probe_runs_once_per_server_not_per_capability(self) -> None:
        probe = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 0, "cm-services:\n  Status: ✔ Connected\n", ""))
        worker = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "done", ""))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = root / "repo", root / "lane"
            for path in (repo, lane):
                path.mkdir()
                (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
            claude.launch(executable="claude", repo=repo, worktree=lane,
                provider="claude", model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude"},
                prompt="task", capabilities=("asana-read", "drive-read", "gcloud-read", "database-read", "algolia-read"),
                env={"PATH": "/bin"}, runner=worker, readiness_runner=probe)
        # Both capabilities share cm-services: one probe, by bare name — a
        # user-scope registration takes no project-server approval settings.
        probe.assert_called_once()
        argv = probe.call_args.args[0]
        self.assertEqual(argv[-3:], ["mcp", "get", "cm-services"])
        self.assertNotIn("--settings", argv)
        prompt = self.system_prompt_of(list(worker.call_args.args[0]))
        self.assertEqual(prompt.count("cm-services startup"), 1)
        self.assertIn('servers: ["cm-services"]', prompt)
        self.assertIn("presence evidence only", prompt)
        # The instruction names only the granted tools, not the whole server.
        self.assertIn("mcp__cm-services__asana_get_task", prompt)
        self.assertIn("mcp__cm-services__drive_doc_get", prompt)
        self.assertIn("mcp__cm-services__gcp_menu", prompt)
        self.assertIn("mcp__cm-services__postgres_select", prompt)
        self.assertIn("mcp__cm-services__algolia_get_settings", prompt)

    def test_cm_services_probe_failure_fails_closed_before_the_worker(self) -> None:
        probe = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 1, "cm-services:\n  Status: ✘ failed\n", ""))
        worker = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = root / "repo", root / "lane"
            for path in (repo, lane):
                path.mkdir()
                (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
            with self.assertRaisesRegex(claude.ClaudeAdapterError, "not ready"):
                claude.launch(executable="claude", repo=repo, worktree=lane,
                    provider="claude", model="claude-sonnet-5", provider_config=self.native,
                    model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude"},
                    prompt="task", capabilities=("asana-read",),
                    env={"PATH": "/bin"}, runner=worker, readiness_runner=probe)
        worker.assert_not_called()

    def test_graph_startup_instruction_names_exact_servers_and_wait(self) -> None:
        prompt = self.system_prompt_of(self.command("execute", ("gitnexus",)))
        self.assertIn("Claude Code code-graph startup", prompt)
        self.assertIn('servers: ["gitnexus"]', prompt)
        self.assertIn("mcp__<server>__", prompt)
        self.assertNotIn('servers: ["slack"]', prompt)
        both = self.system_prompt_of(self.command("execute", ("codegraph", "gitnexus")))
        # Canonical order regardless of the grant tuple's order.
        self.assertIn('["gitnexus", "codegraph"]', both)
        plain = self.system_prompt_of(self.command("execute", ("shell",)))
        self.assertNotIn("code-graph startup", plain)

    def test_slack_startup_instruction_waits_without_claiming_read_proof(self) -> None:
        prompt = self.system_prompt_of(self.command("execute", ("slack-read",)))
        self.assertIn('servers: ["slack"]', prompt)
        self.assertIn("presence evidence only", prompt)
        self.assertIn("never\nproof of authentication", prompt)

    def test_review_mode_stays_isolated_from_project_server_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixed"
            repo, lane = root / "repo", root / "lane"
            for path in (repo, lane):
                path.mkdir(parents=True)
                (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
            def review(capabilities=()) -> list[str]:
                return claude.build_command(executable="claude", repo=repo, worktree=lane,
                    provider="claude", model="claude-sonnet-5", provider_config=self.native,
                    model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude-readonly"},
                    prompt="task", mode="review", capabilities=capabilities)
            granted = review(("playwright", "gitnexus", "codegraph", "slack-read"))
            plain = review()
        self.assertEqual(granted, plain)
        self.assertNotIn("--settings", granted)
        self.assertNotIn("--allowedTools", granted)
        self.assertIn("--strict-mcp-config", granted)
        self.assertIn('{"mcpServers":{}}', granted)
        for item in granted:
            self.assertNotIn("enabledMcpjsonServers", item)
        prompt = self.system_prompt_of(granted)
        for absent in ("Playwright startup", "Slack read startup", "code-graph startup"):
            self.assertNotIn(absent, prompt)

    def test_native_execute_settings_keep_native_auth_shape(self) -> None:
        settings = self.settings_of(self.command("execute", ("gitnexus",)))
        self.assertEqual(
            settings,
            {"sandbox": {"enabled": False}, "enabledMcpjsonServers": ["gitnexus"]})


class AllowedToolsTests(unittest.TestCase):
    native = {"gateway": "native-claude", "auth_method": "oauth", "billable": False}

    def command(self, mode: str, capabilities=()) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixed"
            repo, lane = root / "repo", root / "lane"
            for path in (repo, lane):
                path.mkdir(parents=True)
                (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
            protocol = "native-claude-readonly" if mode == "review" else "native-claude"
            return claude.build_command(executable="claude", repo=repo, worktree=lane,
                provider="claude", model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": protocol},
                prompt="task", mode=mode, capabilities=capabilities)

    def test_review_never_receives_an_allowlist(self) -> None:
        self.assertEqual(claude.allowed_tools("review", ("shell", "git-push")), ())
        self.assertEqual(claude.disallowed_tools("review", ("git-push",)), ())
        command = self.command("review", ("shell",))
        self.assertNotIn("--allowedTools", command)
        self.assertNotIn("--disallowedTools", command)
        strip = lambda argv: [item for item in argv if not item.startswith("/")]
        self.assertEqual(strip(command), strip(self.command("review")))

    def test_execute_without_shell_gets_only_file_tools(self) -> None:
        tools = claude.allowed_tools("execute", ())
        self.assertEqual(tools, ("Read", "Edit", "Write", "Glob", "Grep"))
        self.assertFalse(any(tool.startswith("Bash(") for tool in tools))

    def test_graph_capabilities_grant_read_only_mcp_tools_in_execute(self) -> None:
        gitnexus = tuple(f"mcp__gitnexus__{name}" for name in (
            "api_impact", "check", "context", "cypher", "detect_changes", "explain",
            "group_list", "impact", "list_repos", "pdg_query", "query", "route_map",
            "shape_check", "tool_map", "trace"))
        codegraph = tuple(f"mcp__codegraph__{name}" for name in (
            "find_symbol", "find_callers", "find_callees", "find_importers",
            "neighbors", "impact_of", "path_between"))
        base = claude.allowed_tools("execute", ())
        self.assertEqual(claude.allowed_tools("execute", ("gitnexus",)), base + ("WaitForMcpServers",) + gitnexus)
        self.assertEqual(claude.allowed_tools("execute", ("codegraph",)), base + ("WaitForMcpServers",) + codegraph)
        self.assertEqual(claude.allowed_tools("execute", ("gitnexus", "codegraph")), base + ("WaitForMcpServers",) + gitnexus + codegraph)
        # Index-mutating GitNexus tools stay ungranted; no wildcard grants.
        for tool in claude.allowed_tools("execute", ("gitnexus", "codegraph")):
            self.assertNotRegex(tool, r"rename|group_sync|analyze|clean|__\*$")
        self.assertFalse(any(tool.startswith("mcp__") for tool in base))
        self.assertEqual(claude.allowed_tools("review", ("gitnexus", "codegraph")), ())
        self.assertEqual(claude.disallowed_tools("execute", ("gitnexus", "codegraph")), ())

    def test_slack_read_grants_exact_read_only_tools_and_keeps_review_strict(self) -> None:
        slack = ("mcp__slack__slack_read_thread", "mcp__slack__slack_read_channel")
        base = claude.allowed_tools("execute", ())
        self.assertEqual(claude.allowed_tools("execute", ("slack-read",)), base + ("WaitForMcpServers",) + slack)
        granted = [tool for tool in claude.allowed_tools("execute", ("slack-read",))
                   if tool.startswith("mcp__slack__")]
        self.assertEqual(sorted(granted), sorted(slack))
        self.assertNotIn("mcp__slack__*", claude.allowed_tools("execute", ("slack-read",)))
        # Review never receives the grants regardless of the capability.
        self.assertEqual(claude.allowed_tools("review", ("slack-read",)), ())
        self.assertEqual(claude.disallowed_tools("review", ("slack-read",)), ())
        # CLI propagation: the exact tool IDs reach the rendered argv, and the
        # execute system prompt carries the slack-read startup instruction.
        command = self.command("execute", ("slack-read",))
        for tool in slack:
            self.assertIn(tool, command)
        prompt = command[command.index("--append-system-prompt") + 1]
        self.assertIn("slack_read_thread", prompt)
        self.assertIn("Slack read startup", prompt)
        review = self.command("review", ("slack-read",))
        self.assertNotIn("--allowedTools", review)
        review_prompt = review[review.index("--append-system-prompt") + 1]
        self.assertNotIn("Slack read startup", review_prompt)
        plain = self.command("execute")
        plain_prompt = plain[plain.index("--append-system-prompt") + 1]
        self.assertNotIn("Slack read startup", plain_prompt)

    def test_cm_services_grants_are_exact_and_disjoint(self) -> None:
        asana = tuple(f"mcp__cm-services__{name}" for name in (
            "asana_get_task", "asana_get_project", "asana_list_project_tasks"))
        drive = tuple(f"mcp__cm-services__{name}" for name in (
            "drive_file_info", "drive_sheet_tabs", "drive_sheet_get", "drive_doc_get"))
        gcp = tuple(f"mcp__cm-services__{name}" for name in (
            "gcp_logs", "gcp_run_services", "gcp_run_jobs", "gcp_scheduler_jobs",
            "gcp_functions", "gcp_billing_mtd", "gcp_billing_daily", "gcp_menu"))
        database = ("mcp__cm-services__postgres_select",)
        algolia = ("mcp__cm-services__algolia_get_settings",)
        base = claude.allowed_tools("execute", ())
        self.assertEqual(
            claude.allowed_tools("execute", ("asana-read",)),
            base + ("WaitForMcpServers",) + asana)
        self.assertEqual(
            claude.allowed_tools("execute", ("drive-read",)),
            base + ("WaitForMcpServers",) + drive)
        self.assertEqual(
            claude.allowed_tools("execute", ("gcloud-read",)),
            base + ("WaitForMcpServers",) + gcp)
        self.assertEqual(
            claude.allowed_tools("execute", ("database-read",)),
            base + ("WaitForMcpServers",) + database)
        self.assertEqual(
            claude.allowed_tools("execute", ("algolia-read",)),
            base + ("WaitForMcpServers",) + algolia)
        both = claude.allowed_tools("execute", ("asana-read", "drive-read"))
        self.assertEqual(both, base + ("WaitForMcpServers",) + asana + drive)
        # Granting one capability never unlocks the other's tools on the
        # shared server, and no rule anywhere is a server-wide wildcard.
        for tool in both:
            self.assertNotRegex(tool, r"__\*$")
        self.assertFalse(any(tool.startswith("mcp__cm-services__") for tool in base))
        self.assertEqual(claude.allowed_tools("review", ("asana-read", "drive-read")), ())
        # The startup instruction appears once and names the exact server.
        prompt = self.command("execute", ("asana-read",))[
            self.command("execute", ("asana-read",)).index("--append-system-prompt") + 1]
        self.assertIn("cm-services startup", prompt)
        self.assertIn("mcp__cm-services__asana_get_task", prompt)
        self.assertNotIn("mcp__cm-services__drive_doc_get", prompt)

    def test_shell_or_workspace_write_adds_ordinary_dev_commands_not_push(self) -> None:
        for capability in ("shell", "workspace-write"):
            tools = claude.allowed_tools("execute", (capability,))
            for expected in ("Bash(pnpm *)", "Bash(npx *)", "Bash(npm *)", "Bash(node *)",
                             "Bash(./node_modules/.bin/*)", "Bash(uv *)", "Bash(uvx *)",
                             "Bash(python3 *)", "Bash(python3.11 *)", "Bash(git add *)",
                             "Bash(git commit *)", "Bash(ln *)", "Bash(cp *)", "Bash(mkdir *)",
                             "Bash(ls *)", "Bash(cat *)", "Bash(gh pr view *)", "Bash(gh pr list *)",
                             "Bash(gh pr diff *)", "Bash(gh run view *)"):
                self.assertIn(expected, tools)
            self.assertNotIn("Bash(git push *)", tools)
            self.assertFalse(any("gcloud" in tool or "gh pr merge" in tool or "deploy" in tool for tool in tools))
        self.assertEqual(claude.allowed_tools("execute", ("shell",)), claude.allowed_tools("execute", ("shell", "shell")))

    def test_git_push_requires_shell_and_denies_force(self) -> None:
        tools = claude.allowed_tools("execute", ("git-push",))
        self.assertIn("Bash(git push *)", tools)
        self.assertIn("Bash(git add *)", tools)
        self.assertEqual(claude.disallowed_tools("execute", ("git-push",)),
                         ("Bash(git push --force*)", "Bash(git push -f*)", "Bash(git push * --force*)", "Bash(git push * -f*)", "Bash(git push --force-with-lease*)", "Bash(git push * --force-with-lease*)", "Bash(git push --mirror*)", "Bash(git push * --mirror*)", "Bash(git push +*)", "Bash(git push * +*)"))
        self.assertEqual(claude.disallowed_tools("execute", ("shell",)), ())

    def test_unknown_capability_fails_closed(self) -> None:
        for mode in ("execute", "review"):
            with self.assertRaisesRegex(claude.ClaudeAdapterError, "unknown capability"):
                claude.allowed_tools(mode, ("sudo",))

    def test_execute_argv_carries_each_tool_separately(self) -> None:
        command = self.command("execute", ("shell", "git-push"))
        allowed = [command[index + 1] for index, flag in enumerate(command) if flag == "--allowedTools"]
        self.assertEqual(tuple(allowed), claude.allowed_tools("execute", ("shell", "git-push")))
        denied = [command[index + 1] for index, flag in enumerate(command) if flag == "--disallowedTools"]
        self.assertEqual(tuple(denied), claude.disallowed_tools("execute", ("git-push",)))
        self.assertIn("acceptEdits", command)
        self.assertIn("Injected canonical side-lane governance", command[-1])

    def test_launch_forwards_capabilities(self) -> None:
        runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout="ok", stderr=""))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = root / "repo", root / "lane"
            for path in (repo, lane):
                path.mkdir()
                (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
            result = claude.launch(executable="claude", repo=repo, worktree=lane, provider="claude",
                model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude"},
                prompt="task", mode="execute", capabilities=("shell",), env={"PATH": "/bin"}, runner=runner)
        self.assertIn("Bash(pnpm *)", result.argv)
        self.assertEqual(result.allowed_tools, claude.allowed_tools("execute", ("shell",)))
        self.assertIn("allowed_tools", result.as_dict())


class ReportOnlyModeTests(unittest.TestCase):
    """`--report-only` adds a same-invocation Stop hook and nothing else.

    The opt-in exists because a cloud worker navigated, saved its screenshot,
    ended its turn with exit 0, and reported a report it never wrote. Prose
    was the only artifact. The repair is a deterministic Stop hook inside the
    same Claude Code invocation: it blocks one stop and feeds the same model
    loop a reason to write the report, then allows the stop.
    """

    native = {"gateway": "native-claude", "auth_method": "oauth", "billable": False}
    REPORT_NAME = "SIDE_LANE_REPORT.md"

    def repo(self, root: Path, name: str) -> Path:
        path = root / name
        path.mkdir()
        (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
        return path

    def routed_provider_config(self) -> dict:
        return {"gateway": "omniroute-router", "auth_method": "provider-key",
                "billable": True, "base_url": "https://omniroute.example"}

    def routed_model_config(self, **overrides) -> dict:
        config = {"runtime_model": "routed-selector",
                  "protocol": "anthropic-compatible",
                  "qualification": {"verified": True, "verified_on": "2026-09-19",
                                    "source": "receipt"},
                  "routing_policy_contract": {
                      "requested_selector": "routed-selector",
                      "allowed_upstream_models": ["deepseek-flash"],
                      "settings_precedence": "verified",
                  }}
        config.update(overrides)
        return config

    def native_model_config(self, **overrides) -> dict:
        config = {"runtime_model": "claude-sonnet-5", "protocol": "native-claude"}
        config.update(overrides)
        return config

    def command(self, repo: Path, lane: Path, *, model_config=None, **kwargs) -> list[str]:
        return claude.build_command(
            executable="claude", repo=repo, worktree=lane, provider="claude",
            model="claude-sonnet-5", provider_config=self.native,
            model_config=model_config or self.native_model_config(max_budget_usd=2.5),
            prompt="task", mode="execute", **kwargs,
        )

    def launch_settings(self, command: list[str]) -> dict:
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertIsInstance(settings, dict)
        return settings

    def stop_hook_entry(self, command: list[str]) -> dict:
        settings = self.launch_settings(command)
        stop = settings.get("hooks", {}).get("Stop")
        self.assertIsInstance(stop, list)
        self.assertEqual(len(stop), 1)
        return stop[0]["hooks"][0]

    def hook_argv(self, command: list[str]) -> list[str]:
        return shlex.split(self.stop_hook_entry(command)["command"])

    # --- the hook is present exactly when opted in ---------------------------

    def test_opt_in_installs_one_stop_hook_with_the_fixed_report_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            command = self.command(repo, lane, report_only=True)
        entry = self.stop_hook_entry(command)
        self.assertEqual(entry["type"], "command")
        self.assertEqual(entry["timeout"], claude.REPORT_ONLY_HOOK_TIMEOUT_SECONDS)
        argv = self.hook_argv(command)
        # An unresolved relative module path would make the interpreter exit 2
        # and a non-zero hook exit blocks every stop.
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(Path(argv[1]).is_absolute())
        self.assertEqual(argv[1], str(Path(argv[1]).resolve()))
        self.assertEqual(argv[2], "--settings")
        self.assertEqual(json.loads(argv[3])["report_path"],
                         str((lane / self.REPORT_NAME).resolve()))

    def test_default_commands_carry_no_stop_hook(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            for provider_config, model_config, extra in (
                (self.native, self.native_model_config(), {}),
                (self.routed_provider_config(),
                 self.routed_model_config(max_budget_usd=2.5), {}),
                (self.native, {"runtime_model": "claude-sonnet-5",
                               "protocol": "native-claude-readonly"}, {"mode": "review"}),
            ):
                with self.subTest(extra=extra, routed=provider_config is not self.native):
                    command = claude.build_command(
                        executable="claude", repo=repo, worktree=lane,
                        provider="omniroute" if provider_config is not self.native else "claude",
                        model="routed-selector" if provider_config is not self.native else "claude-sonnet-5",
                        provider_config=provider_config, model_config=model_config,
                        prompt="task", **extra,
                    )
                    self.assertFalse(any("report_stop_hook" in part for part in command))
                    if "--settings" in command:
                        self.assertNotIn("hooks", self.launch_settings(command))

    def test_routed_opt_in_hook_survives_the_isolated_config_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            home = root / "home"
            home.mkdir()
            observed: dict[str, object] = {}

            def runner(command, **kwargs):
                child_config = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
                observed["command"] = list(command)
                observed["settings"] = json.loads((child_config / "settings.json").read_text())
                return subprocess.CompletedProcess(
                    command, 0,
                    '{"type":"result","subtype":"success","model":"deepseek-flash",'
                    '"usage":{"input_tokens":1,"output_tokens":1}}\n', "")

            claude.launch(
                executable="claude", repo=repo, worktree=lane, provider="omniroute",
                model="routed-selector", provider_config=self.routed_provider_config(),
                model_config=self.routed_model_config(max_budget_usd=1.0),
                prompt="task", env={"HOME": str(home), "PATH": "/bin"},
                secret="router-secret", runner=runner, report_only=True,
            )
        # The Stop hook is process-local: it rides the run's own --settings
        # payload, never the disposable config home's files.
        command = observed["command"]
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertEqual(settings["hooks"]["Stop"][0]["hooks"][0]["type"], "command")
        self.assertNotIn("Stop", observed["settings"].get("hooks", {}))
        self.assertIn("--max-budget-usd", command)

    def test_opt_in_preserves_every_other_execute_control(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            plain = self.command(repo, lane, capabilities=("shell",))
            opted = self.command(repo, lane, capabilities=("shell",), report_only=True)
        # Same argv but for the settings payload: the opt-in must not widen
        # tools, permission mode, MCP handling, or the model selector.
        def without_settings(argv: list[str]) -> list[str]:
            index = argv.index("--settings")
            return argv[:index] + argv[index + 2:]

        self.assertEqual(without_settings(plain), without_settings(opted))
        allowed = [part for part in opted if part.startswith("Bash(")]
        self.assertIn("Bash(git commit *)", allowed)
        self.assertNotIn("Bash(git push *)", allowed)

    # --- fail closed before any model launch ---------------------------------

    def test_opt_in_requires_a_finite_positive_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            for label, value in (("missing", None), ("zero", 0), ("negative", -1),
                                 ("infinite", float("inf")), ("nan", float("nan")),
                                 ("text", "much"), ("true", True)):
                with self.subTest(label=label):
                    model_config = self.native_model_config()
                    if value is not None:
                        model_config["max_budget_usd"] = value
                    with self.assertRaises(claude.ClaudeAdapterError):
                        self.command(repo, lane, model_config=model_config,
                                     report_only=True)

    def test_opt_in_budget_is_forwarded_as_the_usd_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            command = self.command(repo, lane, report_only=True)
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "2.5")

    def test_opt_in_is_rejected_outside_execute_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            config = {"runtime_model": "claude-sonnet-5",
                      "protocol": "native-claude-readonly", "max_budget_usd": 2.5}
            for mode in ("review", "plan"):
                with self.subTest(mode=mode):
                    with self.assertRaises(claude.ClaudeAdapterError):
                        claude.build_command(
                            executable="claude", repo=repo, worktree=lane,
                            provider="claude", model="claude-sonnet-5",
                            provider_config=self.native, model_config=config,
                            prompt="task", mode=mode, report_only=True,
                        )

    def test_launch_fails_before_the_model_when_the_budget_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "done", ""))
            with self.assertRaises(claude.ClaudeAdapterError):
                claude.launch(
                    executable="claude", repo=repo, worktree=lane, provider="claude",
                    model="claude-sonnet-5", provider_config=self.native,
                    model_config=self.native_model_config(), prompt="task",
                    mode="execute", env={"PATH": "/bin"}, runner=runner,
                    report_only=True,
                )
        runner.assert_not_called()

    def test_opt_in_runs_the_model_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            runner = mock.Mock(
                return_value=subprocess.CompletedProcess([], 0, "done", ""))
            claude.launch(
                executable="claude", repo=repo, worktree=lane, provider="claude",
                model="claude-sonnet-5", provider_config=self.native,
                model_config=self.native_model_config(max_budget_usd=2.5),
                prompt="task", mode="execute", env={"PATH": "/bin"}, runner=runner,
                report_only=True,
            )
        self.assertEqual(runner.call_count, 1)
        # One subprocess, one timeout: the opt-in adds no estimate, no second
        # invocation, and no resume/continue of the same worker.
        self.assertEqual(runner.call_args.kwargs["timeout"], 1800)
        command = runner.call_args.args[0]
        self.assertEqual(command.count("-p"), 1)

    # --- the installed hook actually protects the run -------------------------

    def _run_installed_hook(self, command: list[str], stdin: dict) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            self.hook_argv(command), input=json.dumps(stdin).encode("utf-8"),
            capture_output=True, timeout=10,
        )

    def test_installed_hook_blocks_the_stop_when_the_report_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            command = self.command(repo, lane, report_only=True)
            result = self._run_installed_hook(command, {
                "hook_event_name": "Stop", "cwd": str(repo), "stop_hook_active": False})
            self.assertEqual(result.returncode, 0)
            decision = json.loads(result.stdout.decode("utf-8"))
            self.assertEqual(decision["decision"], "block")
            self.assertIn("report", decision["reason"].lower())
            # A first block feeds the same loop; the second round is bounded.
            second = self._run_installed_hook(command, {
                "hook_event_name": "Stop", "cwd": str(repo), "stop_hook_active": True})
            self.assertEqual(second.returncode, 0)
            self.assertEqual(second.stdout.strip(), b"")

    def test_installed_hook_allows_the_stop_once_the_report_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            command = self.command(repo, lane, report_only=True)
            (lane / self.REPORT_NAME).write_text("# Findings\n- item\n", encoding="utf-8")
            result = self._run_installed_hook(command, {
                "hook_event_name": "Stop", "cwd": str(repo), "stop_hook_active": False})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), b"")

    def test_installed_hook_is_shell_safe_for_awkward_worktree_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "lane with 'quotes' & $(spaces)"
            root.mkdir()
            repo = self.repo(Path(directory), "repo")
            lane = root
            (lane / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
            command = self.command(repo, lane, report_only=True)
            argv = self.hook_argv(command)
            self.assertEqual(argv[1], str(Path(report_stop_hook.__file__).resolve()))
            result = self._run_installed_hook(command, {
                "hook_event_name": "Stop", "cwd": "/", "stop_hook_active": False})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout.decode("utf-8"))["decision"], "block")


if __name__ == "__main__":
    unittest.main()
