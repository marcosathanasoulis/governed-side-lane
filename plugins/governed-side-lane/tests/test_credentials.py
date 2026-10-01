from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from side_lane import credentials


class CredentialTests(unittest.TestCase):
    def test_presence_never_requests_secret_value(self) -> None:
        completed = mock.Mock(returncode=0)
        with mock.patch("side_lane.credentials.subprocess.run", return_value=completed) as run:
            self.assertTrue(credentials.credential_present("service", "Darwin"))
        self.assertNotIn("-w", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["stdout"], credentials.subprocess.DEVNULL)

    def test_linux_without_env_or_file_fails_closed(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(credentials.credential_present("service", "Linux"))
            with self.assertRaisesRegex(credentials.CredentialError, "absent"):
                credentials.read_credential("service", "Linux")

    def test_env_variable_name_sanitises_service(self) -> None:
        self.assertEqual(
            credentials.env_variable_for_service("side-lane-glm"),
            "SIDE_LANE_CREDENTIAL_SIDE_LANE_GLM",
        )
        self.assertEqual(credentials.env_variable_for_service("a.b c"),
                         "SIDE_LANE_CREDENTIAL_A_B_C")

    def test_linux_reads_environment_before_credentials_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "service").write_text("file-value\n", encoding="utf-8")
            env = {"SIDE_LANE_CREDENTIAL_SERVICE": " env-value \n",
                   "SIDE_LANE_CREDENTIALS_DIR": directory}
            with mock.patch.dict("os.environ", env, clear=True):
                self.assertTrue(credentials.credential_present("service", "Linux"))
                self.assertEqual(credentials.read_credential("service", "Linux"), "env-value")

    def test_linux_reads_credentials_file_without_reading_it_for_presence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "side-lane-glm"
            path.write_text("file-value\n", encoding="utf-8")
            with mock.patch.dict(
                    "os.environ", {"SIDE_LANE_CREDENTIALS_DIR": directory}, clear=True):
                with mock.patch.object(Path, "read_text", side_effect=AssertionError("secret read")):
                    self.assertTrue(credentials.credential_present(
                        "side-lane-glm", "Linux"))
                self.assertEqual(credentials.read_credential(
                    "side-lane-glm", "Linux"), "file-value")
                self.assertFalse(credentials.credential_present("other-service", "Linux"))

    def test_service_path_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict("os.environ", {"SIDE_LANE_CREDENTIALS_DIR": directory}, clear=True):
            for service in ("", ".", "..", "../secret", "subdir/secret",
                            r"..\secret", "/absolute", r"C:\absolute"):
                with self.subTest(service=service), \
                     self.assertRaisesRegex(credentials.CredentialError, "portable file name"):
                    credentials.read_credential(service, "Linux")

    def test_credentials_file_symlink_cannot_escape_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "credentials"
            root.mkdir()
            outside = Path(directory) / "outside"
            outside.write_text("must-not-read", encoding="utf-8")
            try:
                (root / "service").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with mock.patch.dict(
                    "os.environ", {"SIDE_LANE_CREDENTIALS_DIR": str(root)}, clear=True):
                with self.assertRaisesRegex(credentials.CredentialError, "escapes"):
                    credentials.read_credential("service", "Linux")

    def test_env_backend_flag_overrides_platform(self) -> None:
        env = {"SIDE_LANE_CREDENTIAL_BACKEND": "env", "SIDE_LANE_CREDENTIAL_SERVICE": "v"}
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch("side_lane.credentials.subprocess.run") as run:
                self.assertEqual(credentials.read_credential("service", "Darwin"), "v")
            run.assert_not_called()

    def test_backend_environment_is_parent_only(self) -> None:
        inherited = {"PATH": "/bin", "SIDE_LANE_CREDENTIAL_BACKEND": "env",
                     "SIDE_LANE_CREDENTIAL_FIRST": "first-secret",
                     "SIDE_LANE_CREDENTIAL_SECOND": "second-secret",
                     "SIDE_LANE_CREDENTIALS_DIR": "/private/credentials"}
        self.assertEqual(credentials.scrub_backend_environment(inherited), {"PATH": "/bin"})

    def test_darwin_default_still_uses_keychain(self) -> None:
        with mock.patch.dict("os.environ", {"SIDE_LANE_CREDENTIAL_SERVICE": "ignored"}, clear=True):
            completed = mock.Mock(returncode=0, stdout="from-keychain\n")
            with mock.patch("side_lane.credentials.subprocess.run", return_value=completed) as run:
                self.assertEqual(credentials.read_credential("service", "Darwin"), "from-keychain")
            run.assert_called_once()

    def test_macos_distinguishes_absence_from_store_failure(self) -> None:
        with mock.patch("side_lane.credentials.subprocess.run", return_value=mock.Mock(returncode=44, stdout="")):
            self.assertFalse(credentials.credential_present("service", "Darwin"))
        with mock.patch("side_lane.credentials.subprocess.run", return_value=mock.Mock(returncode=36, stdout="")):
            with self.assertRaisesRegex(credentials.CredentialError, "lookup failed"):
                credentials.credential_present("service", "Darwin")

    def test_store_credential_rejects_service_outside_allowlist(self) -> None:
        with self.assertRaisesRegex(credentials.CredentialError, "writable allowlist"):
            credentials.store_credential("not-allowlisted", "secret", "Darwin")
        with self.assertRaisesRegex(credentials.CredentialError, "writable allowlist"):
            credentials.delete_credential("not-allowlisted", "Darwin")

    def test_store_credential_rejects_empty_secret(self) -> None:
        with self.assertRaisesRegex(credentials.CredentialError, "non-empty"):
            credentials.store_credential("governed-side-lane-openrouter", "   ", "Darwin")

    def test_macos_store_pipes_secret_through_stdin_never_argv(self) -> None:
        completed = mock.Mock(returncode=0)
        with mock.patch("side_lane.credentials.subprocess.run", return_value=completed) as run:
            credentials.store_credential(
                "governed-side-lane-openrouter", "sk-super-secret-value", "Darwin"
            )
        call = run.call_args
        self.assertEqual(call.args[0], ["security", "-i"])
        self.assertNotIn("sk-super-secret-value", call.args[0])
        self.assertIn("sk-super-secret-value", call.kwargs["input"])
        self.assertIn("add-generic-password", call.kwargs["input"])

    def test_macos_store_escapes_quotes_and_backslashes(self) -> None:
        completed = mock.Mock(returncode=0)
        with mock.patch("side_lane.credentials.subprocess.run", return_value=completed) as run:
            credentials.store_credential(
                "governed-side-lane-openrouter", 'weird"value\\here', "Darwin"
            )
        script = run.call_args.kwargs["input"]
        self.assertIn('weird\\"value\\\\here', script)

    def test_macos_store_failure_raises(self) -> None:
        with mock.patch(
            "side_lane.credentials.subprocess.run",
            return_value=mock.Mock(returncode=1, stderr="nope"),
        ):
            with self.assertRaisesRegex(credentials.CredentialError, "could not store"):
                credentials.store_credential("governed-side-lane-glm", "secret", "Darwin")

    def test_macos_delete_is_idempotent(self) -> None:
        with mock.patch(
            "side_lane.credentials.subprocess.run", return_value=mock.Mock(returncode=44)
        ) as run:
            credentials.delete_credential("governed-side-lane-glm", "Darwin")
        run.assert_called_once()

    def test_windows_store_invokes_credential_script_via_stdin(self) -> None:
        completed = mock.Mock(returncode=0)
        with mock.patch("side_lane.credentials.subprocess.run", return_value=completed) as run:
            credentials.store_credential(
                "governed-side-lane-openrouter", "sk-secret", "Windows"
            )
        call = run.call_args
        self.assertNotIn("sk-secret", call.args[0])
        self.assertIn("sk-secret", call.kwargs["input"])
        self.assertIn("set", call.args[0])
        self.assertIn("governed-side-lane-openrouter", call.args[0])

    def test_windows_delete_invokes_credential_script(self) -> None:
        completed = mock.Mock(returncode=0)
        with mock.patch("side_lane.credentials.subprocess.run", return_value=completed) as run:
            credentials.delete_credential("governed-side-lane-openrouter", "Windows")
        self.assertIn("delete", run.call_args.args[0])

    def test_linux_store_without_secret_tool_refuses_with_no_plaintext_fallback(self) -> None:
        with mock.patch("side_lane.credentials.shutil.which", return_value=None):
            with self.assertRaisesRegex(credentials.CredentialError, "no Secret Service"):
                credentials.store_credential(
                    "governed-side-lane-openrouter", "secret", "Linux"
                )

    def test_linux_store_pipes_secret_through_stdin_via_secret_tool(self) -> None:
        completed = mock.Mock(returncode=0)
        with mock.patch("side_lane.credentials.shutil.which", return_value="/usr/bin/secret-tool"):
            with mock.patch("side_lane.credentials.subprocess.run", return_value=completed) as run:
                credentials.store_credential(
                    "governed-side-lane-openrouter", "sk-linux-secret", "Linux"
                )
        call = run.call_args
        self.assertNotIn("sk-linux-secret", call.args[0])
        self.assertEqual(call.kwargs["input"], "sk-linux-secret\n")
        self.assertEqual(call.args[0][:2], ["secret-tool", "store"])

    def test_linux_delete_treats_not_found_as_success(self) -> None:
        with mock.patch("side_lane.credentials.shutil.which", return_value="/usr/bin/secret-tool"):
            with mock.patch(
                "side_lane.credentials.subprocess.run",
                return_value=mock.Mock(returncode=1),
            ):
                credentials.delete_credential("governed-side-lane-openrouter", "Linux")

    def test_unsupported_platform_refuses_write(self) -> None:
        with self.assertRaisesRegex(credentials.CredentialError, "supported credential stores"):
            credentials.store_credential("governed-side-lane-glm", "secret", "Plan9")


if __name__ == "__main__":
    unittest.main()
