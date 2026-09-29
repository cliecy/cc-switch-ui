import json
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import cc_switch_ui.config as config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = config.CONFIG_PATH
        self.original_state_dir = config.STATE_DIR
        self.original_recovery_notice = config._config_recovery_notice
        config.CONFIG_PATH = Path(self.temp_dir.name) / ".ccm_config"
        config._config_recovery_notice = None

    def tearDown(self):
        config.CONFIG_PATH = self.original_path
        config.STATE_DIR = self.original_state_dir
        config._config_recovery_notice = self.original_recovery_notice
        self.temp_dir.cleanup()

    def test_default_config_includes_codex_and_private_permissions(self):
        cfg = config.load_config()

        self.assertEqual(cfg["providers"]["codex_custom"]["client"], "codex")
        self.assertEqual(
            stat.S_IMODE(config.CONFIG_PATH.stat().st_mode),
            0o600,
        )

    def test_existing_config_is_extended_without_overwriting_accounts(self):
        cfg = config._default_config()
        cfg["providers"].pop("codex_custom")
        cfg["providers"]["claude"].pop("client")
        cfg["providers"]["claude"]["accounts"] = [
            {"id": "existing", "name": "kept", "api_key": "existing-key"}
        ]
        config.save_config(cfg)

        migrated = config.load_config()

        self.assertIn("codex_custom", migrated["providers"])
        self.assertEqual(migrated["providers"]["claude"]["client"], "claude")
        self.assertEqual(
            migrated["providers"]["claude"]["accounts"][0]["id"],
            "existing",
        )

    def test_codex_launch_uses_temporary_custom_provider(self):
        cfg = config.load_config()
        provider = cfg["providers"]["codex_custom"]
        provider["base_url"] = "https://proxy.example.com/v1"
        provider["model"] = "custom-coder"
        provider["accounts"] = [
            {"id": "account-1", "name": "server", "api_key": "secret-key"}
        ]
        provider["active_account"] = "account-1"
        cfg["current_provider"] = "codex_custom"
        config.save_config(cfg)

        launch = config.build_launch_for_active("continue")

        self.assertTrue(launch["ready"])
        self.assertEqual(launch["client"], "codex")
        self.assertEqual(launch["command"][:3], ["codex", "resume", "--last"])
        self.assertIn('model_provider="cc_switch_ui"', launch["command"])
        self.assertIn(
            'model_providers.cc_switch_ui.base_url="https://proxy.example.com/v1"',
            launch["command"],
        )
        self.assertEqual(launch["env"], {"CC_SWITCH_CODEX_API_KEY": "secret-key"})
        self.assertEqual(launch["provider_id"], "codex_custom")
        self.assertEqual(launch["account_id"], "account-1")
        self.assertEqual(launch["account_name"], "server")
        self.assertIn("ANTHROPIC_API_KEY", launch["clear_env"])
        self.assertIn("ANTHROPIC_BASE_URL", launch["clear_env"])
        self.assertIn("OPENAI_API_KEY", launch["clear_env"])
        self.assertIn("CC_SWITCH_CODEX_API_KEY", launch["clear_env"])
        self.assertNotIn("secret-key", " ".join(launch["command"]))

    def test_official_claude_can_use_existing_login_without_api_key(self):
        config.load_config()

        launch = config.build_launch_for_active("new")

        self.assertTrue(launch["ready"])
        self.assertEqual(launch["command"], ["claude"])
        self.assertEqual(launch["env"], {})
        self.assertIn("OPENAI_API_KEY", launch["clear_env"])
        self.assertIn("CC_SWITCH_CODEX_API_KEY", launch["clear_env"])

    def test_launch_fingerprint_changes_for_active_credentials(self):
        cfg = config.load_config()
        provider = cfg["providers"]["claude"]
        provider["accounts"] = [
            {"id": "account-1", "name": "main", "api_key": "first-key"}
        ]
        provider["active_account"] = "account-1"
        config.save_config(cfg)

        first = config.launch_fingerprint_for_active()
        provider["accounts"][0]["api_key"] = "second-key"
        config.save_config(cfg)
        second = config.launch_fingerprint_for_active()
        provider["auth_var"] = "ANTHROPIC_AUTH_TOKEN"
        config.save_config(cfg)
        third = config.launch_fingerprint_for_active()

        self.assertNotEqual(first, second)
        self.assertNotEqual(second, third)

    def test_codex_provider_label_is_part_of_launch_fingerprint(self):
        cfg = config.load_config()
        provider = cfg["providers"]["codex_custom"]
        provider["label"] = "Codex Old Label"
        provider["base_url"] = "https://proxy.example.com/v1"
        provider["model"] = "custom-coder"
        provider["accounts"] = [
            {"id": "account-1", "name": "server", "api_key": "secret-key"}
        ]
        provider["active_account"] = "account-1"
        cfg["current_provider"] = "codex_custom"
        config.save_config(cfg)

        first = config.launch_fingerprint_for_active()
        first_command = config.build_launch_for_active()["command"]
        provider["label"] = "Codex New Label"
        config.save_config(cfg)
        second = config.launch_fingerprint_for_active()
        second_command = config.build_launch_for_active()["command"]

        self.assertNotEqual(first, second)
        self.assertIn('model_providers.cc_switch_ui.name="Codex Old Label"', first_command)
        self.assertIn('model_providers.cc_switch_ui.name="Codex New Label"', second_command)

    def test_anthropic_token_provider_clears_conflicting_api_key(self):
        cfg = config.load_config()
        provider = cfg["providers"]["openrouter"]
        provider["accounts"] = [
            {"id": "account-1", "name": "router", "api_key": "router-key"}
        ]
        provider["active_account"] = "account-1"
        cfg["current_provider"] = "openrouter"
        config.save_config(cfg)

        launch = config.build_launch_for_active("new")

        self.assertEqual(launch["env"]["ANTHROPIC_AUTH_TOKEN"], "router-key")
        self.assertEqual(launch["env"]["ANTHROPIC_API_KEY"], "")

    def test_public_state_masks_keys(self):
        cfg = config.load_config()
        provider = cfg["providers"]["codex_custom"]
        provider["accounts"] = [
            {"id": "account-1", "name": "server", "api_key": "secret-key-value"},
            {"id": "account-2", "name": "short", "api_key": "abcdefghijkl"},
        ]
        provider["active_account"] = "account-1"
        config.save_config(cfg)

        accounts = {
            a["id"]: a for a in config.public_state()["providers"]["codex_custom"]["accounts"]
        }

        self.assertNotEqual(accounts["account-1"]["key_masked"], "secret-key-value")
        self.assertTrue(accounts["account-1"]["has_key"])
        self.assertEqual(config.mask_key("abcdefgh"), "•" * 8)
        masked = accounts["account-2"]["key_masked"]
        self.assertEqual(masked, "ab" + "•" * 6 + "kl")
        self.assertLessEqual(sum(1 for ch in "abcdefghijkl" if ch in masked), 4)
        self.assertEqual(config.mask_key("x" * 17), "xxxx" + "•" * 6 + "xxxx")

    def test_public_state_distinguishes_ready_claude_login_from_unready_codex(self):
        config.load_config()

        with mock.patch(
            "cc_switch_ui.config.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}" if name in {"claude", "codex"} else None,
        ):
            state = config.public_state()

        self.assertTrue(state["providers"]["claude"]["readiness"]["ready"])
        self.assertEqual(
            state["providers"]["claude"]["readiness"]["auth_mode"],
            "claude_login",
        )
        self.assertFalse(state["providers"]["codex_custom"]["readiness"]["ready"])
        self.assertEqual(state["selected_launch"]["client"], "claude")
        self.assertEqual(state["selected_launch"]["base_url"], "https://api.anthropic.com")

    def test_invalid_config_is_backed_up_and_reported(self):
        config.CONFIG_PATH.write_text("{not-json", encoding="utf-8")

        recovered = config.load_config()
        warning = config.public_state()["config_warning"]
        backups = list(config.CONFIG_PATH.parent.glob(".ccm_config.corrupt-*"))

        self.assertEqual(recovered["current_provider"], "claude")
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "{not-json")
        self.assertEqual(warning["backup_path"], str(backups[0]))

    def test_custom_anthropic_provider_requires_base_url(self):
        cfg = config.load_config()
        provider = cfg["providers"]["custom"]
        provider["accounts"] = [
            {"id": "account-1", "name": "custom", "api_key": "secret-key"}
        ]
        provider["active_account"] = "account-1"
        cfg["current_provider"] = "custom"
        config.save_config(cfg)

        launch = config.build_launch_for_active("new")

        self.assertFalse(launch["ready"])
        self.assertIn("Base URL", launch["error"])


    def test_configure_paths_redirects_config_and_state(self):
        target = Path(self.temp_dir.name) / "alt"
        original_config = config.CONFIG_PATH
        original_state = config.STATE_DIR
        try:
            config.configure_paths(target)
            self.assertEqual(config.CONFIG_PATH, target / ".ccm_config")
            self.assertEqual(config.STATE_DIR, target)

            config.load_config()

            self.assertTrue((target / ".ccm_config").exists())
            self.assertEqual(config.trash_path(), target / ".ccm_config.trash")
            self.assertFalse(config.CONFIG_PATH.parent.joinpath("..", ".ccm_config").exists())
        finally:
            config.CONFIG_PATH = original_config
            config.STATE_DIR = original_state

    def test_configure_paths_without_arg_is_noop(self):
        config.configure_paths(None)
        self.assertEqual(config.CONFIG_PATH, Path(self.temp_dir.name) / ".ccm_config")
        self.assertEqual(config.STATE_DIR, self.original_state_dir)

    def test_malformed_providers_are_isolated_with_backup(self):
        shapes = [
            ("deepseek", {"accounts": "oops"}),
            ("kimi", {"accounts": [{"name": 1}]}),
            ("glm", {"active_account": 42}),
            ("qwen", {"base_url": 123}),
            ("openrouter", ["not", "a", "dict"]),
        ]
        for pid, value in shapes:
            with self.subTest(provider=pid):
                for old in config.CONFIG_PATH.parent.glob(".ccm_config.corrupt-*"):
                    old.unlink()
                config._config_recovery_notice = None
                cfg = config._default_config()
                if isinstance(value, list):
                    cfg["providers"][pid] = value
                else:
                    cfg["providers"][pid].update(value)
                config.save_config(cfg)

                cfg2 = config.load_config()
                state = config.public_state()

                self.assertNotIn(pid, cfg2["providers"])
                self.assertIn(pid, state["providers_broken"])
                warning = state["config_warning"]
                self.assertEqual(
                    warning["message"],
                    "部分供应商配置格式无效，已隔离到 providers_broken 并可经备份恢复",
                )
                self.assertIn(pid, warning["error"])
                backups = list(config.CONFIG_PATH.parent.glob(".ccm_config.corrupt-*"))
                self.assertEqual(len(backups), 1)
                self.assertIn(
                    pid,
                    json.loads(backups[0].read_text(encoding="utf-8"))["providers"],
                )

    def test_non_string_current_provider_falls_back_to_claude(self):
        cfg = config._default_config()
        cfg["current_provider"] = 5
        config.save_config(cfg)

        cfg2 = config.load_config()

        self.assertEqual(cfg2["current_provider"], "claude")
        self.assertIsNone(config._config_recovery_notice)
        self.assertEqual(config.public_state()["providers_broken"], [])

    def test_valid_config_is_not_isolated(self):
        cfg = config.load_config()
        provider = cfg["providers"]["deepseek"]
        provider["accounts"] = [
            {"id": "a1", "name": "one", "api_key": "k-1234"},
            {"id": "a2", "name": "two", "api_key": "k-5678", "shared": True},
        ]
        provider["active_account"] = "a1"
        config.save_config(cfg)

        state = config.public_state()

        self.assertIsNone(config._config_recovery_notice)
        self.assertEqual(state["providers_broken"], [])
        self.assertEqual(state["providers"]["deepseek"]["active_account"], "a1")
        self.assertEqual(len(state["providers"]["deepseek"]["accounts"]), 2)

    def test_base_url_credentials_masked_in_state_but_raw_in_launch(self):
        cfg = config.load_config()
        provider = cfg["providers"]["deepseek"]
        provider["base_url"] = "https://u:p@example.com"
        provider["accounts"] = [{"id": "a1", "name": "one", "api_key": "secret"}]
        provider["active_account"] = "a1"
        cfg["current_provider"] = "deepseek"
        config.save_config(cfg)

        state = config.public_state()
        self.assertEqual(
            state["providers"]["deepseek"]["base_url"],
            "https://***:***@example.com",
        )
        self.assertEqual(
            state["selected_launch"]["base_url"],
            "https://***:***@example.com",
        )
        launch = config.build_launch_for_active("new")
        self.assertTrue(launch["ready"])
        self.assertEqual(
            launch["env"]["ANTHROPIC_BASE_URL"], "https://u:p@example.com"
        )

    def test_move_to_trash_preserves_key_and_restores(self):
        cfg = config.load_config()
        provider = cfg["providers"]["deepseek"]
        account = {"id": "a1", "name": "one", "api_key": "super-secret-key-0123456789"}
        provider["accounts"] = [account]
        provider["active_account"] = "a1"
        config.save_config(cfg)
        before = config.public_state()["providers"]["deepseek"]["accounts"][0]["key_masked"]

        config.move_account_to_trash("deepseek", account)

        trash_raw = json.loads(config.trash_path().read_text(encoding="utf-8"))
        self.assertEqual(trash_raw["deleted"][0]["account"]["api_key"], account["api_key"])
        self.assertEqual(trash_raw["deleted"][0]["provider"], "deepseek")

        restored, error = config.restore_account("deepseek", "a1")
        self.assertIsNone(error)
        self.assertEqual(restored["api_key"], account["api_key"])
        self.assertEqual(config._load_trash(), [])

        provider["accounts"].append(restored)
        config.save_config(cfg)
        after = config.public_state()["providers"]["deepseek"]["accounts"][0]["key_masked"]
        self.assertEqual(before, after)

        miss, err = config.restore_account("deepseek", "does-not-exist")
        self.assertIsNone(miss)
        self.assertIsNotNone(err)

    def test_trash_expires_after_seven_days(self):
        old = datetime.now(timezone.utc) - timedelta(days=8)
        config.trash_path().write_text(
            json.dumps(
                {
                    "deleted": [
                        {
                            "provider": "deepseek",
                            "account": {"id": "old", "name": "old", "api_key": "k"},
                            "deleted_at": old.isoformat(),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(config._load_trash(), [])

        rewritten = json.loads(config.trash_path().read_text(encoding="utf-8"))
        self.assertEqual(rewritten["deleted"], [])

    def test_corrupt_trash_is_renamed_not_fatal(self):
        config.trash_path().write_text("{{{" , encoding="utf-8")

        self.assertEqual(config._load_trash(), [])

        self.assertFalse(config.trash_path().exists())
        corrupted = list(config.trash_path().parent.glob(".ccm_config.trash.corrupt-*"))
        self.assertEqual(len(corrupted), 1)
        self.assertEqual(corrupted[0].read_text(encoding="utf-8"), "{{{")

    def test_shared_flag_roundtrip_and_coercion(self):
        cfg = config.load_config()
        provider = cfg["providers"]["deepseek"]
        provider["accounts"] = [
            {"id": "a1", "name": "shared", "api_key": "k1", "shared": True},
            {"id": "a2", "name": "solo", "api_key": "k2"},
        ]
        config.save_config(cfg)

        cfg2 = config.load_config()
        self.assertIs(cfg2["providers"]["deepseek"]["accounts"][0]["shared"], True)
        self.assertFalse(cfg2["providers"]["deepseek"]["accounts"][1]["shared"])
        by_id = {
            a["id"]: a for a in config.public_state()["providers"]["deepseek"]["accounts"]
        }
        self.assertTrue(by_id["a1"]["shared"])
        self.assertFalse(by_id["a2"]["shared"])

        provider["accounts"][0]["shared"] = "yes"
        config.save_config(cfg)
        state = config.public_state()
        self.assertFalse(state["providers"]["deepseek"]["accounts"][0]["shared"])


class LaunchSplitTests(ConfigTests):
    """3.1 launch 函数拆分：wrapper 与 provider 版本行为一致 + account_id 覆盖。"""

    def test_wrappers_match_provider_functions(self):
        cfg = config.load_config()
        cfg["providers"]["deepseek"]["base_url"] = "https://api.deepseek.com"
        cfg["providers"]["deepseek"]["accounts"] = [
            {"id": "a1", "name": "one", "api_key": "deepseek-key-1"}
        ]
        cfg["providers"]["deepseek"]["active_account"] = "a1"
        cfg["current_provider"] = "deepseek"
        config.save_config(cfg)

        for mode in ("new", "continue", "resume"):
            via_wrapper = config.build_launch_for_active(mode)
            via_provider = config.build_launch_for_provider(
                "deepseek", mode
            )
            self.assertEqual(via_wrapper, via_provider)

        self.assertEqual(
            config.launch_fingerprint_for_active(),
            config.launch_fingerprint_for_provider("deepseek"),
        )

    def test_account_id_override_selects_that_account(self):
        cfg = config.load_config()
        provider = cfg["providers"]["deepseek"]
        provider["base_url"] = "https://api.deepseek.com"
        provider["accounts"] = [
            {"id": "a1", "name": "one", "api_key": "key-active"},
            {"id": "a2", "name": "two", "api_key": "key-pinned"},
        ]
        provider["active_account"] = "a1"
        cfg["current_provider"] = "deepseek"
        config.save_config(cfg)

        default_launch = config.build_launch_for_provider("deepseek")
        self.assertEqual(
            default_launch["env"]["ANTHROPIC_AUTH_TOKEN"], "key-active"
        )

        pinned = config.build_launch_for_provider(
            "deepseek", "new", "a2"
        )
        self.assertEqual(pinned["account_id"], "a2")
        self.assertEqual(pinned["env"]["ANTHROPIC_AUTH_TOKEN"], "key-pinned")

        # 指定不存在的 account → 回落激活账号
        fallback = config.build_launch_for_provider(
            "deepseek", "new", "does-not-exist"
        )
        self.assertEqual(fallback["account_id"], "a1")

        # fingerprint 跟随 account_id
        self.assertNotEqual(
            config.launch_fingerprint_for_provider("deepseek"),
            config.launch_fingerprint_for_provider("deepseek", "a2"),
        )
        self.assertEqual(
            config.launch_fingerprint_for_provider("deepseek", "does-not-exist"),
            config.launch_fingerprint_for_provider("deepseek"),
        )


class SessionConfigTests(ConfigTests):
    """3.1 会话数据模型：默认补齐 + 非法条目隔离。"""

    def test_sessions_defaulted_and_persisted(self):
        cfg = config.load_config()
        self.assertEqual(cfg["sessions"], [])

        cfg["sessions"] = [{"id": "s1", "name": "work"}]
        config.save_config(cfg)

        loaded = config.load_config()
        self.assertEqual(loaded["sessions"][0]["id"], "s1")
        self.assertEqual(loaded["sessions"][0]["name"], "work")
        self.assertIsNone(loaded["sessions"][0]["cwd"])
        self.assertIsNone(loaded["sessions"][0]["provider_id"])
        self.assertEqual(loaded["sessions"][0]["session_mode"], "new")
        self.assertIsNone(loaded["sessions"][0]["account_id"])

    def test_malformed_sessions_isolated_to_broken(self):
        cfg = config.load_config()
        cfg["sessions"] = [
            {"id": "good", "name": "ok", "session_mode": "resume"},
            {"id": 42},
            "not-a-dict",
        ]
        config.save_config(cfg)

        loaded = config.load_config()

        self.assertEqual([s["id"] for s in loaded["sessions"]], ["good"])
        self.assertEqual(len(loaded["sessions_broken"]), 2)
        # 隔离触发恢复通知（可经备份恢复）
        self.assertIsNotNone(config._config_recovery_notice)

    def test_sessions_not_list_coerced(self):
        cfg = config.load_config()
        cfg["sessions"] = {"oops": True}
        config.save_config(cfg)

        loaded = config.load_config()
        self.assertEqual(loaded["sessions"], [])


class ProfileConfigTests(ConfigTests):
    """4.2 档案数据模型：默认补齐 + 非法条目隔离。"""

    def test_profiles_defaulted_and_persisted(self):
        cfg = config.load_config()
        self.assertEqual(cfg["profiles"], [])

        cfg["profiles"] = [
            {"id": "p1", "name": "work", "provider_id": "deepseek"}
        ]
        config.save_config(cfg)

        loaded = config.load_config()
        self.assertEqual(loaded["profiles"][0]["id"], "p1")
        self.assertEqual(loaded["profiles"][0]["name"], "work")
        self.assertEqual(loaded["profiles"][0]["provider_id"], "deepseek")
        self.assertIsNone(loaded["profiles"][0]["cwd"])
        self.assertIsNone(loaded["profiles"][0]["account_id"])
        self.assertEqual(loaded["profiles"][0]["session_mode"], "new")

    def test_malformed_profiles_isolated_to_broken(self):
        cfg = config.load_config()
        cfg["profiles"] = [
            {"id": "good", "name": "ok", "provider_id": "claude", "session_mode": "resume"},
            {"name": 42, "provider_id": "claude"},
            "not-a-dict",
        ]
        config.save_config(cfg)

        loaded = config.load_config()

        self.assertEqual([p["id"] for p in loaded["profiles"]], ["good"])
        self.assertEqual(len(loaded["profiles_broken"]), 2)
        # 隔离触发恢复通知（可经备份恢复）
        self.assertIsNotNone(config._config_recovery_notice)

    def test_profiles_not_list_coerced(self):
        cfg = config.load_config()
        cfg["profiles"] = {"oops": True}
        config.save_config(cfg)

        loaded = config.load_config()
        self.assertEqual(loaded["profiles"], [])



if __name__ == "__main__":
    unittest.main()
