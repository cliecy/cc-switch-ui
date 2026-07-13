import stat
import tempfile
import unittest
from pathlib import Path

import cc_switch_ui.config as config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = config.CONFIG_PATH
        config.CONFIG_PATH = Path(self.temp_dir.name) / ".ccm_config"

    def tearDown(self):
        config.CONFIG_PATH = self.original_path
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


if __name__ == "__main__":
    unittest.main()
