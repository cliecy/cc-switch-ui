import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cc_switch_ui.process import AgentProcess


class AgentProcessTests(unittest.TestCase):
    def test_child_environment_is_sanitized_and_pty_is_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "fake-agent"
            captured = Path(temp_dir) / "environment.txt"
            script.write_text("#!/bin/sh\nenv | sort > \"$1\"\n", encoding="utf-8")
            script.chmod(0o700)

            process = AgentProcess()
            with mock.patch.dict(
                os.environ,
                {
                    "ANTHROPIC_BASE_URL": "https://stale.invalid",
                    "OPENAI_API_KEY": "stale-key",
                },
            ):
                ok, _ = process.start(
                    {"CC_SWITCH_CODEX_API_KEY": "fresh-key"},
                    "test",
                    command=[str(script), str(captured)],
                    clear_env=("ANTHROPIC_BASE_URL", "OPENAI_API_KEY"),
                    client="codex",
                )

            self.assertTrue(ok)
            process.reader_thread.join(timeout=3)
            self.assertFalse(process.reader_thread.is_alive())
            self.assertIsNone(process.master_fd)
            self.assertEqual(process.last_exit_code, 0)

            environment = captured.read_text(encoding="utf-8")
            self.assertNotIn("ANTHROPIC_BASE_URL=", environment)
            self.assertNotIn("OPENAI_API_KEY=", environment)
            self.assertIn("CC_SWITCH_CODEX_API_KEY=fresh-key", environment)

    def test_restart_does_not_let_old_reader_close_new_pty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "fake-agent"
            script.write_text("#!/bin/sh\ncat >/dev/null\n", encoding="utf-8")
            script.chmod(0o700)

            process = AgentProcess()
            first_ok, _ = process.start({}, "first", command=[str(script)])
            self.assertTrue(first_ok)
            first_reader = process.reader_thread
            self.assertTrue(process.stop()[0])

            second_ok, _ = process.start({}, "second", command=[str(script)])
            self.assertTrue(second_ok)
            second_fd = process.master_fd
            first_reader.join(timeout=2)
            time.sleep(0.1)

            self.assertTrue(process.is_running())
            self.assertEqual(process.master_fd, second_fd)
            self.assertTrue(process.send_input("ping\n")[0])
            process.stop()
            process.reader_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
