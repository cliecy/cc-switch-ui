import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cc_switch_ui.config as config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = config.CONFIG_PATH
        self.original_recovery_notice = config._config_recovery_notice
        config.CONFIG_PATH = Path(self.temp_dir.name) / ".ccm_config"
        config._config_recovery_notice = None

    def tearDown(self):
        config.CONFIG_PATH = self.original_path
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
        self.assertNotIn("OPENAI_API_KEY", launch["clear_env"])
        self.assertNotIn("secret-key", " ".join(launch["command"]))

    def test_official_claude_can_use_existing_login_without_api_key(self):
        config.load_config()

        launch = config.build_launch_for_active("new")

        self.assertTrue(launch["ready"])
        self.assertEqual(launch["command"], ["claude"])
        self.assertEqual(launch["env"], {})

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
            {"id": "account-1", "name": "server", "api_key": "secret-key-value"}
        ]
        provider["active_account"] = "account-1"
        config.save_config(cfg)

        account = config.public_state()["providers"]["codex_custom"]["accounts"][0]

        self.assertNotEqual(account["key_masked"], "secret-key-value")
        self.assertTrue(account["has_key"])

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


if __name__ == "__main__":
    unittest.main()
