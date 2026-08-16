import unittest
from pathlib import Path


class AssetTests(unittest.TestCase):
    def test_root_and_packaged_frontends_are_synchronized(self):
        root = Path(__file__).resolve().parents[1]

        self.assertEqual(
            (root / "index.html").read_text(encoding="utf-8"),
            (root / "src/cc_switch_ui/index.html").read_text(encoding="utf-8"),
        )

    def test_frontend_exposes_primary_views_and_next_launch_semantics(self):
        root = Path(__file__).resolve().parents[1]
        frontend = (root / "src/cc_switch_ui/index.html").read_text(encoding="utf-8")

        for element_id in (
            "view-run",
            "view-connections",
            "view-tools",
            "running-summary",
            "run-meta",
            "switch-notice",
        ):
            self.assertIn(f'id="{element_id}"', frontend)
        for view in ("run", "connections", "tools"):
            self.assertIn(f'data-view="{view}"', frontend)

        self.assertIn('role="dialog"', frontend)
        self.assertIn('type="password"', frontend)
        self.assertIn("codexAuthOption.hidden = !isCodex", frontend)
        self.assertIn("留空 = 当前工作目录", frontend)
        for obsolete_label in ("设为当前", "使用中", "终端已连接"):
            self.assertNotIn(obsolete_label, frontend)


if __name__ == "__main__":
    unittest.main()
