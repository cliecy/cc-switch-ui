import unittest
from pathlib import Path


class AssetTests(unittest.TestCase):
    def test_root_and_packaged_frontends_are_synchronized(self):
        root = Path(__file__).resolve().parents[1]

        self.assertEqual(
            (root / "index.html").read_text(encoding="utf-8"),
            (root / "src/cc_switch_ui/index.html").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
