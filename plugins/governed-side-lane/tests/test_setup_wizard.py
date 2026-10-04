from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SETUP_PATH = ROOT / "scripts" / "setup.py"

_spec = importlib.util.spec_from_file_location("side_lane_setup_wizard", SETUP_PATH)
setup = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
sys.modules["side_lane_setup_wizard"] = setup
_spec.loader.exec_module(setup)  # type: ignore[union-attr]

from side_lane import credentials, preferences  # noqa: E402
from side_lane.auth import AuthStatus  # noqa: E402


# Host detection must be decided only by each test's injected ``which``: a
# developer machine with the ChatGPT or Codex desktop app installed would
# otherwise "find" Codex through the real bundled-app locations.
_BUNDLED_PATCH = None


def setUpModule():
    global _BUNDLED_PATCH
    from side_lane import hosts as _hosts_module

    _BUNDLED_PATCH = mock.patch.object(_hosts_module, "BUNDLED_CODEX_CANDIDATES", ())
    _BUNDLED_PATCH.start()


def tearDownModule():
    if _BUNDLED_PATCH is not None:
        _BUNDLED_PATCH.stop()



def fake_which(present: dict[str, str]):
    return lambda name: present.get(name)


def fake_runner(returncode: int = 0, stdout: str = "ok\n"):
    def _runner(command, **kwargs):
        return subprocess.CompletedProcess(command, returncode, stdout, "")
    return _runner


class DetectionTests(unittest.TestCase):
    def test_absent_host_reports_not_present(self) -> None:
        info = setup.detect_host("codex", which=fake_which({}))
        self.assertFalse(info["present"])
        self.assertIsNone(info["auth"])

    def test_present_host_reports_auth_status(self) -> None:
        with mock.patch(
            "side_lane_setup_wizard.auth_status",
            return_value=AuthStatus("claude", "ready", "oauth", "claude auth login"),
        ):
            info = setup.detect_host("claude", which=fake_which({"claude": "/usr/bin/claude"}))
        self.assertTrue(info["present"])
        self.assertTrue(info["auth"].ready)

    def test_detect_hosts_covers_both(self) -> None:
        with mock.patch(
            "side_lane_setup_wizard.auth_status",
            return_value=AuthStatus("claude", "ready", "oauth", "x"),
        ):
            detected = setup.detect_hosts(which=fake_which({"claude": "/usr/bin/claude"}))
        self.assertEqual(set(detected), {"claude", "codex"})
        self.assertTrue(detected["claude"]["present"])
        self.assertFalse(detected["codex"]["present"])


class UsageFlagParsingTests(unittest.TestCase):
    def test_parses_repeatable_flags(self) -> None:
        parsed = setup.parse_usage_flags(["claude=extra-usage", "codex=included-oauth"])
        self.assertEqual(parsed, {"claude": "extra-usage", "codex": "included-oauth"})

    def test_rejects_unknown_host(self) -> None:
        with self.assertRaisesRegex(setup.SetupError, "host must be one of"):
            setup.parse_usage_flags(["devin=unknown"])

    def test_rejects_unknown_state(self) -> None:
        with self.assertRaisesRegex(setup.SetupError, "state must be one of"):
            setup.parse_usage_flags(["claude=on-the-house"])

    def test_rejects_malformed_entry(self) -> None:
        with self.assertRaisesRegex(setup.SetupError, "host=state"):
            setup.parse_usage_flags(["claude"])


class OpenRouterKeyStatusTests(unittest.TestCase):
    def test_never_touches_network_when_opener_injected(self) -> None:
        calls = []

        def opener(url, data, headers):
            calls.append((url, headers))
            return {"data": {"limit": 10}}

        result = setup.check_openrouter_key_status("sk-test", opener=opener)
        self.assertEqual(result, {"data": {"limit": 10}})
        self.assertEqual(len(calls), 1)
        self.assertIn("Bearer sk-test", calls[0][1]["Authorization"])

    def test_opener_failure_returns_none_not_raise(self) -> None:
        def opener(url, data, headers):
            raise OSError("network blocked in test")

        self.assertIsNone(setup.check_openrouter_key_status("sk-test", opener=opener))


class StoreOpenRouterKeyTests(unittest.TestCase):
    def test_empty_key_is_refused(self) -> None:
        with self.assertRaisesRegex(setup.SetupError, "must not be empty"):
            setup.store_openrouter_key("   ")

    def test_stores_through_credentials_module(self) -> None:
        with mock.patch("side_lane.credentials.store_credential") as store:
            setup.store_openrouter_key("sk-abc", system="Darwin")
        store.assert_called_once_with(setup.OPENROUTER_SERVICE, "sk-abc", "Darwin")


class NonInteractiveRunTests(unittest.TestCase):
    def _args(self, **overrides):
        parser = setup.make_parser()
        defaults = [
            "--non-interactive", "--hosts", "claude", "--prompt-it-mode", "ask-first",
            "--openrouter", "skip", "--usage", "claude=extra-usage",
        ]
        args = parser.parse_args(defaults)
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_full_non_interactive_run_is_idempotent(self) -> None:
        with mock.patch(
            "side_lane_setup_wizard.auth_status",
            return_value=AuthStatus("claude", "ready", "oauth", "x"),
        ), tempfile.TemporaryDirectory() as directory:
            prefs_path = Path(directory) / "preferences.json"
            with mock.patch("side_lane.preferences.preferences_path", return_value=prefs_path):
                runner = fake_runner()
                which = fake_which({"claude": "/usr/bin/claude"})
                output = []
                first = setup.run_non_interactive(
                    self._args(), which=which, runner=runner,
                    system="Linux", print_fn=output.append,
                )
                second = setup.run_non_interactive(
                    self._args(), which=which, runner=runner,
                    system="Linux", print_fn=output.append,
                )
                self.assertEqual(first, 0)
                self.assertEqual(second, 0)
                self.assertEqual(
                    preferences.load_preferences(prefs_path), {"claude": "extra-usage"}
                )

    def test_unknown_requested_host_raises(self) -> None:
        with self.assertRaises(setup.SetupError):
            setup.run_non_interactive(
                self._args(hosts="not-a-host"), which=fake_which({}),
                runner=fake_runner(), system="Linux",
            )

    def test_remove_openrouter_key_flag(self) -> None:
        with mock.patch("side_lane.credentials.delete_credential") as delete:
            setup.run_non_interactive(
                self._args(remove_openrouter_key=True, hosts="claude"),
                which=fake_which({"claude": "/usr/bin/claude"}),
                runner=fake_runner(), system="Linux", print_fn=lambda *_: None,
            )
        delete.assert_called_once_with(setup.OPENROUTER_SERVICE, "Linux")


class WizardInteractiveTests(unittest.TestCase):
    def setUp(self) -> None:
        # The answer sequences below include the Prompt it mode question, which
        # the wizard only asks when it finds a mode.py. Pin that so the test is
        # the same in a full checkout and in an exported single-component tree.
        patcher = mock.patch.object(
            setup, "locate_mode_script", return_value=Path("/nonexistent/mode.py")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_enter_for_default_skips_openrouter_and_sets_unknown_usage(self) -> None:
        answers = iter(["", "", "", ""])  # prompt-it mode, openrouter, usage(claude)
        output = []
        with tempfile.TemporaryDirectory() as directory:
            prefs_path = Path(directory) / "preferences.json"
            with mock.patch("side_lane.preferences.preferences_path", return_value=prefs_path), \
                 mock.patch(
                     "side_lane_setup_wizard.auth_status",
                     return_value=AuthStatus("claude", "ready", "oauth", "x"),
                 ):
                result = setup.run_wizard(
                    input_fn=lambda _prompt: next(answers),
                    getpass_fn=lambda _prompt: "unused",
                    which=fake_which({"claude": "/usr/bin/claude"}),
                    runner=fake_runner(),
                    system="Linux",
                    print_fn=output.append,
                )
                self.assertEqual(result, 0)
                self.assertEqual(
                    preferences.load_preferences(prefs_path), {"claude": "unknown"}
                )
        joined = "\n".join(output)
        self.assertNotIn("unused", joined)

    def test_key_never_appears_in_printed_output(self) -> None:
        answers = iter(["1", "y", "n", "2"])
        output = []
        with tempfile.TemporaryDirectory() as directory:
            prefs_path = Path(directory) / "preferences.json"
            with mock.patch("side_lane.preferences.preferences_path", return_value=prefs_path), \
                 mock.patch("side_lane.credentials.store_credential") as store, \
                 mock.patch("side_lane.credentials.credential_present", return_value=True), \
                 mock.patch(
                     "side_lane_setup_wizard.auth_status",
                     return_value=AuthStatus("claude", "ready", "oauth", "x"),
                 ):
                setup.run_wizard(
                    input_fn=lambda _prompt: next(answers),
                    getpass_fn=lambda _prompt: "sk-TOTALLY-SECRET-VALUE",
                    which=fake_which({"claude": "/usr/bin/claude"}),
                    runner=fake_runner(),
                    system="Linux",
                    print_fn=output.append,
                )
        store.assert_called_once_with(setup.OPENROUTER_SERVICE, "sk-TOTALLY-SECRET-VALUE", "Linux")
        joined = "\n".join(output)
        self.assertNotIn("sk-TOTALLY-SECRET-VALUE", joined)

    def test_no_hosts_present_reports_and_exits_nonzero(self) -> None:
        output = []
        result = setup.run_wizard(
            input_fn=lambda _p: "", getpass_fn=lambda _p: "", which=fake_which({}),
            runner=fake_runner(), system="Linux", print_fn=output.append,
        )
        self.assertEqual(result, 1)


class CheckCommandTests(unittest.TestCase):
    def test_check_reports_absence_cleanly(self) -> None:
        output = []
        result = setup.run_check(which=fake_which({}), system="Linux", print_fn=output.append)
        self.assertEqual(result, 1)
        self.assertIn("not installed", "\n".join(output))

    def test_check_reports_presence(self) -> None:
        output = []
        with mock.patch(
            "side_lane_setup_wizard.auth_status",
            return_value=AuthStatus("claude", "ready", "oauth", "x"),
        ):
            result = setup.run_check(
                which=fake_which({"claude": "/usr/bin/claude"}), system="Linux",
                print_fn=output.append,
            )
        self.assertEqual(result, 0)


class OpenRouterKeyStepTests(unittest.TestCase):
    def test_stores_and_reports_presence(self) -> None:
        output = []
        with mock.patch("side_lane.credentials.store_credential") as store, \
             mock.patch("side_lane.credentials.credential_present", return_value=True):
            result = setup.run_openrouter_key_step(
                getpass_fn=lambda _p: "sk-key-value", system="Linux", print_fn=output.append,
            )
        self.assertEqual(result, 0)
        store.assert_called_once_with(setup.OPENROUTER_SERVICE, "sk-key-value", "Linux")
        self.assertNotIn("sk-key-value", "\n".join(output))


if __name__ == "__main__":
    unittest.main()
