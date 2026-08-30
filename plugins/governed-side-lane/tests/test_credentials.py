from __future__ import annotations

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

    def test_unsupported_platform_fails_closed(self) -> None:
        self.assertFalse(credentials.credential_present("service", "Linux"))
        with self.assertRaises(credentials.CredentialError):
            credentials.read_credential("service", "Linux")

    def test_macos_distinguishes_absence_from_store_failure(self) -> None:
        with mock.patch("side_lane.credentials.subprocess.run", return_value=mock.Mock(returncode=44, stdout="")):
            self.assertFalse(credentials.credential_present("service", "Darwin"))
        with mock.patch("side_lane.credentials.subprocess.run", return_value=mock.Mock(returncode=36, stdout="")):
            with self.assertRaisesRegex(credentials.CredentialError, "lookup failed"):
                credentials.credential_present("service", "Darwin")


if __name__ == "__main__":
    unittest.main()
