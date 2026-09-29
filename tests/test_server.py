import contextlib
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import cc_switch_ui.app as app_module
import cc_switch_ui.config as config
from cc_switch_ui import logutil
from cc_switch_ui.server import create_app


class ServerBase(unittest.TestCase):
    """共享 setUp：隔离配置与状态目录（审计/日志不碰真实 HOME）。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_config_path = config.CONFIG_PATH
        self.original_state_dir = config.STATE_DIR
        config.CONFIG_PATH = self.root / ".ccm_config"
        config.STATE_DIR = self.root / "state"
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.app._agent_proc.keepalive = False
        self.app._agent_proc.stop()
        config.CONFIG_PATH = self.original_config_path
        config.STATE_DIR = self.original_state_dir
        self.temp_dir.cleanup()


class ServerTests(ServerBase):
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

    def test_state_marks_active_api_key_change_as_restart_required(self):
        cfg = config.load_config()
        provider = cfg["providers"]["claude"]
        provider["accounts"] = [
            {"id": "account-1", "name": "main", "api_key": "old-key"}
        ]
        provider["active_account"] = "account-1"
        config.save_config(cfg)
        self.app._agent_proc._launch_signature = config.launch_fingerprint_for_active()

        running_status = {
            "running": True,
            "launch": {
                "provider_id": "claude",
                "client": "claude",
                "base_url": "https://api.anthropic.com",
                "model": "",
                "account_id": "account-1",
            },
        }
        with mock.patch.object(self.app._agent_proc, "status", return_value=running_status):
            response = self.client.put(
                "/api/account/account-1",
                json={"provider": "claude", "name": "main", "api_key": "new-key"},
            )
            state = self.client.get("/api/state").get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(state["restart_required"])

    def test_state_marks_active_auth_var_change_as_restart_required(self):
        cfg = config.load_config()
        provider = cfg["providers"]["claude"]
        provider["accounts"] = [
            {"id": "account-1", "name": "main", "api_key": "key"}
        ]
        provider["active_account"] = "account-1"
        config.save_config(cfg)
        self.app._agent_proc._launch_signature = config.launch_fingerprint_for_active()

        running_status = {
            "running": True,
            "launch": {
                "provider_id": "claude",
                "client": "claude",
                "base_url": "https://api.anthropic.com",
                "model": "",
                "account_id": "account-1",
            },
        }
        with mock.patch.object(self.app._agent_proc, "status", return_value=running_status):
            response = self.client.put(
                "/api/provider/claude",
                json={"auth_var": "ANTHROPIC_AUTH_TOKEN"},
            )
            state = self.client.get("/api/state").get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(state["restart_required"])

    def test_provider_rejects_auth_var_for_wrong_client(self):
        response = self.client.put(
            "/api/provider/claude",
            json={"auth_var": "CC_SWITCH_CODEX_API_KEY"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            config.load_config()["providers"]["claude"]["auth_var"],
            "ANTHROPIC_API_KEY",
        )

    def test_directory_picker_reports_missing_path(self):
        missing = Path.home() / "definitely-not-here-cc-switch-test"
        response = self.client.get(f"/api/fs/list?path={missing}")

        self.assertEqual(response.status_code, 404)
        self.assertIn("目录不存在", response.get_json()["error"])

    def test_invalid_terminal_dimensions_return_400(self):
        with mock.patch.object(self.app._agent_proc, "stop") as stop:
            response = self.client.post("/api/agent/restart", json={"rows": "bad"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        stop.assert_not_called()

    def test_restart_accepts_visible_session_mode_and_directory(self):
        cfg = config.load_config()
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
            mock.patch("cc_switch_ui.server.load_config", return_value=cfg) as load,
            mock.patch("cc_switch_ui.server.build_launch_for_active", return_value=launch) as build,
            mock.patch(
                "cc_switch_ui.server.launch_fingerprint_for_active",
                return_value="launch-signature",
            ) as fingerprint,
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
        load.assert_called_once_with()
        build.assert_called_once_with("resume", cfg=cfg)
        fingerprint.assert_called_once_with(cfg=cfg)
        self.assertEqual(start.call_args.kwargs["cwd"], self.temp_dir.name)
        self.assertEqual(start.call_args.kwargs["launch_snapshot"]["session_mode"], "resume")
        self.assertEqual(start.call_args.kwargs["launch_signature"], "launch-signature")

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


class ErrorCodeTests(ServerBase):
    """2.4 稳定错误码：每个 4xx/ok:false 路径断言对应 code。"""

    def test_4xx_paths_carry_stable_codes(self):
        proc = self.app._agent_proc
        cases = []

        # validation
        cases.append(("validation", self.client.post("/api/agent/start", json={"rows": "bad"})))
        cases.append(("validation", self.client.post("/api/agent/input", json={"raw": 42})))
        # unknown_provider
        cases.append(("unknown_provider", self.client.post("/api/provider/switch", json={"provider": "nope"})))
        # unknown_account
        cases.append(("unknown_account", self.client.put("/api/account/doesnotexist", json={"provider": "deepseek", "name": "x"})))
        cases.append(("unknown_account", self.client.post("/api/account/activate", json={"provider": "deepseek", "account_id": "nope"})))
        # not_ready
        cfg = config.load_config()
        cfg["current_provider"] = "codex_custom"
        config.save_config(cfg)
        cases.append(("not_ready", self.client.post("/api/agent/start", json={})))
        # not_running（200 且 ok:false）
        cases.append(("not_running", self.client.post("/api/agent/stop")))
        cases.append(("not_running", self.client.post("/api/agent/input", json={"text": "x"})))
        # already_running
        with (
            mock.patch.object(proc, "is_running", return_value=True),
            mock.patch.object(proc, "status", return_value={"running": True, "pid": 42}),
        ):
            cases.append(("already_running", self.client.post("/api/agent/start", json={})))
        # forbidden（fs 根收敛 + cli check 非回环）
        cases.append(("forbidden", self.client.get("/api/fs/list?path=/etc")))
        manager = mock.Mock()
        check_app = create_app(cli_manager=manager)
        check_app.testing = True
        cases.append((
            "forbidden",
            check_app.test_client().post(
                "/api/cli/check",
                json={},
                environ_overrides={"REMOTE_ADDR": "10.0.0.9"},
            ),
        ))
        # origin_mismatch
        cases.append((
            "origin_mismatch",
            self.client.post(
                "/api/provider/switch",
                json={"provider": "deepseek"},
                headers={"Origin": "http://evil.example"},
            ),
        ))

        for expected, response in cases:
            with self.subTest(expected=expected, status=response.status_code):
                body = response.get_json()
                self.assertFalse(body["ok"])
                self.assertEqual(body["code"], expected)
                # 4xx 走 _err（error 字段）；200 且 ok:false 走 message 字段
                if response.status_code >= 400:
                    self.assertIn("error", body)
                else:
                    self.assertIn("message", body)

    def test_conflict_code_when_agent_running(self):
        manager = mock.Mock()
        app2 = create_app(allow_cli_management=True, cli_manager=manager)
        app2.testing = True
        with mock.patch.object(app2._agent_proc, "status", return_value={"running": True}):
            response = app2.test_client().post(
                "/api/cli/manage",
                json={
                    "agent": "codex",
                    "action": "npm_install",
                    "confirm": "npm_install:codex",
                },
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "conflict")
        manager.manage.assert_not_called()

    def test_cli_disabled_code(self):
        response = self.client.post(
            "/api/cli/manage",
            json={"agent": "codex", "action": "npm_install", "confirm": "npm_install:codex"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "cli_disabled")

    def test_confirm_mismatch_code(self):
        manager = mock.Mock()
        app2 = create_app(allow_cli_management=True, cli_manager=manager)
        app2.testing = True
        response = app2.test_client().post(
            "/api/cli/manage",
            json={"agent": "codex", "action": "npm_install", "confirm": "wrong"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "confirm_mismatch")

    def test_unauthenticated_code(self):
        app2 = create_app(auth_token="secret")
        app2.testing = True
        response = app2.test_client().get("/api/state")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["code"], "unauthenticated")
        self.assertEqual(response.get_json()["error"], "token 缺失或无效")

    def test_read_only_code(self):
        app2 = create_app(read_only=True)
        app2.testing = True
        response = app2.test_client().delete(
            "/api/account/whatever?provider=deepseek"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "read_only")


class OriginCheckTests(ServerBase):
    """0.7 Origin/CSRF 校验。"""

    def test_evil_origin_rejected_on_post(self):
        response = self.client.post(
            "/api/provider/switch",
            json={"provider": "deepseek"},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "origin_mismatch")
        # 供应商选择未被改动
        self.assertEqual(config.load_config()["current_provider"], "claude")

    def test_matching_origin_allowed(self):
        response = self.client.post(
            "/api/provider/switch",
            json={"provider": "deepseek"},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(config.load_config()["current_provider"], "deepseek")

    def test_missing_origin_allowed(self):
        response = self.client.post("/api/provider/switch", json={"provider": "deepseek"})
        self.assertEqual(response.status_code, 200)

    def test_get_not_affected(self):
        response = self.client.get(
            "/api/state", headers={"Origin": "http://evil.example"}
        )
        self.assertEqual(response.status_code, 200)

    def test_put_and_delete_checked(self):
        put = self.client.put(
            "/api/provider/deepseek",
            json={"label": "X"},
            headers={"Origin": "http://evil.example"},
        )
        delete = self.client.delete(
            "/api/account/whatever?provider=deepseek",
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(put.status_code, 403)
        self.assertEqual(delete.status_code, 403)


class HealthzTests(ServerBase):
    """0.8 /healthz。"""

    def test_healthz_reports_ok_version_and_uptime(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertIsInstance(data["version"], str)
        self.assertFalse(data["agent_running"])
        self.assertGreaterEqual(data["uptime"], 0.0)

    def test_healthz_reflects_running_agent(self):
        with mock.patch.object(self.app._agent_proc, "is_running", return_value=True):
            data = self.client.get("/healthz").get_json()
        self.assertTrue(data["agent_running"])


class CliCheckLoopbackTests(ServerBase):
    """0.12 /api/cli/check 回环门禁。"""

    def test_remote_source_rejected(self):
        manager = mock.Mock()
        app2 = create_app(cli_manager=manager)
        app2.testing = True
        response = app2.test_client().post(
            "/api/cli/check",
            json={},
            environ_overrides={"REMOTE_ADDR": "10.0.0.9"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "forbidden")
        manager.latest_versions.assert_not_called()

    def test_loopback_source_allowed(self):
        manager = mock.Mock()
        manager.latest_versions.return_value = {"claude": "1.0.0", "codex": "0.1.0"}
        app2 = create_app(cli_manager=manager)
        app2.testing = True
        response = app2.test_client().post("/api/cli/check", json={})
        self.assertEqual(response.status_code, 200)
        manager.latest_versions.assert_called_once_with("official")


class TrashRouteTests(ServerBase):
    """0.5 删除回收站：删除 → 可恢复，key 永不外泄。"""

    def _add_account(self, provider, name, key):
        return self.client.post(
            "/api/account", json={"provider": provider, "name": name, "api_key": key}
        ).get_json()

    def test_delete_restore_roundtrip_preserves_key(self):
        key = "sk-roundtrip-abcdef123456"
        added = self._add_account("deepseek", "acc1", key)
        aid = added["id"]

        before = self.client.get("/api/state").get_json()
        masked_before = before["providers"]["deepseek"]["accounts"][0]["key_masked"]

        response = self.client.delete(f"/api/account/{aid}?provider=deepseek")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

        # 配置中已移除；回收站文件内 key 逐字节一致
        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["providers"]["deepseek"]["accounts"], [])
        trash_raw = json.loads(config.trash_path().read_text(encoding="utf-8"))
        entry = trash_raw["deleted"][0]
        self.assertEqual(entry["provider"], "deepseek")
        self.assertEqual(entry["account"]["api_key"], key)

        # 回收站列表：无 key 字段
        items = self.client.get("/api/account/trash").get_json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], aid)
        self.assertEqual(items[0]["name"], "acc1")
        self.assertEqual(items[0]["provider"], "deepseek")
        self.assertEqual(items[0]["days_left"], 7)
        self.assertNotIn("api_key", items[0])
        self.assertNotIn("api_key", items[0])

        # 按 provider 过滤
        self.assertEqual(
            self.client.get("/api/account/trash?provider=claude").get_json()["items"], []
        )

        # 恢复后 key_masked 与删前一致
        restore = self.client.post(f"/api/account/{aid}/restore?provider=deepseek")
        self.assertEqual(restore.status_code, 200)
        after = self.client.get("/api/state").get_json()
        acc = after["providers"]["deepseek"]["accounts"][0]
        self.assertEqual(acc["id"], aid)
        self.assertEqual(acc["key_masked"], masked_before)

    def test_delete_unknown_account_404(self):
        response = self.client.delete("/api/account/nope?provider=deepseek")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], "unknown_account")

    def test_restore_unknown_provider_400(self):
        response = self.client.post("/api/account/nope/restore?provider=nosuch")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "unknown_provider")

    def test_restore_not_in_trash_404(self):
        response = self.client.post("/api/account/nope/restore?provider=deepseek")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], "unknown_account")


class StaticAssetTests(ServerBase):
    """0.6 随包静态资源路由。"""

    def test_static_route_serves_package_files(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "xterm.min.js").write_text("console.log(1);", encoding="utf-8")
            with mock.patch("cc_switch_ui.server._STATIC_DIR", Path(td)):
                response = self.client.get("/static/xterm.min.js")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_data(as_text=True), "console.log(1);")
                response.close()

    def test_static_route_404_for_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("cc_switch_ui.server._STATIC_DIR", Path(td)):
                response = self.client.get("/static/nope.js")
        self.assertEqual(response.status_code, 404)


class AuthTokenTests(ServerBase):
    """1.1 Bearer token 鉴权 + 反代回环修复。"""

    def setUp(self):
        super().setUp()
        self.token_app = create_app(auth_token="secret")
        self.token_app.testing = True
        self.token_client = self.token_app.test_client()

    def tearDown(self):
        self.token_app._agent_proc.keepalive = False
        self.token_app._agent_proc.stop()
        super().tearDown()

    def test_missing_token_401(self):
        response = self.token_client.get("/api/state")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["code"], "unauthenticated")

    def test_wrong_token_401(self):
        response = self.token_client.get(
            "/api/state", headers={"Authorization": "Bearer wrong"}
        )
        self.assertEqual(response.status_code, 401)

    def test_bearer_token_200(self):
        response = self.token_client.get(
            "/api/state", headers={"Authorization": "Bearer secret"}
        )
        self.assertEqual(response.status_code, 200)

    def test_query_token_200(self):
        response = self.token_client.get("/api/state?token=secret")
        self.assertEqual(response.status_code, 200)

    def test_mutating_request_requires_token(self):
        response = self.token_client.post(
            "/api/provider/switch", json={"provider": "deepseek"}
        )
        self.assertEqual(response.status_code, 401)

    def test_sse_without_token_401(self):
        response = self.token_client.get("/api/agent/stream")
        self.assertEqual(response.status_code, 401)

    def test_sse_with_query_token_200(self):
        response = self.token_client.get("/api/agent/stream?token=secret")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.content_type)

    def test_unauthenticated_is_audited(self):
        self.token_client.get("/api/state")
        audit = config.STATE_DIR / "audit.jsonl"
        lines = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(l["action"] == "unauthorized" for l in lines))

    def test_xff_non_loopback_blocks_cli_management(self):
        manager = mock.Mock()
        app2 = create_app(allow_cli_management=True, cli_manager=manager)
        app2.testing = True
        response = app2.test_client().post(
            "/api/cli/manage",
            json={
                "agent": "codex",
                "action": "npm_install",
                "confirm": "npm_install:codex",
            },
            headers={"X-Forwarded-For": "10.1.2.3"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "forbidden")
        manager.manage.assert_not_called()

    def test_xff_loopback_trusted_passes(self):
        manager = mock.Mock()
        manager.manage.return_value = {"ok": True, "output": "done"}
        app2 = create_app(
            allow_cli_management=True, cli_manager=manager, trust_proxy_loopback=True
        )
        app2.testing = True
        response = app2.test_client().post(
            "/api/cli/manage",
            json={
                "agent": "codex",
                "action": "npm_install",
                "confirm": "npm_install:codex",
            },
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        self.assertEqual(response.status_code, 200)
        manager.manage.assert_called_once()

    def test_xff_malformed_not_loopback(self):
        manager = mock.Mock()
        app2 = create_app(
            allow_cli_management=True, cli_manager=manager, trust_proxy_loopback=True
        )
        app2.testing = True
        response = app2.test_client().post(
            "/api/cli/manage",
            json={
                "agent": "codex",
                "action": "npm_install",
                "confirm": "npm_install:codex",
            },
            headers={"X-Forwarded-For": "not-an-ip"},
        )
        self.assertEqual(response.status_code, 403)
        manager.manage.assert_not_called()


class ReadOnlyTests(ServerBase):
    """1.3 只读模式：终端操作放行，其余写操作 403。"""

    def setUp(self):
        super().setUp()
        self.ro_app = create_app(read_only=True)
        self.ro_app.testing = True
        self.ro_client = self.ro_app.test_client()

    def tearDown(self):
        self.ro_app._agent_proc.keepalive = False
        self.ro_app._agent_proc.stop()
        super().tearDown()

    def test_get_allowed(self):
        self.assertEqual(self.ro_client.get("/api/state").status_code, 200)
        self.assertEqual(self.ro_client.get("/api/fs/list?path=~").status_code, 200)

    def test_account_delete_forbidden(self):
        response = self.ro_client.delete("/api/account/whatever?provider=deepseek")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "read_only")

    def test_provider_switch_forbidden(self):
        response = self.ro_client.post("/api/provider/switch", json={"provider": "deepseek"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "read_only")

    def test_config_import_forbidden(self):
        response = self.ro_client.post("/api/config/import", json={"config": {}})
        self.assertEqual(response.status_code, 403)

    def test_cli_manage_forbidden(self):
        response = self.ro_client.post("/api/cli/manage", json={})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "read_only")

    def test_agent_start_allowed(self):
        with mock.patch.object(
            self.ro_app._agent_proc, "start", return_value=(True, "已启动")
        ):
            response = self.ro_client.post("/api/agent/start", json={})
        self.assertEqual(response.status_code, 200)

    def test_agent_stop_and_input_allowed(self):
        self.assertEqual(self.ro_client.post("/api/agent/stop").status_code, 200)
        response = self.ro_client.post("/api/agent/input", json={"text": "x"})
        self.assertEqual(response.status_code, 200)


class AuditLogTests(ServerBase):
    """1.2 审计日志：追加、JSON 可解析、>10MB 轮转。"""

    @property
    def audit_path(self):
        return config.STATE_DIR / "audit.jsonl"

    def test_switch_appends_valid_audit_line(self):
        self.client.post("/api/provider/switch", json={"provider": "deepseek"})
        self.assertTrue(self.audit_path.exists())
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["action"], "provider_switch")
        self.assertEqual(rec["target"], "deepseek")
        self.assertEqual(rec["result"], "ok")
        self.assertIn("ts", rec)
        self.assertIn("remote_addr", rec)
        self.assertIn("origin", rec)

    def test_account_actions_audited(self):
        added = self.client.post(
            "/api/account", json={"provider": "deepseek", "name": "a", "api_key": "k"}
        ).get_json()
        aid = added["id"]
        self.client.post("/api/account/activate", json={"provider": "deepseek", "account_id": aid})
        self.client.delete(f"/api/account/{aid}?provider=deepseek")
        records = [
            json.loads(l) for l in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        by_action = {r["action"]: r for r in records}
        for action in ("account_add", "account_activate", "account_delete"):
            self.assertIn(action, by_action)
            self.assertEqual(by_action[action]["target"], f"deepseek:{aid}")

    def test_rotation_when_over_10mb(self):
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_path.write_text("x" * (10 * 1024 * 1024 + 1), encoding="utf-8")
        self.client.post("/api/provider/switch", json={"provider": "deepseek"})
        rotated = config.STATE_DIR / "audit.jsonl.1"
        self.assertTrue(rotated.exists())
        self.assertGreaterEqual(rotated.stat().st_size, 10 * 1024 * 1024)
        # 新文件只含新写入的一行
        new_lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(new_lines), 1)


class StructuredLoggingTests(ServerBase):
    """2.1 结构化日志：after_request 输出 JSON 行到 stderr。"""

    def test_request_log_lines_are_valid_json(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            logutil.setup_logging()
            self.client.get("/api/state")
            self.client.get("/healthz")
        lines = [l for l in buf.getvalue().splitlines() if l.strip()]
        self.assertGreaterEqual(len(lines), 2)
        parsed = [json.loads(l) for l in lines]
        requests = [p for p in parsed if p.get("msg") == "request"]
        self.assertGreaterEqual(len(requests), 2)
        paths = {r["path"] for r in requests}
        self.assertIn("/api/state", paths)
        self.assertIn("/healthz", paths)
        for r in requests:
            self.assertEqual(r["level"], "INFO")
            self.assertIn("ts", r)
            self.assertIn(r["status"], (200,))
            self.assertIsInstance(r["dur_ms"], (int, float))
            self.assertIn("method", r)
            self.assertIn("remote", r)

    def test_error_lines_carry_traceback(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            logutil.setup_logging()
            try:
                raise ValueError("boom-final")
            except ValueError:
                logging.getLogger("cc_switch_ui.test").error("crashed", exc_info=True)
        parsed = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
        errs = [p for p in parsed if p.get("level") == "ERROR"]
        self.assertEqual(len(errs), 1)
        self.assertIn("ValueError: boom-final", errs[0]["stack"])


class SwitchRestartTests(ServerBase):
    """2.3 原子「切换并重启」。"""

    def test_not_ready_leaves_running_untouched(self):
        proc = self.app._agent_proc
        with (
            mock.patch.object(proc, "status", return_value={"running": True}),
            mock.patch.object(proc, "stop") as stop,
            mock.patch.object(proc, "start") as start,
        ):
            response = self.client.post(
                "/api/agent/switch-restart", json={"provider": "codex_custom"}
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "not_ready")
        stop.assert_not_called()
        start.assert_not_called()
        # 选择未被持久化
        self.assertEqual(config.load_config()["current_provider"], "claude")

    def test_unknown_provider_rejected(self):
        proc = self.app._agent_proc
        with (
            mock.patch.object(proc, "stop") as stop,
            mock.patch.object(proc, "start") as start,
        ):
            response = self.client.post(
                "/api/agent/switch-restart", json={"provider": "nope"}
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "unknown_provider")
        stop.assert_not_called()
        start.assert_not_called()

    def test_missing_provider_field_validation(self):
        response = self.client.post("/api/agent/switch-restart", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "validation")

    def test_ready_switches_in_one_call(self):
        proc = self.app._agent_proc
        # 当前选择（codex_custom）未就绪，目标 claude 官方就绪
        cfg = config.load_config()
        cfg["current_provider"] = "codex_custom"
        config.save_config(cfg)
        with (
            mock.patch.object(proc, "status", return_value={"running": False}),
            mock.patch.object(proc, "stop") as stop,
            mock.patch.object(proc, "start", return_value=(True, "已启动")) as start,
            mock.patch("cc_switch_ui.server.time.sleep"),
        ):
            response = self.client.post(
                "/api/agent/switch-restart",
                json={"provider": "claude", "rows": 30, "cols": 100, "session_mode": "continue"},
            )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertIn("status", body)
        stop.assert_not_called()
        start.assert_called_once()
        self.assertEqual(start.call_args.kwargs["client"], "claude")
        log_path = Path(start.call_args.kwargs["log_path"])
        self.assertEqual(log_path.parent, config.STATE_DIR / "logs")
        self.assertTrue(log_path.name.startswith("default-"))
        # 选择已持久化，last_launch 已更新
        self.assertEqual(config.load_config()["current_provider"], "claude")
        last = proc.last_launch
        self.assertEqual(last["launch"]["provider_id"], "claude")
        self.assertEqual(last["session_mode"], "continue")
        self.assertEqual(last["rows"], 30)
        self.assertEqual(last["cols"], 100)

    def test_running_stops_before_restart(self):
        proc = self.app._agent_proc
        cfg = config.load_config()
        cfg["current_provider"] = "codex_custom"
        config.save_config(cfg)
        with (
            mock.patch.object(proc, "status", return_value={"running": True}),
            mock.patch.object(proc, "stop") as stop,
            mock.patch.object(proc, "start", return_value=(True, "已启动")) as start,
            mock.patch("cc_switch_ui.server.time.sleep"),
        ):
            response = self.client.post(
                "/api/agent/switch-restart", json={"provider": "claude"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(stop.call_count, 1)
        self.assertEqual(start.call_count, 1)

    def test_start_failure_reflected_in_response(self):
        proc = self.app._agent_proc
        with (
            mock.patch.object(proc, "status", return_value={"running": False}),
            mock.patch.object(proc, "stop"),
            mock.patch.object(proc, "start", return_value=(False, "未找到 claude 命令，请先安装对应 CLI")) as start,
            mock.patch("cc_switch_ui.server.time.sleep"),
        ):
            response = self.client.post(
                "/api/agent/switch-restart", json={"provider": "claude"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["ok"])
        self.assertIn("未找到", response.get_json()["message"])


class ExportImportTests(ServerBase):
    """4.5 配置/密钥导出导入 + CSV。"""

    def setUp(self):
        super().setUp()
        cfg = config.load_config()
        cfg["providers"]["deepseek"]["accounts"] = [
            {"id": "aaa", "name": "A", "api_key": "key-a"},
            {"id": "bbb", "name": "B", "api_key": "key-b"},
        ]
        cfg["providers"]["deepseek"]["active_account"] = "aaa"
        cfg["providers"]["claude"]["accounts"] = [
            {"id": "ccc", "name": "C", "api_key": "key-c"},
        ]
        cfg["providers"]["claude"]["active_account"] = "ccc"
        config.save_config(cfg)

    def _seed_accounts(self):
        pass  # 已在 setUp 直接写配置

    def test_export_masks_keys_by_default(self):
        response = self.client.get("/api/config/export")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        for raw in ("key-a", "key-b", "key-c"):
            self.assertNotIn(raw, text)
        cfg = response.get_json()["config"]
        accounts = [
            a
            for p in cfg["providers"].values()
            for a in p["accounts"]
        ]
        self.assertEqual(len(accounts), 3)
        self.assertTrue(all(set(a) <= {"id", "name", "api_key", "shared"} for a in accounts))

    def test_export_include_keys_returns_raw(self):
        response = self.client.get("/api/config/export?include_keys=1")
        text = response.get_data(as_text=True)
        for raw in ("key-a", "key-b", "key-c"):
            self.assertIn(raw, text)

    def test_replace_roundtrip_preserves_keys_and_backs_up(self):
        exported = self.client.get("/api/config/export?include_keys=1").get_json()["config"]
        response = self.client.post(
            "/api/config/import", json={"config": exported, "mode": "replace"}
        )
        self.assertEqual(response.status_code, 200)
        cfg = config.load_config()
        deepseek = {a["id"]: a for a in cfg["providers"]["deepseek"]["accounts"]}
        self.assertEqual(set(deepseek), {"aaa", "bbb"})
        self.assertEqual(deepseek["aaa"]["api_key"], "key-a")
        self.assertEqual(deepseek["bbb"]["api_key"], "key-b")
        self.assertEqual(
            cfg["providers"]["claude"]["accounts"][0]["api_key"], "key-c"
        )
        backups = list(self.root.glob(".ccm_config.pre-import-*"))
        self.assertEqual(len(backups), 1)
        # 备份文件权限 0600
        self.assertEqual(
            oct(os.stat(backups[0]).st_mode & 0o777), "0o600"
        )

    def test_merge_union_and_same_id_overwrite(self):
        incoming = {
            "providers": {
                "deepseek": {
                    "accounts": [
                        {"id": "aaa", "name": "A2", "api_key": "key-a-new"},
                        {"id": "ddd", "name": "D", "api_key": "key-d"},
                    ],
                    "active_account": "ddd",
                },
                "kimi": {
                    "label": "Kimi",
                    "client": "claude",
                    "base_url": "https://api.moonshot.cn/anthropic",
                    "model": "kimi-k2-0905-preview",
                    "auth_var": "ANTHROPIC_AUTH_TOKEN",
                    "accounts": [{"id": "eee", "name": "E", "api_key": "key-e"}],
                    "active_account": "eee",
                },
            }
        }
        response = self.client.post(
            "/api/config/import", json={"config": incoming, "mode": "merge"}
        )
        self.assertEqual(response.status_code, 200)
        cfg = config.load_config()
        deepseek = {a["id"]: a for a in cfg["providers"]["deepseek"]["accounts"]}
        self.assertEqual(set(deepseek), {"aaa", "bbb", "ddd"})
        self.assertEqual(deepseek["aaa"]["api_key"], "key-a-new")
        self.assertEqual(deepseek["bbb"]["api_key"], "key-b")
        self.assertEqual(deepseek["ddd"]["api_key"], "key-d")
        self.assertEqual(cfg["providers"]["deepseek"]["active_account"], "ddd")
        # 缺失 provider 被新增
        self.assertEqual(
            cfg["providers"]["kimi"]["accounts"][0]["api_key"], "key-e"
        )
        # 已有 provider 的端点字段不被空值覆盖
        self.assertEqual(
            cfg["providers"]["deepseek"]["base_url"],
            "https://api.deepseek.com/anthropic",
        )

    def test_invalid_import_rejected(self):
        bad = {"providers": {"deepseek": {"accounts": "oops"}}}
        response = self.client.post(
            "/api/config/import", json={"config": bad, "mode": "merge"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "validation")

        bad_mode = self.client.post(
            "/api/config/import", json={"config": bad, "mode": "bogus"}
        )
        self.assertEqual(bad_mode.status_code, 400)
        self.assertEqual(bad_mode.get_json()["code"], "validation")

    def test_import_missing_or_wrapped_providers_rejected(self):
        # 缺 providers 键 → 400（不得 500）
        missing = self.client.post(
            "/api/config/import", json={"config": {"foo": 1}, "mode": "replace"}
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()["code"], "validation")
        # 误传导出包装 {ok, config} → 同样 400
        wrapper = self.client.post(
            "/api/config/import",
            json={
                "config": {"ok": True, "config": {"providers": {}}},
                "mode": "replace",
            },
        )
        self.assertEqual(wrapper.status_code, 400)
        self.assertEqual(wrapper.get_json()["code"], "validation")

    def test_csv_export(self):
        response = self.client.get("/api/account/export.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.content_type)
        lines = response.get_data(as_text=True).strip().splitlines()
        self.assertEqual(lines[0], "provider,name,api_key,active")
        self.assertEqual(len(lines), 4)  # 表头 + 3 个账号
        text = response.get_data(as_text=True)
        for raw in ("key-a", "key-b", "key-c"):
            self.assertNotIn(raw, text)
        raw_response = self.client.get("/api/account/export.csv?include_keys=1")
        self.assertIn("key-a", raw_response.get_data(as_text=True))


class ProviderTestRouteTests(ServerBase):
    """4.7 供应商连接测试（monkeypatch urlopen）。"""

    def _seed(self, provider, key, **provider_fields):
        if provider_fields:
            cfg = config.load_config()
            cfg["providers"][provider].update(provider_fields)
            config.save_config(cfg)
        if key is not None:
            self.client.post(
                "/api/account", json={"provider": provider, "name": "t", "api_key": key}
            )

    def _ok_response(self, status=200):
        resp = mock.MagicMock()
        resp.__enter__.return_value.status = status
        return resp

    def test_claude_official_without_key(self):
        response = self.client.post("/api/provider/claude/test")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["result"], "claude_login")
        self.assertIn("note", body)

    def test_other_provider_without_key_not_ready(self):
        response = self.client.post("/api/provider/kimi/test")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "not_ready")

    def test_claude_client_uses_bearer_for_auth_token(self):
        self._seed("deepseek", "sk-ds-123")
        with mock.patch("urllib.request.urlopen", return_value=self._ok_response(200)) as u:
            response = self.client.post("/api/provider/deepseek/test")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "result": "ok", "status": 200})
        req = u.call_args.args[0]
        self.assertEqual(str(req.full_url), "https://api.deepseek.com/anthropic/v1/messages")
        self.assertEqual(req.get_header("Authorization"), "Bearer sk-ds-123")
        self.assertEqual(req.headers.get("Content-type"), "application/json")
        body = json.loads(req.data)
        self.assertEqual(body["model"], "deepseek-chat")
        self.assertEqual(body["max_tokens"], 1)
        self.assertEqual(body["messages"], [{"role": "user", "content": "ping"}])
        self.assertEqual(u.call_args.kwargs["timeout"], 10)

    def test_claude_client_uses_x_api_key(self):
        self._seed("claude", "sk-cl-456")
        with mock.patch("urllib.request.urlopen", return_value=self._ok_response(200)) as u:
            self.client.post("/api/provider/claude/test")
        req = u.call_args.args[0]
        self.assertEqual(str(req.full_url), "https://api.anthropic.com/v1/messages")
        self.assertEqual(req.get_header("X-api-key"), "sk-cl-456")
        self.assertIsNone(req.get_header("Authorization"))

    def test_codex_uses_responses_endpoint(self):
        self._seed(
            "codex_custom", "sk-cx-789",
            base_url="https://api.openai.example", model="gpt-5",
        )
        with mock.patch("urllib.request.urlopen", return_value=self._ok_response(200)) as u:
            response = self.client.post("/api/provider/codex_custom/test")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        req = u.call_args.args[0]
        self.assertEqual(str(req.full_url), "https://api.openai.example/responses")
        self.assertEqual(req.get_header("Authorization"), "Bearer sk-cx-789")
        body = json.loads(req.data)
        self.assertEqual(
            body, {"model": "gpt-5", "input": "ping", "max_output_tokens": 1}
        )

    def test_claude_non_official_empty_model_not_ready(self):
        self._seed("custom", "sk-cust-1", base_url="https://proxy.example")
        response = self.client.post("/api/provider/custom/test")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "not_ready")
        self.assertIn("模型", response.get_json()["error"])

    def test_unknown_provider(self):
        response = self.client.post("/api/provider/nope/test")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "unknown_provider")

    def test_http_401_classified_auth_failed(self):
        self._seed("deepseek", "sk-bad")
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None),
        ):
            response = self.client.post("/api/provider/deepseek/test")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["result"], "auth_failed")
        self.assertEqual(body["status"], 401)

    def test_http_500_classified_other(self):
        self._seed("deepseek", "sk-x")
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("http://x", 500, "ISE", {}, None),
        ):
            response = self.client.post("/api/provider/deepseek/test")
        body = response.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["result"], "other")
        self.assertEqual(body["status"], 500)

    def test_unreachable_classified_network(self):
        self._seed("deepseek", "sk-x")
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("unreachable"),
        ):
            response = self.client.post("/api/provider/deepseek/test")
        body = response.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["result"], "network")
        self.assertIn("error", body)

    def test_timeout_classified_network(self):
        self._seed("deepseek", "sk-x")
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            response = self.client.post("/api/provider/deepseek/test")
        body = response.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["result"], "network")


class FsRootTests(ServerBase):
    """2.5 /api/fs/list 根收敛。"""

    def test_etc_forbidden_by_default(self):
        response = self.client.get("/api/fs/list?path=/etc")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "forbidden")

    def test_home_allowed_by_default(self):
        response = self.client.get("/api/fs/list?path=")
        self.assertEqual(response.status_code, 200)

    def test_fs_root_scopes_listing(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "sub").mkdir()
            app2 = create_app(fs_root=td)
            app2.testing = True
            client2 = app2.test_client()
            ok = client2.get(f"/api/fs/list?path={td}")
            self.assertEqual(ok.status_code, 200)
            self.assertIn("sub", ok.get_json()["dirs"])
            in_root = client2.get(f"/api/fs/list?path={Path(td) / 'sub'}")
            self.assertEqual(in_root.status_code, 200)
            bad = client2.get("/api/fs/list?path=/etc")
            self.assertEqual(bad.status_code, 403)
            above = client2.get(f"/api/fs/list?path={Path(td).parent}")
            self.assertEqual(above.status_code, 403)


class ProfileRouteTests(ServerBase):
    """4.2 档案（profile）CRUD 与校验。"""

    def setUp(self):
        super().setUp()
        cfg = config.load_config()
        cfg["providers"]["deepseek"]["accounts"] = [
            {"id": "ds-1", "name": "ds", "api_key": "deepseek-key-1"},
            {"id": "ds-2", "name": "ds2", "api_key": "deepseek-key-2"},
        ]
        cfg["providers"]["deepseek"]["active_account"] = "ds-1"
        config.save_config(cfg)

    def test_profile_crud_roundtrip(self):
        created = self.client.post("/api/profiles", json={"name": "work"})
        self.assertEqual(created.status_code, 200)
        profile = created.get_json()["profile"]
        self.assertTrue(profile["id"])
        self.assertEqual(profile["name"], "work")
        self.assertEqual(profile["provider_id"], "claude")  # 缺省 current_provider
        self.assertIsNone(profile["account_id"])
        self.assertEqual(profile["session_mode"], "new")

        listed = self.client.get("/api/profiles").get_json()["profiles"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], profile["id"])

        updated = self.client.put(
            f"/api/profiles/{profile['id']}",
            json={"name": "renamed", "cwd": "/tmp/x", "session_mode": "continue"},
        )
        self.assertEqual(updated.status_code, 200)
        up = updated.get_json()["profile"]
        self.assertEqual(up["name"], "renamed")
        self.assertEqual(up["cwd"], "/tmp/x")
        self.assertEqual(up["session_mode"], "continue")
        self.assertEqual(up["provider_id"], "claude")  # 未提供 → 不变

        self.assertEqual(self.client.delete(f"/api/profiles/{profile['id']}").status_code, 200)
        self.assertEqual(self.client.get("/api/profiles").get_json()["profiles"], [])

    def test_create_profile_validates_provider_account_and_shape(self):
        bad_provider = self.client.post(
            "/api/profiles", json={"name": "x", "provider_id": "nope"}
        )
        self.assertEqual(bad_provider.status_code, 400)
        self.assertEqual(bad_provider.get_json()["code"], "unknown_provider")

        bad_account = self.client.post(
            "/api/profiles",
            json={"name": "x", "provider_id": "deepseek", "account_id": "nope"},
        )
        self.assertEqual(bad_account.status_code, 404)
        self.assertEqual(bad_account.get_json()["code"], "unknown_account")

        for body in (
            {"name": "   "},
            {"name": 42},
            {"name": "x", "session_mode": "bogus"},
            {"name": "x", "cwd": 123},
        ):
            response = self.client.post("/api/profiles", json=body)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["code"], "validation")

    def test_update_and_delete_unknown_profile_404(self):
        put = self.client.put("/api/profiles/nope", json={"name": "x"})
        self.assertEqual(put.status_code, 404)
        self.assertEqual(put.get_json()["code"], "unknown_profile")

        delete = self.client.delete("/api/profiles/nope")
        self.assertEqual(delete.status_code, 404)
        self.assertEqual(delete.get_json()["code"], "unknown_profile")

    def test_update_profile_account_validated_against_target_provider(self):
        profile = self.client.post(
            "/api/profiles",
            json={"name": "p", "provider_id": "deepseek", "account_id": "ds-1"},
        ).get_json()["profile"]

        ok = self.client.put(
            f"/api/profiles/{profile['id']}", json={"account_id": "ds-2"}
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()["profile"]["account_id"], "ds-2")

        # 目标 provider 换成 claude，ds-2 不属于它 → 404 且档案不变
        bad = self.client.put(
            f"/api/profiles/{profile['id']}",
            json={"provider_id": "claude", "account_id": "ds-2"},
        )
        self.assertEqual(bad.status_code, 404)
        self.assertEqual(bad.get_json()["code"], "unknown_account")
        unchanged = self.client.get("/api/profiles").get_json()["profiles"][0]
        self.assertEqual(unchanged["provider_id"], "deepseek")
        self.assertEqual(unchanged["account_id"], "ds-2")

    def test_profile_audit_trail(self):
        self.client.post("/api/profiles", json={"name": "audited"})
        profile_id = self.client.get("/api/profiles").get_json()["profiles"][0]["id"]
        self.client.put(f"/api/profiles/{profile_id}", json={"name": "x"})
        self.client.delete(f"/api/profiles/{profile_id}")

        lines = (config.STATE_DIR / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
        actions = [json.loads(line)["action"] for line in lines]
        self.assertIn("profile_create", actions)
        self.assertIn("profile_edit", actions)
        self.assertIn("profile_delete", actions)


class KeepaliveResetTests(ServerBase):
    """0.11 server 侧：重新开启 keepalive 调 reset_fast_fail。"""

    def test_enable_resets_fast_fail(self):
        proc = self.app._agent_proc
        with mock.patch.object(proc, "reset_fast_fail") as reset:
            response = self.client.post("/api/agent/keepalive", json={"enabled": True})
        self.assertEqual(response.status_code, 200)
        reset.assert_called_once()

    def test_disable_does_not_reset(self):
        proc = self.app._agent_proc
        with mock.patch.object(proc, "reset_fast_fail") as reset:
            response = self.client.post("/api/agent/keepalive", json={"enabled": False})
        self.assertEqual(response.status_code, 200)
        reset.assert_not_called()


class AppEntryTests(unittest.TestCase):
    """0.0 / 1.1 入口参数：--config-dir、--auth-token(-file)、--read-only、--fs-root。"""

    def _run_main(self, argv):
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch("cc_switch_ui.app.setup_logging"),
            mock.patch("cc_switch_ui.app.create_app") as create,
        ):
            create.return_value = mock.Mock()
            app_module.main()
        return create.call_args.kwargs

    def test_config_dir_applies_before_create_app(self):
        tmp = tempfile.mkdtemp(prefix="ccs-cfg-")
        try:
            with mock.patch("cc_switch_ui.config.configure_paths") as configure:
                kwargs = self._run_main(["cc-switch-ui", "--config-dir", tmp])
            configure.assert_called_once_with(Path(tmp))
            self.assertIsNone(kwargs["auth_token"])
            self.assertFalse(kwargs["read_only"])
            self.assertFalse(kwargs["trust_proxy_loopback"])
            self.assertIsNone(kwargs["fs_root"])
            self.assertFalse(kwargs["allow_cli_management"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_config_dir_leaves_defaults(self):
        with mock.patch("cc_switch_ui.config.configure_paths") as configure:
            self._run_main(["cc-switch-ui"])
        configure.assert_not_called()

    def test_auth_token_file_read_and_stripped(self):
        fd, path = tempfile.mkstemp()
        try:
            os.write(fd, b"  s3cr3t-token  \n")
            os.close(fd)
            kwargs = self._run_main(["cc-switch-ui", "--auth-token-file", path])
            self.assertEqual(kwargs["auth_token"], "s3cr3t-token")
        finally:
            os.unlink(path)

    def test_auth_token_and_file_mutually_exclusive(self):
        with (
            mock.patch.object(
                sys, "argv",
                ["cc-switch-ui", "--auth-token", "a", "--auth-token-file", "b"],
            ),
            mock.patch("cc_switch_ui.app.setup_logging"),
            self.assertRaises(SystemExit) as raised,
        ):
            app_module.main()
        self.assertEqual(raised.exception.code, 2)

    def test_read_only_fs_root_trust_pass_through(self):
        kwargs = self._run_main([
            "cc-switch-ui",
            "--read-only",
            "--fs-root", "/tmp",
            "--trust-proxy-loopback",
            "--auth-token", "tok",
        ])
        self.assertTrue(kwargs["read_only"])
        self.assertEqual(kwargs["fs_root"], "/tmp")
        self.assertTrue(kwargs["trust_proxy_loopback"])
        self.assertEqual(kwargs["auth_token"], "tok")

    def test_max_sessions_pass_through(self):
        kwargs = self._run_main(["cc-switch-ui", "--max-sessions", "3"])
        self.assertEqual(kwargs["max_sessions"], 3)

    def test_max_sessions_defaults_to_8(self):
        kwargs = self._run_main(["cc-switch-ui"])
        self.assertEqual(kwargs["max_sessions"], 8)


class SessionServerIntegrationTests(ServerBase):
    """3.3 会话路由 × 安全中间件：只读白名单 / token 鉴权 / healthz。"""

    def test_read_only_allows_session_terminal_actions(self):
        # 预置一个会话条目，让终端路由可识别它（只读模式下无法经 API 创建）
        cfg = config.load_config()
        cfg.setdefault("sessions", []).append({
            "id": "seeded",
            "name": "seeded",
            "cwd": None,
            "provider_id": "deepseek",
            "session_mode": "new",
            "account_id": None,
        })
        config.save_config(cfg)
        app = create_app(read_only=True)
        app.testing = True
        client = app.test_client()
        try:
            created = client.post(
                "/api/sessions", json={"provider_id": "deepseek"}
            )
            self.assertEqual(created.status_code, 403)
            self.assertEqual(created.get_json()["code"], "read_only")

            # 终端操作白名单放行（进程未运行 → not_running，而非 read_only 403）
            stop = client.post("/api/sessions/seeded/stop")
            self.assertEqual(stop.status_code, 200)
            self.assertEqual(stop.get_json()["code"], "not_running")
            size = client.post(
                "/api/sessions/seeded/resize", json={"rows": 24, "cols": 80}
            )
            self.assertEqual(size.status_code, 200)
            self.assertTrue(size.get_json()["ok"])
        finally:
            for proc in app._sessions.list():
                proc.keepalive = False
                proc.stop()

    def test_token_required_for_session_routes(self):
        app = create_app(auth_token="secret")
        app.testing = True
        client = app.test_client()
        try:
            anon = client.get("/api/sessions")
            self.assertEqual(anon.status_code, 401)
            self.assertEqual(anon.get_json()["code"], "unauthenticated")

            ok = client.get(
                "/api/sessions",
                headers={"Authorization": "Bearer secret"},
            )
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.get_json(), {"ok": True, "sessions": []})
        finally:
            for proc in app._sessions.list():
                proc.keepalive = False
                proc.stop()

    def test_healthz_session_count(self):
        data = self.client.get("/healthz").get_json()
        self.assertEqual(data["session_count"], 1)  # default 会话


if __name__ == "__main__":
    unittest.main()
