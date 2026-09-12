import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from side_lane.adapters import devin


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
        self.assertFalse(config["subagents_enabled"])
        self.assertEqual(config["agent"]["model"], "swe-2-medium")
        self.assertIn("Exec(python3)", config["permissions"]["allow"])
        self.assertIn("Exec(git status)", config["permissions"]["allow"])
        self.assertIn("Exec(git commit)", config["permissions"]["allow"])
        self.assertNotIn("Exec(git)", config["permissions"]["allow"])
        self.assertFalse(any("*" in rule for rule in config["permissions"]["allow"]
                             if rule.startswith("Exec(")))
        self.assertIn("mcp__playwright__browser_navigate", config["permissions"]["allow"])
        self.assertIn("mcp__playwright__browser_click", config["permissions"]["allow"])
        self.assertIn("mcp__playwright__browser_find", config["permissions"]["allow"])
        self.assertNotIn("mcp__playwright__*", config["permissions"]["allow"])

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
        self.assertEqual(policy_hook["matcher"], "^exec$")
        self.assertEqual(policy_hook["hooks"][0]["command"], "python policy.py rules.json")

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
