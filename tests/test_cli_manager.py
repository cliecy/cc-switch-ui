import subprocess
import unittest
from unittest import mock

from cc_switch_ui.cli_manager import (
    CliManager,
    CliManagerError,
    _install_method,
    _trim_output,
    _version_from_output,
)


class CliManagerTests(unittest.TestCase):
    def test_version_parsing_and_install_method(self):
        self.assertEqual(_version_from_output("codex-cli 0.144.1"), "0.144.1")
        self.assertEqual(_version_from_output("2.1.3 (Claude Code)"), "2.1.3")
        self.assertEqual(
            _install_method("/home/u/.nvm/lib/node_modules/@openai/codex/bin/codex.js"),
            "npm",
        )
        self.assertNotIn(
            "proxy-password",
            _trim_output("https://proxy-user:proxy-password@proxy.example/error"),
        )

    def test_npm_install_uses_allowlisted_argv(self):
        manager = CliManager()
        completed = subprocess.CompletedProcess([], 0, "installed", "")

        with (
            mock.patch("cc_switch_ui.cli_manager.shutil.which") as which,
            mock.patch.object(manager, "_run", return_value=completed) as run,
        ):
            which.side_effect = lambda name: "/usr/bin/npm" if name == "npm" else None
            result = manager.manage("codex", "npm_install", "0.144.1", "china")

        command = run.call_args_list[0].args[0]
        self.assertEqual(command[0], "/usr/bin/npm")
        self.assertIn("@openai/codex@0.144.1", command)
        self.assertIn("https://registry.npmmirror.com", command)
        self.assertTrue(result["ok"])

    def test_rejects_command_injection_in_version(self):
        manager = CliManager()
        with mock.patch(
            "cc_switch_ui.cli_manager.shutil.which", return_value="/usr/bin/npm"
        ):
            with self.assertRaises(CliManagerError):
                manager.manage("claude", "npm_install", "latest;id", "official")

    def test_rejects_arbitrary_registry(self):
        manager = CliManager()
        with self.assertRaises(CliManagerError):
            manager.latest_versions("http://127.0.0.1:8080")

    def test_claude_native_install_supports_stable_channel(self):
        manager = CliManager()
        completed = subprocess.CompletedProcess([], 0, "installed stable", "")
        with (
            mock.patch(
                "cc_switch_ui.cli_manager.shutil.which",
                return_value="/home/u/.local/bin/claude",
            ),
            mock.patch.object(manager, "_run", return_value=completed) as run,
        ):
            manager.manage("claude", "native_install", "stable", "official")

        self.assertEqual(
            run.call_args_list[0].args[0],
            ["/home/u/.local/bin/claude", "install", "stable"],
        )


if __name__ == "__main__":
    unittest.main()
