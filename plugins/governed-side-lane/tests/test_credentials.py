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


if __name__ == "__main__":
    unittest.main()
