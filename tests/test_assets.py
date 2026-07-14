import unittest
from pathlib import Path


class AssetTests(unittest.TestCase):
    def test_root_and_packaged_frontends_are_synchronized(self):
        root = Path(__file__).resolve().parents[1]

        self.assertEqual(
            (root / "index.html").read_text(encoding="utf-8"),
            (root / "src/cc_switch_ui/index.html").read_text(encoding="utf-8"),
        )

    def test_frontend_separates_running_agent_from_next_launch(self):
        root = Path(__file__).resolve().parents[1]
        frontend = (root / "src/cc_switch_ui/index.html").read_text(encoding="utf-8")

        self.assertIn('id="run-agent"', frontend)
        self.assertIn('id="next-agent"', frontend)
        self.assertIn('id="switch-notice"', frontend)
        self.assertIn('role="dialog"', frontend)
        self.assertIn('type="password"', frontend)


if __name__ == "__main__":
    unittest.main()
