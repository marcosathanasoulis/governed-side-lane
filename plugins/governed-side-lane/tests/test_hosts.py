import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from side_lane import hosts
from side_lane.hosts import HostExecutableError, require_host_executable, resolve_host_executable


def _make_executable(directory: Path, name: str = "codex") -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class HostExecutableTests(unittest.TestCase):
    def test_path_lookup_wins_when_present(self) -> None:
        found = resolve_host_executable("codex", env={}, which=lambda name: f"/usr/local/bin/{name}")
        self.assertEqual(found, "/usr/local/bin/codex")

    def test_explicit_override_is_used_and_must_be_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = _make_executable(Path(directory))
            env = {"SIDE_LANE_CODEX_EXECUTABLE": str(binary)}
            self.assertEqual(resolve_host_executable("codex", env=env, which=lambda _: "/elsewhere/codex"), str(binary))
            broken = {"SIDE_LANE_CODEX_EXECUTABLE": str(Path(directory) / "missing")}
            with self.assertRaisesRegex(HostExecutableError, "SIDE_LANE_CODEX_EXECUTABLE"):
                resolve_host_executable("codex", env=broken, which=lambda _: "/elsewhere/codex")

    def test_codex_falls_back_to_desktop_app_bundle_and_reports_that_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "ChatGPT.app" / "Contents" / "Resources"
            bundle.mkdir(parents=True)
            bundled = _make_executable(bundle)
            with mock.patch.object(hosts, "BUNDLED_CODEX_CANDIDATES", (str(Path(directory) / "absent"), str(bundled))):
                found = resolve_host_executable("codex", env={}, which=lambda _: None)
        self.assertEqual(found, str(bundled))

    def test_claude_never_consults_codex_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundled = _make_executable(Path(directory))
            with mock.patch.object(hosts, "BUNDLED_CODEX_CANDIDATES", (str(bundled),)):
                self.assertIsNone(resolve_host_executable("claude", env={}, which=lambda _: None))

    def test_missing_executable_error_is_actionable_and_secret_free(self) -> None:
        with mock.patch.object(hosts, "BUNDLED_CODEX_CANDIDATES", ()):
            with self.assertRaisesRegex(HostExecutableError, "npm install -g @openai/codex.*SIDE_LANE_CODEX_EXECUTABLE"):
                require_host_executable("codex", env={"OPENAI_API_KEY": "never"}, which=lambda _: None)
            try:
                require_host_executable("codex", env={"OPENAI_API_KEY": "never"}, which=lambda _: None)
            except HostExecutableError as exc:
                self.assertNotIn("never", str(exc))

    def test_unsupported_host_is_rejected(self) -> None:
        with self.assertRaises(HostExecutableError):
            resolve_host_executable("gemini", env={}, which=lambda _: None)
