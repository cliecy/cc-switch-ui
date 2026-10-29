import json
import tempfile
import unittest
from pathlib import Path

import cc_switch_ui.config as config
from cc_switch_ui import tui


def _sample_state():
    """构造与 public_state 输出同构的最小 state（手写，避免依赖 load）。"""
    return {
        "current_provider": "deepseek",
        "providers": {
            "claude": {
                "label": "Claude 官方",
                "client": "claude",
                "accounts": [{"id": "a-1", "name": "主号", "key_masked": "sk••••abcd", "shared": False}],
                "active_account": "a-1",
                "readiness": {"ready": True, "missing": []},
            },
            "deepseek": {
                "label": "DeepSeek",
                "client": "claude",
                "accounts": [
                    {"id": "ds-1", "name": "ds", "key_masked": "sk••••5678", "shared": False},
                    {"id": "ds-2", "name": "共享", "key_masked": "", "shared": True},
                ],
                "active_account": "ds-1",
                "readiness": {"ready": True, "missing": []},
            },
            "custom": {
                "label": "Custom",
                "client": "codex",
                "accounts": [],
                "active_account": None,
                "readiness": {"ready": False, "missing": ["Base URL"]},
            },
        },
    }


class BuildRowsTests(unittest.TestCase):
    def test_rows_order_flags_and_markers(self):
        rows = tui.build_rows(_sample_state())
        kinds = [(r["kind"], r["provider_id"], r["account_id"]) for r in rows]
        self.assertEqual(kinds, [
            ("provider", "claude", None),
            ("account", "claude", "a-1"),
            ("provider", "deepseek", None),
            ("account", "deepseek", "ds-1"),
            ("account", "deepseek", "ds-2"),
            ("provider", "custom", None),
        ])
        # 当前 provider 的 provider 行 active=True；其激活账号 active=True
        deepseek_row = rows[2]
        ds1_row = rows[3]
        ds2_row = rows[4]
        self.assertTrue(deepseek_row["active"])
        self.assertTrue(ds1_row["active"])
        self.assertFalse(ds2_row["active"])
        # 非当前 provider 的账号行 active 恒为 False
        self.assertFalse(rows[1]["active"])
        # 未就绪 provider 标记 ✗
        self.assertIn("[✗]", rows[5]["text"])
        self.assertIn("[✓]", deepseek_row["text"])
        # 无 key 的账号显示「（未配置）」
        self.assertIn("（未配置）", ds2_row["text"])

    def test_empty_state(self):
        self.assertEqual(tui.build_rows({"current_provider": None, "providers": {}}), [])


class MoveSelectionTests(unittest.TestCase):
    def setUp(self):
        self.rows = tui.build_rows(_sample_state())

    def test_up_down_bounds(self):
        self.assertEqual(tui.move_selection(self.rows, 0, "up"), 0)
        self.assertEqual(tui.move_selection(self.rows, 0, "down"), 1)
        bottom = len(self.rows) - 1
        self.assertEqual(tui.move_selection(self.rows, bottom, "down"), bottom)

    def test_home_end(self):
        self.assertEqual(tui.move_selection(self.rows, 3, "home"), 0)
        self.assertEqual(tui.move_selection(self.rows, 0, "end"), len(self.rows) - 1)

    def test_unknown_key_and_empty_rows(self):
        self.assertEqual(tui.move_selection(self.rows, 2, "zzz"), 2)
        self.assertEqual(tui.move_selection([], 0, "down"), 0)


class SwitchCurrentTests(unittest.TestCase):
    """config.switch_current：Web 与 TUI 共用的切换核心语义。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_config_path = config.CONFIG_PATH
        self.original_state_dir = config.STATE_DIR
        config.CONFIG_PATH = self.root / ".ccm_config"
        config.STATE_DIR = self.root / "state"
        cfg = config.load_config()
        cfg["providers"]["deepseek"]["accounts"] = [
            {"id": "ds-1", "name": "ds", "api_key": "ds-key"},
            {"id": "ds-2", "name": "ds2", "api_key": "ds-key2"},
        ]
        cfg["providers"]["deepseek"]["active_account"] = "ds-1"
        cfg["current_provider"] = "claude"
        config.save_config(cfg)

    def tearDown(self):
        config.CONFIG_PATH = self.original_config_path
        config.STATE_DIR = self.original_state_dir
        self.temp_dir.cleanup()

    def test_switch_provider_persists_current(self):
        ok, err = config.switch_current("deepseek")
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(config.load_config()["current_provider"], "deepseek")

    def test_unknown_provider_rejected_and_persisted_state_untouched(self):
        ok, err = config.switch_current("nope")
        self.assertFalse(ok)
        self.assertEqual(err, "unknown_provider")
        self.assertEqual(config.load_config()["current_provider"], "claude")

    def test_activate_account_updates_both_fields(self):
        ok, err = config.switch_current("deepseek", "ds-2")
        self.assertTrue(ok)
        self.assertIsNone(err)
        cfg = config.load_config()
        self.assertEqual(cfg["current_provider"], "deepseek")
        self.assertEqual(cfg["providers"]["deepseek"]["active_account"], "ds-2")

    def test_unknown_account_rejected_without_touching_current(self):
        ok, err = config.switch_current("deepseek", "nope")
        self.assertFalse(ok)
        self.assertEqual(err, "unknown_account")
        cfg = config.load_config()
        # 账号不存在时，配置完全保持原样（先校验、后写盘）
        self.assertEqual(cfg["current_provider"], "claude")
        self.assertEqual(cfg["providers"]["deepseek"]["active_account"], "ds-1")

    def test_other_providers_account_rejected(self):
        ok, err = config.switch_current("deepseek", "a-1")
        self.assertFalse(ok)
        self.assertEqual(err, "unknown_account")

    def test_apply_enter_writes_audit(self):
        ok, msg = tui.apply_enter("deepseek", "ds-2")
        self.assertTrue(ok)
        self.assertIn("已切换", msg)
        audit = config.STATE_DIR / config.AUDIT_FILE
        self.assertTrue(audit.exists())
        line = json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(line["action"], "tui_switch")
        self.assertEqual(line["target"], "deepseek:ds-2")


class TuiEntrypointTests(unittest.TestCase):
    def test_app_entrypoint_importable(self):
        # argparse 分支能被构造：tui 子命令与 --read-only 兼容（入口可导入即可）
        from cc_switch_ui.app import main  # noqa: F401
        self.assertTrue(callable(main))


if __name__ == "__main__":
    unittest.main()