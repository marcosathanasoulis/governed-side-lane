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


if __name__ == "__main__":
    unittest.main()


class RefreshCommandTests(unittest.TestCase):
    def test_bare_host_keeps_the_conventional_hint(self) -> None:
        from side_lane.auth import refresh_command

        self.assertEqual(refresh_command("codex"), "codex login")
        self.assertEqual(refresh_command("codex", "codex"), "codex login")
        self.assertEqual(refresh_command("claude"), "claude auth login")

    def test_resolved_bundle_path_is_quoted_into_the_hint(self) -> None:
        from side_lane.auth import refresh_command

        bundle = "/Applications/ChatGPT.app/Contents/Resources/codex"
        self.assertEqual(refresh_command("codex", bundle), f"{bundle} login")
        spaced = "/Users/me/My Tools/claude"
        self.assertEqual(refresh_command("claude", spaced), "'/Users/me/My Tools/claude' auth login")

    def test_status_and_failure_carry_the_runnable_hint(self) -> None:
        bundle = "/Applications/ChatGPT.app/Contents/Resources/codex"
        signed_out = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "signed out"))
        status = auth_status("codex", executable=bundle, runner=signed_out)
        self.assertEqual(status.refresh_command, f"{bundle} login")
        with self.assertRaisesRegex(AuthError, "ChatGPT.app/Contents/Resources/codex login"):
            require_native_oauth("codex", executable=bundle, runner=signed_out)
        missing = mock.Mock(side_effect=OSError("no such file"))
        self.assertEqual(auth_status("claude", executable="/opt/claude", runner=missing).refresh_command, "/opt/claude auth login")
