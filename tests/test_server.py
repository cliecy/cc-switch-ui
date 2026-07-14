import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cc_switch_ui.config as config
from cc_switch_ui.server import create_app


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = config.CONFIG_PATH
        config.CONFIG_PATH = Path(self.temp_dir.name) / ".ccm_config"
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.app._agent_proc.keepalive = False
        self.app._agent_proc.stop()
        config.CONFIG_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_state_exposes_generic_and_compatible_status(self):
        data = self.client.get("/api/state").get_json()

        self.assertIn("agent_status", data)
        self.assertEqual(data["agent_status"], data["claude_status"])
        self.assertIn("codex_available", data)
        self.assertEqual(data["selected_launch"]["client"], "claude")
        self.assertFalse(data["restart_required"])

    def test_state_marks_selected_provider_change_as_restart_required(self):
        with mock.patch.object(
            self.app._agent_proc,
            "status",
            return_value={
                "running": True,
                "launch": {"provider_id": "deepseek", "client": "claude"},
            },
        ):
            data = self.client.get("/api/state").get_json()

        self.assertTrue(data["restart_required"])

    def test_state_marks_same_provider_account_change_as_restart_required(self):
        with mock.patch.object(
            self.app._agent_proc,
            "status",
            return_value={
                "running": True,
                "launch": {
                    "provider_id": "claude",
                    "client": "claude",
                    "base_url": "https://api.anthropic.com",
                    "model": "",
                    "account_id": "old-account",
                },
            },
        ):
            data = self.client.get("/api/state").get_json()

        self.assertTrue(data["restart_required"])

    def test_directory_picker_reports_missing_path(self):
        response = self.client.get("/api/fs/list?path=/definitely/not/here")

        self.assertEqual(response.status_code, 404)
        self.assertIn("目录不存在", response.get_json()["error"])

    def test_invalid_terminal_dimensions_return_400(self):
        with mock.patch.object(self.app._agent_proc, "stop") as stop:
            response = self.client.post("/api/agent/restart", json={"rows": "bad"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        stop.assert_not_called()

    def test_restart_accepts_visible_session_mode_and_directory(self):
        self.app._agent_proc.last_launch = {
            "session_mode": "new",
            "rows": 24,
            "cols": 80,
            "cwd": None,
        }
        launch = {
            "ready": True,
            "env": {},
            "label": "Claude 官方",
            "provider_id": "claude",
            "provider_label": "Claude 官方",
            "client": "claude",
            "base_url": "https://api.anthropic.com",
            "model": "",
            "account_id": None,
            "account_name": "",
            "command": ["claude", "--resume"],
            "clear_env": (),
        }
        with (
            mock.patch("cc_switch_ui.server.build_launch_for_active", return_value=launch) as build,
            mock.patch.object(self.app._agent_proc, "stop"),
            mock.patch.object(self.app._agent_proc, "start", return_value=(True, "已启动")) as start,
            mock.patch("cc_switch_ui.server.time.sleep"),
        ):
            response = self.client.post(
                "/api/agent/restart",
                json={
                    "rows": 30,
                    "cols": 100,
                    "cwd": self.temp_dir.name,
                    "session_mode": "resume",
                },
            )

        self.assertEqual(response.status_code, 200)
        build.assert_called_once_with("resume")
        self.assertEqual(start.call_args.kwargs["cwd"], self.temp_dir.name)
        self.assertEqual(start.call_args.kwargs["launch_snapshot"]["session_mode"], "resume")

    def test_invalid_terminal_input_returns_400(self):
        response = self.client.post("/api/agent/input", json={"raw": 42})

        self.assertEqual(response.status_code, 400)

    def test_codex_provider_requires_url_model_and_key(self):
        cfg = config.load_config()
        cfg["current_provider"] = "codex_custom"
        config.save_config(cfg)

        response = self.client.post(
            "/api/agent/start",
            json={"rows": 24, "cols": 80, "session_mode": "new"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Base URL", response.get_json()["error"])

    def test_old_status_route_remains_available(self):
        response = self.client.get("/api/claude/status")

        self.assertEqual(response.status_code, 200)

    def test_cli_management_is_disabled_by_default(self):
        response = self.client.post(
            "/api/cli/manage",
            json={
                "agent": "codex",
                "action": "npm_install",
                "version": "latest",
                "registry": "official",
                "confirm": "npm_install:codex",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("--allow-cli-management", response.get_json()["error"])

    def test_cli_management_requires_exact_confirmation(self):
        manager = mock.Mock()
        enabled_app = create_app(allow_cli_management=True, cli_manager=manager)
        enabled_app.testing = True

        response = enabled_app.test_client().post(
            "/api/cli/manage",
            json={"agent": "codex", "action": "npm_install", "confirm": "yes"},
        )

        self.assertEqual(response.status_code, 400)
        manager.manage.assert_not_called()

    def test_cli_management_calls_allowlisted_manager(self):
        manager = mock.Mock()
        manager.manage.return_value = {"ok": True, "output": "done"}
        enabled_app = create_app(allow_cli_management=True, cli_manager=manager)
        enabled_app.testing = True

        response = enabled_app.test_client().post(
            "/api/cli/manage",
            json={
                "agent": "claude",
                "action": "self_update",
                "version": "latest",
                "registry": "official",
                "confirm": "self_update:claude",
            },
        )

        self.assertEqual(response.status_code, 200)
        manager.manage.assert_called_once_with(
            agent="claude",
            action="self_update",
            version="latest",
            registry="official",
        )

    def test_cli_management_rejects_remote_request(self):
        manager = mock.Mock()
        enabled_app = create_app(allow_cli_management=True, cli_manager=manager)
        enabled_app.testing = True

        response = enabled_app.test_client().post(
            "/api/cli/manage",
            json={
                "agent": "codex",
                "action": "npm_install",
                "version": "latest",
                "registry": "official",
                "confirm": "npm_install:codex",
            },
            environ_base={"REMOTE_ADDR": "203.0.113.20"},
        )

        self.assertEqual(response.status_code, 403)
        manager.manage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
