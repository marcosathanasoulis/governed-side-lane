import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from side_lane.adapters import claude
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
        # Non-string ``type`` values must be ignored, not raise ``TypeError``.
        self.assertEqual(codex._stream_metadata('{"type": []}\n{"type": {}}\n{"type": 7}\n'), (None, None))
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


class CodexTimeoutTests(unittest.TestCase):
    """Opt-in ``timeout_seconds`` on Codex routes (parity with claude/devin)."""

    provider = {"gateway": "native-codex", "auth_method": "oauth", "billable": False}
    execute = {"runtime_model": "gpt-5.6-terra", "protocol": "native-codex"}

    def repo(self, root: Path, name: str) -> Path:
        path = root / name
        path.mkdir()
        (path / ".git").mkdir()
        return path

    def test_absent_timeout_passes_no_timeout_kwarg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
            codex.run_codex(executable="codex", repo=repo, worktree=lane,
                provider="openai", model="gpt-5.6-terra", provider_config=self.provider,
                model_config=self.execute, prompt="task", env={"PATH": "/bin"}, runner=runner)
        self.assertNotIn("timeout", runner.call_args.kwargs)

    def test_absent_timeout_preserves_injected_runner_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            error = subprocess.TimeoutExpired("external-runner", 7)
            runner = mock.Mock(side_effect=error)
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                codex.run_codex(executable="codex", repo=repo, worktree=lane,
                    provider="openai", model="gpt-5.6-terra", provider_config=self.provider,
                    model_config=self.execute, prompt="task", env={"PATH": "/bin"},
                    runner=runner)
            self.assertIs(raised.exception, error)
            self.assertNotIn("timeout", runner.call_args.kwargs)

    def test_configured_timeout_is_forwarded_to_an_injected_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
            result = codex.run_codex(executable="codex", repo=repo, worktree=lane,
                provider="openai", model="gpt-5.6-terra", provider_config=self.provider,
                model_config={**self.execute, "timeout_seconds": 2400}, prompt="task",
                env={"PATH": "/bin"}, runner=runner)
        self.assertEqual(runner.call_args.kwargs["timeout"], 2400)
        self.assertEqual(result.returncode, 0)

    def test_malformed_timeout_fails_closed_before_any_process_starts(self) -> None:
        for bad in (0, -5, True, False, "600", 1.5, None):
            with self.subTest(timeout_seconds=bad), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
                runner = mock.Mock()
                with self.assertRaisesRegex(codex.CodexAdapterError, "positive integer"):
                    codex.run_codex(executable="codex", repo=repo, worktree=lane,
                        provider="openai", model="gpt-5.6-terra", provider_config=self.provider,
                        model_config={**self.execute, "timeout_seconds": bad}, prompt="task",
                        env={"PATH": "/bin"}, runner=runner)
                runner.assert_not_called()

    @unittest.skipUnless(os.name == "posix",
        "the fake worker is a POSIX shell script and this asserts process-group cleanup")
    def test_configured_timeout_bounds_the_default_runner_process_group(self) -> None:
        """Real launch through the CLI's own seam: no runner is injected, so the
        adapter's bounded lifecycle must stop the worker's whole process group —
        a background child sharing that group must not outlive the kill and
        write its sentinel. The receipt is exit 124 with the partial stream."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            sentinel = root / "orphan-wrote-this"
            fake = root / "codex"
            fake.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '{\"type\":\"thread.started\",\"model\":\"gpt-5.6-terra\"}'\n"
                f"(sleep 2; touch {sentinel}) &\n"
                "sleep 30\n",
                encoding="utf-8",
            )
            os.chmod(fake, 0o755)
            started = time.monotonic()
            result = codex.run_codex(executable=str(fake), repo=repo, worktree=lane,
                provider="openai", model="gpt-5.6-terra", provider_config=self.provider,
                model_config={**self.execute, "timeout_seconds": 1}, prompt="task",
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
            self.assertEqual(result.returncode, 124)
            self.assertIn("worker timed out", result.stderr)
            self.assertIn("process group stopped", result.stderr)
            # The partial JSONL stream survived the kill and was still parsed.
            self.assertEqual(result.resolved_model, "gpt-5.6-terra")
            # Wait past the same-group child's 2s mark; group cleanup means it
            # was SIGTERMed with its parent and never reaches the sentinel.
            while time.monotonic() - started < 4:
                time.sleep(0.1)
            self.assertFalse(sentinel.exists(), "same-group child outlived the worker timeout")

    def test_configured_timeout_stops_the_child_where_no_process_groups(self) -> None:
        """Without POSIX process groups (Windows) the same default-runner seam
        takes the Popen terminate/kill path — no ``killpg``, no new session.
        ``_stop_process_tree`` is pinned off so the direct-child fallback is
        what runs even on a real Windows host."""
        process = mock.Mock()
        process.pid = 77
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("codex", 1), ("partial", "")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            with mock.patch.object(claude, "_posix_process_groups", return_value=False), \
                    mock.patch.object(claude, "_stop_process_tree", return_value=False), \
                    mock.patch.object(claude.subprocess, "Popen",
                                      return_value=process) as popen:
                result = codex.run_codex(executable="codex", repo=repo, worktree=lane,
                    provider="openai", model="gpt-5.6-terra", provider_config=self.provider,
                    model_config={**self.execute, "timeout_seconds": 1}, prompt="task",
                    env={"PATH": "/bin"})
        self.assertNotIn("start_new_session", popen.call_args.kwargs)
        process.terminate.assert_called_once_with()
        process.kill.assert_not_called()
        self.assertEqual(result.returncode, 124)
        self.assertIn("worker timed out", result.stderr)
        self.assertNotIn("process group stopped", result.stderr)

    def test_injected_runner_timeout_expired_becomes_a_sanitized_receipt(self) -> None:
        provider = {"gateway": "codex-api-key", "auth_method": "provider-key",
            "billable": True, "credential_service": "example-side-lane-openai"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            def raising_runner(argv, **kwargs):
                raise subprocess.TimeoutExpired(
                    argv, kwargs.get("timeout"), output="partial sk-test-123",
                    stderr="err sk-test-123")
            result = codex.run_codex(executable="codex", repo=repo, worktree=lane,
                provider="openai-api-key", model="gpt-5.6-terra", provider_config=provider,
                model_config={**self.execute, "timeout_seconds": 600}, prompt="task",
                env={"PATH": "/bin"}, secret="sk-test-123", runner=raising_runner)
        self.assertEqual(result.returncode, 124)
        self.assertIn("worker timed out", result.stderr)
        self.assertNotIn("sk-test-123", result.stdout)
        self.assertNotIn("sk-test-123", result.stderr)
        self.assertIn("[REDACTED_PROVIDER_KEY]", result.stdout)


if __name__ == "__main__":
    unittest.main()
