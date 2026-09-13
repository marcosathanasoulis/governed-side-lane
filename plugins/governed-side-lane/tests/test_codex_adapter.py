from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from side_lane.adapters import codex


class CodexAdapterTests(unittest.TestCase):
    provider = {"gateway": "native-codex", "auth_method": "oauth", "billable": False}
    execute = {"runtime_model": "gpt-5.6-terra", "protocol": "native-codex"}
    review = {"runtime_model": "gpt-5.6-terra", "protocol": "native-codex-readonly"}

    def repo(self, root: Path, name: str) -> Path:
        path = root / name
        path.mkdir()
        (path / ".git").mkdir()
        return path

    def test_native_execute_and_review_commands_inject_canonical_governance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            execute = codex.build_codex_command("codex", repo, lane, "openai", "gpt-5.6-terra", self.provider, self.execute, "task")
            review = codex.build_codex_command("codex", repo, lane, "openai", "gpt-5.6-terra", self.provider, self.review, "review", mode="review")
        self.assertEqual(execute[execute.index("-s") + 1], "danger-full-access")
        self.assertEqual(review[review.index("-s") + 1], "read-only")
        self.assertIn("mcp_servers={}", review)
        self.assertEqual(execute[:3], ("codex", "exec", "--json"))
        self.assertEqual(review[:3], ("codex", "exec", "--json"))
        self.assertIn("Injected canonical side-lane governance", execute[-1])
        self.assertNotIn("INPROCESS.md", execute[-1])

    def test_rejects_non_native_or_model_substitution_and_main_checkout_execute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repo(Path(directory), "repo")
            with self.assertRaises(codex.CodexAdapterError):
                codex.build_codex_command("codex", repo, repo, "glm", "gpt-5.6-terra", self.provider, self.execute, "task")
            with self.assertRaises(codex.CodexAdapterError):
                codex.build_codex_command("codex", repo, repo, "openai", "gpt-5.6-terra", self.provider, {**self.execute, "runtime_model": "gpt-5.6-sol"}, "task")
            with self.assertRaisesRegex(codex.CodexAdapterError, "dedicated worktree"):
                codex.build_codex_command("codex", repo, repo, "openai", "gpt-5.6-terra", self.provider, self.execute, "task")

    def test_oauth_environment_scrubs_all_provider_keys(self) -> None:
        child = codex.build_child_env({"PATH": "/bin", "GOOGLE_APPLICATION_CREDENTIALS": "/adc",
            "OPENAI_API_KEY": "x", "ANTHROPIC_CUSTOM": "y", "GLM_API_KEY": "z",
            "CODEX_API_KEY": "inherited-never", "CODEX_ACCESS_TOKEN": "token-never",
            "CODEX_HOME": "/home/dev/.codex",
            "SIDE_LANE_CREDENTIAL_OTHER": "other-secret",
            "SIDE_LANE_CREDENTIALS_DIR": "/private/credentials"})
        self.assertEqual(child, {"PATH": "/bin", "GOOGLE_APPLICATION_CREDENTIALS": "/adc",
            "CODEX_HOME": "/home/dev/.codex"})

    def test_native_route_scrubs_inherited_codex_api_key_and_keeps_codex_home(self) -> None:
        child = codex.build_transport_environment(
            {"PATH": "/bin", "CODEX_API_KEY": "inherited-never", "CODEX_HOME": "/home/dev/.codex"},
            provider="openai", model="gpt-5.6-terra", provider_config=self.provider,
            model_config=self.execute, mode="execute")
        self.assertNotIn("CODEX_API_KEY", child)
        self.assertNotIn("OPENAI_API_KEY", child)
        self.assertEqual(child["CODEX_HOME"], "/home/dev/.codex")

    def test_mocked_run_returns_normalized_result_without_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
            result = codex.run_codex(executable="codex", repo=repo, worktree=lane,
                provider="openai", model="gpt-5.6-terra", provider_config=self.provider,
                model_config=self.execute, prompt="task", env={"OPENAI_API_KEY": "never"}, runner=runner)
        self.assertEqual(result.gateway, "native-codex")
        self.assertEqual(result.auth_method, "oauth")
        self.assertFalse(result.billable)
        self.assertNotIn("OPENAI_API_KEY", runner.call_args.kwargs["env"])
        self.assertIn("--json", result.argv)
        self.assertIsNone(result.usage)
        self.assertIsNone(result.resolved_model)

    def test_mocked_run_captures_usage_from_json_event_stream(self) -> None:
        stdout = "\n".join((
            "not json at all",
            '{"type":"thread.started","thread_id":"t1","model":"gpt-5.6-terra"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
            '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}',
            "{truncated",
            '{"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":0,"output_tokens":45}}',
            "",
        ))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout, ""))
            result = codex.run_codex(executable="codex", repo=repo, worktree=lane,
                provider="openai", model="gpt-5.6-terra", provider_config=self.provider,
                model_config=self.execute, prompt="task", env={"PATH": "/bin"}, runner=runner)
        self.assertEqual(result.usage["input_tokens"], 120)
        self.assertEqual(result.usage["output_tokens"], 45)
        self.assertEqual(result.usage["cached_input_tokens"], 0)
        self.assertEqual(result.resolved_model, "gpt-5.6-terra")
        self.assertEqual(result.stdout, stdout)

    def test_stream_metadata_ignores_non_json_and_malformed_usage(self) -> None:
        self.assertEqual(codex._stream_metadata("plain text\n\n"), (None, None))
        self.assertEqual(codex._stream_metadata('{"type":"turn.completed","usage":"n/a"}'), (None, None))
        self.assertEqual(codex._stream_metadata("[1, 2]\n42\n"), (None, None))
        self.assertEqual(
            codex._stream_metadata('{"type":"turn.completed","usage":{"input_tokens":3}}'),
            (None, {"input_tokens": 3}),
        )



class CodexSupportDirTests(unittest.TestCase):
    execute = {"runtime_model": "gpt-5.6-sol", "protocol": "native-codex"}
    native = {"gateway": "native-codex", "auth_method": "oauth", "billable": False}

    def test_run_codex_prepends_support_dir_to_child_path(self) -> None:
        runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = root / "repo", root / "lane"
            for path in (repo, lane):
                path.mkdir()
                (path / ".git").mkdir()
            codex.run_codex(executable="/bundle/codex", repo=repo, worktree=lane, provider="openai",
                model="gpt-5.6-sol", provider_config=self.native, model_config=self.execute,
                prompt="task", env={"PATH": "/usr/bin", "OPENAI_API_KEY": "never"},
                support_dir="/bundle", runner=runner)
        child = runner.call_args.kwargs["env"]
        self.assertEqual(child["PATH"], "/bundle:/usr/bin")
        self.assertNotIn("OPENAI_API_KEY", child)


class CodexApiKeyRouteTests(unittest.TestCase):
    provider = {"gateway": "codex-api-key", "auth_method": "provider-key", "billable": True,
        "credential_service": "example-side-lane-openai", "base_url": "https://api.openai.com/v1/"}
    native = {"gateway": "native-codex", "auth_method": "oauth", "billable": False}
    execute = {"runtime_model": "gpt-5.6-terra", "protocol": "native-codex"}
    review = {"runtime_model": "gpt-5.6-terra", "protocol": "native-codex-readonly"}

    def repo(self, root: Path, name: str) -> Path:
        path = root / name
        path.mkdir()
        (path / ".git").mkdir()
        return path

    def test_execute_command_builds_and_review_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            command = codex.build_codex_command("codex", repo, lane, "openai-api-key", "gpt-5.6-terra", self.provider, self.execute, "task")
            self.assertEqual(command[command.index("-m") + 1], "gpt-5.6-terra")
            self.assertIn("Injected canonical side-lane governance", command[-1])
            with self.assertRaisesRegex(codex.CodexAdapterError, "execute-only"):
                codex.build_codex_command("codex", repo, lane, "openai-api-key", "gpt-5.6-terra", self.provider, self.review, "review", mode="review")

    def test_route_shape_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            for broken, pattern in (
                ({**self.provider, "auth_method": "oauth"}, "provider-key"),
                ({**self.provider, "billable": False}, "billable true"),
                ({**self.provider, "credential_service": ""}, "credential_service"),
                ({**self.provider, "gateway": "direct-openai"}, "gateway"),
            ):
                with self.assertRaisesRegex(codex.CodexAdapterError, pattern):
                    codex.build_codex_command("codex", repo, lane, "openai-api-key", "gpt-5.6-terra", broken, self.execute, "task")

    def test_environment_carries_only_the_launcher_secret(self) -> None:
        inherited = {"PATH": "/bin", "OPENAI_API_KEY": "inherited-never", "ANTHROPIC_AUTH_TOKEN": "other-never",
            "CODEX_API_KEY": "inherited-never", "CODEX_HOME": "/home/dev/.codex",
            "SIDE_LANE_CREDENTIAL_OTHER": "backend-never"}
        child = codex.build_transport_environment(inherited, provider="openai-api-key", model="gpt-5.6-terra",
            provider_config=self.provider, model_config=self.execute, mode="execute", secret="sk-test-123")
        self.assertEqual(child["OPENAI_API_KEY"], "sk-test-123")
        self.assertEqual(child["CODEX_API_KEY"], "sk-test-123")
        self.assertEqual(child["OPENAI_BASE_URL"], "https://api.openai.com/v1")
        self.assertEqual(child["PATH"], "/bin")
        self.assertEqual(child["CODEX_HOME"], "/home/dev/.codex")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", child)
        self.assertNotIn("SIDE_LANE_CREDENTIAL_OTHER", child)
        without_base = {k: v for k, v in self.provider.items() if k != "base_url"}
        child = codex.build_transport_environment(inherited, provider="openai-api-key", model="gpt-5.6-terra",
            provider_config=without_base, model_config=self.execute, mode="execute", secret="sk-test-123")
        self.assertNotIn("OPENAI_BASE_URL", child)

    def test_missing_secret_and_secret_on_oauth_route_fail_closed(self) -> None:
        with self.assertRaisesRegex(codex.CodexAdapterError, "credential is absent"):
            codex.build_transport_environment({}, provider="openai-api-key", model="gpt-5.6-terra",
                provider_config=self.provider, model_config=self.execute, mode="execute", secret=None)
        with self.assertRaisesRegex(codex.CodexAdapterError, "must not receive an API key"):
            codex.build_transport_environment({}, provider="openai", model="gpt-5.6-terra",
                provider_config=self.native, model_config=self.execute, mode="execute", secret="sk-test-123")

    def test_mocked_run_reports_billable_route_and_redacts_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "used sk-test-123 ok", "err sk-test-123"))
            result = codex.run_codex(executable="codex", repo=repo, worktree=lane,
                provider="openai-api-key", model="gpt-5.6-terra", provider_config=self.provider,
                model_config=self.execute, prompt="task", env={"PATH": "/bin", "OPENAI_API_KEY": "never"},
                secret="sk-test-123", runner=runner)
        self.assertEqual((result.gateway, result.auth_method, result.billable), ("codex-api-key", "provider-key", True))
        self.assertEqual(runner.call_args.kwargs["env"]["OPENAI_API_KEY"], "sk-test-123")
        self.assertEqual(runner.call_args.kwargs["env"]["CODEX_API_KEY"], "sk-test-123")
        self.assertIn("--json", result.argv)
        self.assertEqual(result.stdout, "used [REDACTED_PROVIDER_KEY] ok")
        self.assertEqual(result.stderr, "err [REDACTED_PROVIDER_KEY]")
        self.assertNotIn("sk-test-123", " ".join(result.argv))


if __name__ == "__main__":
    unittest.main()
