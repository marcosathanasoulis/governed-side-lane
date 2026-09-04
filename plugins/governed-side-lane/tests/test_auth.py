import subprocess
import unittest
from unittest import mock

from side_lane.auth import auth_status, require_native_oauth, AuthError


class AuthTests(unittest.TestCase):
    def test_codex_oauth_and_api_key_methods(self) -> None:
        oauth = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "Logged in with ChatGPT", ""))
        self.assertTrue(auth_status("codex", runner=oauth).ready)
        key = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "Logged in with API key SECRET", ""))
        status = auth_status("codex", runner=key)
        self.assertEqual(status.method, "api-key")
        self.assertNotIn("SECRET", str(status.as_dict()))

    def test_claude_json_uses_only_method_metadata(self) -> None:
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '{"loggedIn":true,"authMethod":"oauth","token":"NEVER"}', ""))
        status = auth_status("claude", runner=runner)
        self.assertTrue(status.ready)
        self.assertNotIn("NEVER", str(status.as_dict()))
        ambiguous = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '{"loggedIn":true}', ""))
        self.assertFalse(auth_status("claude", runner=ambiguous).ready)

    def test_missing_oauth_fails_with_refresh_only(self) -> None:
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "signed out"))
        with self.assertRaisesRegex(AuthError, "codex login"):
            require_native_oauth("codex", runner=runner)



class RefreshCommandTests(unittest.TestCase):
    bundle = "/Applications/ChatGPT.app/Contents/Resources/codex"

    def test_bare_host_keeps_the_conventional_hint(self) -> None:
        from side_lane.auth import refresh_command

        self.assertEqual(refresh_command("codex", which=lambda _: None), "codex login")
        self.assertEqual(refresh_command("codex", "codex", which=lambda _: None), "codex login")
        self.assertEqual(refresh_command("claude", which=lambda _: None), "claude auth login")

    def test_path_found_executable_keeps_the_bare_hint_even_when_absolute(self) -> None:
        from side_lane.auth import refresh_command

        self.assertEqual(refresh_command("codex", "/usr/local/bin/codex", which=lambda _: "/usr/local/bin/codex"), "codex login")
        self.assertEqual(refresh_command("claude", "/opt/x/../x/claude", which=lambda _: "/opt/x/claude"), "claude auth login")

    def test_off_path_executable_is_quoted_for_posix_shells(self) -> None:
        from side_lane.auth import refresh_command

        self.assertEqual(refresh_command("codex", self.bundle, which=lambda _: None, platform="posix"), f"{self.bundle} login")
        self.assertEqual(refresh_command("codex", self.bundle, which=lambda _: "/usr/local/bin/codex", platform="posix"), f"{self.bundle} login")
        self.assertEqual(refresh_command("claude", "/Users/me/My Tools/claude", which=lambda _: None, platform="posix"), "'/Users/me/My Tools/claude' auth login")

    def test_off_path_executable_uses_the_powershell_call_operator_on_windows(self) -> None:
        from side_lane.auth import refresh_command

        exe = "C:\\Tools\\codex.exe"
        self.assertEqual(refresh_command("codex", exe, which=lambda _: None, platform="nt"), f"& '{exe}' login")
        spaced = "C:\\Program Files\\Codex\\codex.exe"
        self.assertEqual(refresh_command("codex", spaced, which=lambda _: None, platform="nt"), f"& '{spaced}' login")
        quoted = "C:\\Users\\O'Brien\\claude.exe"
        self.assertEqual(refresh_command("claude", quoted, which=lambda _: None, platform="nt"), "& 'C:\\Users\\O''Brien\\claude.exe' auth login")
        self.assertEqual(refresh_command("codex", exe, which=lambda _: exe, platform="nt"), "codex login")

    def test_status_and_failure_carry_the_runnable_hint(self) -> None:
        signed_out = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "signed out"))
        # which/platform are looked up at call time, so patching the module works.
        with mock.patch("side_lane.auth.shutil.which", return_value=None), mock.patch("side_lane.auth.os.name", "posix"):
            status = auth_status("codex", executable=self.bundle, runner=signed_out)
            self.assertEqual(status.refresh_command, f"{self.bundle} login")
            with self.assertRaisesRegex(AuthError, "ChatGPT.app/Contents/Resources/codex login"):
                require_native_oauth("codex", executable=self.bundle, runner=signed_out)
            missing = mock.Mock(side_effect=OSError("no such file"))
            self.assertEqual(auth_status("claude", executable="/opt/claude", runner=missing).refresh_command, "/opt/claude auth login")
        with mock.patch("side_lane.auth.shutil.which", return_value=self.bundle):
            self.assertEqual(auth_status("codex", executable=self.bundle, runner=signed_out).refresh_command, "codex login")


if __name__ == "__main__":
    unittest.main()
