from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.sh"


class InstallerTests(unittest.TestCase):
    def make_environment(self, home: Path) -> dict[str, str]:
        stub_bin = home / "stub-bin"
        stub_bin.mkdir(parents=True)
        security = stub_bin / "security"
        security.write_text("#!/bin/sh\nexit 44\n", encoding="utf-8")
        security.chmod(0o755)
        env = os.environ.copy()
        env["HOME"] = str(home)
        env.pop("CODEX_HOME", None)
        env["PATH"] = f"{stub_bin}:{home}/.local/bin:/usr/bin:/bin"
        return env

    def run_installer(
        self, mode: str, home: Path, env: dict[str, str], host: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        command = ["bash", str(INSTALLER), mode]
        if host is not None:
            command.append(host)
        return subprocess.run(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def test_install_check_uninstall_are_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            env = self.make_environment(home)
            installed = self.run_installer("install", home, env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            destinations = (
                home / ".local/bin/side-lane",
                home / ".codex/skills/side-lane",
                home / ".codex/skills/prompt-it-side-lane-routing",
                home / ".claude/skills/side-lane",
                home / ".claude/skills/prompt-it-side-lane-routing",
            )
            self.assertTrue(all(path.is_symlink() for path in destinations))
            checked = self.run_installer("check", home, env)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            removed = self.run_installer("uninstall", home, env)
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertTrue(all(not path.exists() for path in destinations))
            self.assertNotIn("governed-side-lane:shared-context", (home / ".codex/AGENTS.md").read_text(encoding="utf-8"))

    def test_install_preserves_unrelated_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            env = self.make_environment(home)
            destination = home / ".local/bin/side-lane"
            destination.parent.mkdir(parents=True)
            destination.write_text("user-owned\n", encoding="utf-8")
            result = self.run_installer("install", home, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(destination.read_text(encoding="utf-8"), "user-owned\n")
            self.assertFalse((home / ".codex/skills/side-lane").exists())
            self.assertFalse((home / ".claude/skills/side-lane").exists())

    def test_host_selective_install_and_uninstall_share_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            env = self.make_environment(home)
            runner = home / ".local/bin/side-lane"
            codex_skill = home / ".codex/skills/side-lane"
            codex_prompt = home / ".codex/skills/prompt-it-side-lane-routing"
            claude_skill = home / ".claude/skills/side-lane"
            claude_prompt = home / ".claude/skills/prompt-it-side-lane-routing"

            codex_install = self.run_installer("install", home, env, "codex")
            self.assertEqual(codex_install.returncode, 0, codex_install.stderr)
            self.assertTrue(runner.is_symlink())
            self.assertTrue(codex_skill.is_symlink())
            self.assertTrue(codex_prompt.is_symlink())
            self.assertFalse(claude_skill.exists())
            self.assertFalse(claude_prompt.exists())
            self.assertEqual(
                self.run_installer("check", home, env, "codex").returncode, 0
            )

            claude_install = self.run_installer("install", home, env, "claude")
            self.assertEqual(claude_install.returncode, 0, claude_install.stderr)
            self.assertTrue(claude_skill.is_symlink())
            self.assertTrue(claude_prompt.is_symlink())

            self.assertEqual(
                self.run_installer("uninstall", home, env, "codex").returncode, 0
            )
            self.assertFalse(codex_skill.exists())
            self.assertFalse(codex_prompt.exists())
            self.assertTrue(claude_skill.is_symlink())
            self.assertTrue(claude_prompt.is_symlink())
            self.assertTrue(runner.is_symlink())

            self.assertEqual(
                self.run_installer("uninstall", home, env, "claude").returncode,
                0,
            )
            self.assertFalse(claude_skill.exists())
            self.assertFalse(claude_prompt.exists())
            self.assertFalse(runner.exists())


if __name__ == "__main__":
    unittest.main()
