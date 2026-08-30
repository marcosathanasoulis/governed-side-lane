from __future__ import annotations

import unittest
from unittest import mock

from scripts import update


class UpdateTests(unittest.TestCase):
    def result(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> mock.Mock:
        return mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)

    def test_preflight_refuses_unexpected_remote_and_dirty_checkout(self) -> None:
        runner = mock.Mock(return_value=self.result("https://evil.example/repo\n"))
        with self.assertRaisesRegex(update.UpdateError, "unexpected origin"):
            update.preflight(runner)
        runner = mock.Mock(side_effect=[
            self.result("https://github.com/marcosathanasoulis/governed-side-lane\n"),
            self.result(" M README.md\n"),
        ])
        with self.assertRaisesRegex(update.UpdateError, "dirty"):
            update.preflight(runner)

    def test_apply_requires_signed_explicit_fast_forward_tag(self) -> None:
        runner = mock.Mock(side_effect=[
            self.result("https://github.com/marcosathanasoulis/governed-side-lane\n"),
            self.result(""), self.result("main\n"), self.result(""),
            self.result("verified\n"), self.result(""), self.result(""),
        ])
        update.apply("v1.2.3", runner)
        commands = [call.args[0] for call in runner.call_args_list]
        self.assertTrue(any("verify-tag" in command for command in commands))
        self.assertTrue(any("--ff-only" in command for command in commands))
        with self.assertRaises(update.UpdateError):
            update.apply("latest", mock.Mock())

    def test_available_returns_only_verified_tags(self) -> None:
        runner = mock.Mock(side_effect=[
            self.result("https://github.com/marcosathanasoulis/governed-side-lane\n"),
            self.result(""), self.result("main\n"), self.result(""),
            self.result("v2.0.0\nv1.0.0\n"),
            self.result(returncode=1, stderr="unsigned"), self.result("verified\n"),
        ])
        self.assertEqual(update.available(runner), ["v1.0.0"])


if __name__ == "__main__":
    unittest.main()
