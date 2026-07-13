import contextlib
import io
import unittest
from unittest import mock

from cc_switch_ui.app import _is_loopback_host, main


class AppTests(unittest.TestCase):
    def test_loopback_detection(self):
        self.assertTrue(_is_loopback_host("127.0.0.1"))
        self.assertTrue(_is_loopback_host("::1"))
        self.assertTrue(_is_loopback_host("localhost"))
        self.assertFalse(_is_loopback_host("0.0.0.0"))
        self.assertFalse(_is_loopback_host("server.example.com"))

    def test_cli_management_rejects_non_loopback_listener(self):
        argv = [
            "cc-switch-ui",
            "--host",
            "0.0.0.0",
            "--allow-remote",
            "--allow-cli-management",
        ]
        with (
            mock.patch("sys.argv", argv),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            main()

        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
