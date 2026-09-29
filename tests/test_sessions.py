import os
import queue
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
import cc_switch_ui.config as config
from cc_switch_ui.server import create_app


def _write_shim(bin_dir, name, marker):
    """fake CLI：启动时回显唯一 marker，然后 cat 保持存活并回显输入。"""
    script = bin_dir / name
    script.write_text(
        f"#!/bin/sh\necho {marker}\ncat\n", encoding="utf-8"
    )
    script.chmod(0o755)


def _drain_until(q, marker, timeout=10.0):
    """从 SSE 队列读取直到出现 marker（或超时），返回累积文本。"""
    collected = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = q.get(timeout=0.5)
        except queue.Empty:
            continue
        collected.append(chunk)
        if marker in chunk:
            break
    return "".join(collected)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class SessionApiTests(unittest.TestCase):
    """3.3 会话 API：并行隔离、生命周期、上限、legacy 兼容。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_config_path = config.CONFIG_PATH
        self.original_state_dir = config.STATE_DIR
        config.CONFIG_PATH = self.root / ".ccm_config"
        config.STATE_DIR = self.root / "state"

        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        _write_shim(self.bin_dir, "claude", "MARKER-CLAUDE")
        _write_shim(self.bin_dir, "codex", "MARKER-CODEX")
        self.cwd_a = self.root / "worka"
        self.cwd_a.mkdir()
        self.cwd_b = self.root / "workb"
        self.cwd_b.mkdir()

        cfg = config.load_config()
        cfg["providers"]["deepseek"]["accounts"] = [
            {"id": "ds-1", "name": "ds", "api_key": "deepseek-key"}
        ]
        cfg["providers"]["deepseek"]["active_account"] = "ds-1"
        cfg["providers"]["codex_custom"]["base_url"] = "https://codex.example.com/v1"
        cfg["providers"]["codex_custom"]["model"] = "gpt-test"
        cfg["providers"]["codex_custom"]["accounts"] = [
            {"id": "cx-1", "name": "cx", "api_key": "codex-key"}
        ]
        cfg["providers"]["codex_custom"]["active_account"] = "cx-1"
        config.save_config(cfg)

        self.app = create_app(max_sessions=8)
        self.app.testing = True
        self.client = self.app.test_client()
        self._path_patcher = mock.patch.dict(
            os.environ,
            {"PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}"},
        )
        self._path_patcher.start()

    def tearDown(self):
        self._path_patcher.stop()
        for proc in self.app._sessions.list():
            proc.keepalive = False
            proc.stop()
            if proc.reader_thread is not None:
                proc.reader_thread.join(timeout=3)
        config.CONFIG_PATH = self.original_config_path
        config.STATE_DIR = self.original_state_dir
        self.temp_dir.cleanup()

    # ---- 创建与列表 ----

    def test_create_session_persists_entry_and_lists_it(self):
        response = self.client.post(
            "/api/sessions",
            json={"name": "work", "cwd": str(self.cwd_a), "provider_id": "deepseek"},
        )
        self.assertEqual(response.status_code, 200)
        session = response.get_json()["session"]
        self.assertTrue(session["id"])
        self.assertEqual(session["name"], "work")
        self.assertEqual(session["provider_id"], "deepseek")
        self.assertEqual(session["provider_label"], "DeepSeek")
        self.assertFalse(session["running"])

        cfg = config.load_config()
        self.assertEqual(cfg["sessions"][0]["id"], session["id"])
        self.assertEqual(cfg["sessions"][0]["cwd"], str(self.cwd_a))

        listed = self.client.get("/api/sessions").get_json()["sessions"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], session["id"])

    def test_create_session_defaults_to_current_provider(self):
        cfg = config.load_config()
        cfg["current_provider"] = "deepseek"
        config.save_config(cfg)

        session = self.client.post(
            "/api/sessions", json={"name": "x"}
        ).get_json()["session"]
        self.assertEqual(session["provider_id"], "deepseek")

    def test_create_session_rejects_unknown_provider_and_account(self):
        response = self.client.post(
            "/api/sessions", json={"provider_id": "nope"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "unknown_provider")

        # 账号不属于该供应商
        response = self.client.post(
            "/api/sessions",
            json={"provider_id": "deepseek", "account_id": "cx-1"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], "unknown_account")

    def test_session_limit_returns_409_max_sessions(self):
        app = create_app(max_sessions=2)
        app.testing = True
        client = app.test_client()
        try:
            first = client.post("/api/sessions", json={"provider_id": "deepseek"})
            self.assertEqual(first.status_code, 200)
            second = client.post("/api/sessions", json={"provider_id": "deepseek"})
            self.assertEqual(second.status_code, 409)
            self.assertEqual(second.get_json()["code"], "max_sessions")
            self.assertEqual(len(app._sessions), 2)
        finally:
            for proc in app._sessions.list():
                proc.keepalive = False
                proc.stop()

    # ---- 并行启动与流隔离 ----

    def test_two_sessions_stream_only_their_own_marker(self):
        sid_a = self.client.post(
            "/api/sessions",
            json={"cwd": str(self.cwd_a), "provider_id": "deepseek"},
        ).get_json()["session"]["id"]
        sid_b = self.client.post(
            "/api/sessions",
            json={"cwd": str(self.cwd_b), "provider_id": "codex_custom"},
        ).get_json()["session"]["id"]

        registry = self.app._sessions
        # 先订阅（不重放历史），再启动，保证 marker 一定被捕获
        q_a = registry.get(sid_a).subscribe()
        q_b = registry.get(sid_b).subscribe()

        resp_a = self.client.post(f"/api/sessions/{sid_a}/start", json={})
        resp_b = self.client.post(f"/api/sessions/{sid_b}/start", json={})
        self.assertTrue(resp_a.get_json()["ok"])
        self.assertTrue(resp_b.get_json()["ok"])

        try:
            stream_a = _drain_until(q_a, "MARKER-CLAUDE")
            stream_b = _drain_until(q_b, "MARKER-CODEX")
            self.assertIn("MARKER-CLAUDE", stream_a)
            self.assertNotIn("MARKER-CODEX", stream_a)
            self.assertIn("MARKER-CODEX", stream_b)
            self.assertNotIn("MARKER-CLAUDE", stream_b)
        finally:
            registry.get(sid_a).unsubscribe(q_a)
            registry.get(sid_b).unsubscribe(q_b)

        # 输入也互不串流：向 A 发输入，只有 A 回显
        echo_q = registry.get(sid_a).subscribe()
        self.client.post(
            f"/api/sessions/{sid_a}/input", json={"text": "ping-a"}
        )
        self.assertIn("ping-a", _drain_until(echo_q, "ping-a"))
        registry.get(sid_a).unsubscribe(echo_q)

    def test_session_stream_route_streams_own_proc(self):
        sid = self.client.post(
            "/api/sessions",
            json={"cwd": str(self.cwd_a), "provider_id": "deepseek"},
        ).get_json()["session"]["id"]
        response = self.client.get(f"/api/sessions/{sid}/stream")
        self.assertEqual(response.status_code, 200)
        gen = response.response
        try:
            first = next(gen)
            self.assertIn(b"retry", first)
            self.client.post(f"/api/sessions/{sid}/start", json={})
            collected = []
            for _ in range(50):
                chunk = next(gen)
                collected.append(chunk)
                if b"MARKER-CLAUDE" in chunk:
                    break
            self.assertIn(b"MARKER-CLAUDE", b"".join(collected))
        finally:
            gen.close()

    def test_start_unknown_session_404s(self):
        response = self.client.post("/api/sessions/nope/start", json={})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], "unknown_session")

    def test_start_not_ready_provider_400(self):
        # custom：无 base_url，未就绪
        sid = self.client.post(
            "/api/sessions",
            json={"provider_id": "custom", "cwd": str(self.cwd_a)},
        ).get_json()["session"]["id"]
        response = self.client.post(f"/api/sessions/{sid}/start", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "not_ready")

    def test_start_body_overrides_are_one_shot(self):
        # 会话默认 provider 未就绪（无 key 的 custom），body 无法换 provider；
        # 这里验证 cwd/session_mode 覆盖生效且不持久化：
        # 用 deepseek（就绪）会话，start 时临时覆盖 session_mode=continue
        sid = self.client.post(
            "/api/sessions",
            json={"cwd": str(self.cwd_a), "provider_id": "deepseek"},
        ).get_json()["session"]["id"]
        response = self.client.post(
            f"/api/sessions/{sid}/start",
            json={"cwd": str(self.cwd_b), "session_mode": "continue"},
        )
        self.assertTrue(response.get_json()["ok"])
        # 覆盖生效：launch 快照里 cwd 与 mode 是临时值
        status = response.get_json()["status"]
        self.assertEqual(status["launch"]["cwd"], str(self.cwd_b.resolve()))
        self.assertEqual(status["launch"]["session_mode"], "continue")
        # 不持久化：config 中会话仍是原 cwd / new
        entry = next(
            s for s in config.load_config()["sessions"] if s["id"] == sid
        )
        self.assertEqual(entry["cwd"], str(self.cwd_a))
        self.assertEqual(entry["session_mode"], "new")
        self.client.post(f"/api/sessions/{sid}/stop")

    # ---- PATCH：配置变更 → restart_required ----

    def test_patch_running_session_flips_restart_required(self):
        sid = self.client.post(
            "/api/sessions",
            json={"cwd": str(self.cwd_a), "provider_id": "deepseek"},
        ).get_json()["session"]["id"]
        self.assertTrue(self.client.post(f"/api/sessions/{sid}/start", json={}).get_json()["ok"])

        listed = self.client.get("/api/sessions").get_json()["sessions"]
        self.assertFalse(listed[0]["restart_required"])

        response = self.client.patch(
            f"/api/sessions/{sid}", json={"provider_id": "claude"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["session"]["restart_required"])

        # config 已更新
        entry = next(
            s for s in config.load_config()["sessions"] if s["id"] == sid
        )
        self.assertEqual(entry["provider_id"], "claude")

        listed = self.client.get("/api/sessions").get_json()["sessions"]
        self.assertTrue(listed[0]["restart_required"])
        self.client.post(f"/api/sessions/{sid}/stop")

    def test_patch_unknown_session_404(self):
        response = self.client.patch("/api/sessions/nope", json={"name": "x"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], "unknown_session")

    # ---- DELETE：杀掉运行中会话，其余存活 ----

    def test_delete_running_session_kills_child_others_survive(self):
        sid_a = self.client.post(
            "/api/sessions",
            json={"cwd": str(self.cwd_a), "provider_id": "deepseek"},
        ).get_json()["session"]["id"]
        sid_b = self.client.post(
            "/api/sessions",
            json={"cwd": str(self.cwd_b), "provider_id": "codex_custom"},
        ).get_json()["session"]["id"]
        status_a = self.client.post(f"/api/sessions/{sid_a}/start", json={}).get_json()["status"]
        status_b = self.client.post(f"/api/sessions/{sid_b}/start", json={}).get_json()["status"]
        pid_a, pid_b = status_a["pid"], status_b["pid"]
        self.assertTrue(_pid_alive(pid_a))
        self.assertTrue(_pid_alive(pid_b))

        response = self.client.delete(f"/api/sessions/{sid_a}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["ok"], True)

        deadline = time.time() + 10
        while time.time() < deadline and _pid_alive(pid_a):
            time.sleep(0.1)
        self.assertFalse(_pid_alive(pid_a))
        self.assertTrue(_pid_alive(pid_b))

        # config 与注册表都已移除
        self.assertFalse(
            any(s.get("id") == sid_a for s in config.load_config()["sessions"])
        )
        self.assertIsNone(self.app._sessions.get(sid_a))
        remaining = self.client.get("/api/sessions").get_json()["sessions"]
        self.assertEqual([s["id"] for s in remaining], [sid_b])

        # 其余会话仍可停止（生命周期完好）
        self.assertTrue(self.client.post(f"/api/sessions/{sid_b}/stop").get_json()["ok"])

    def test_delete_unknown_session_404(self):
        response = self.client.delete("/api/sessions/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], "unknown_session")

    # ---- switch-restart（会话版） ----

    def test_session_switch_restart_updates_session_provider_not_global(self):
        cfg = config.load_config()
        cfg["current_provider"] = "deepseek"
        config.save_config(cfg)
        sid = self.client.post(
            "/api/sessions",
            json={"cwd": str(self.cwd_a), "provider_id": "deepseek"},
        ).get_json()["session"]["id"]
        response = self.client.post(
            f"/api/sessions/{sid}/switch-restart",
            json={"provider": "claude"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        # 会话 provider 已切换，全局选择未动
        entry = next(
            s for s in config.load_config()["sessions"] if s["id"] == sid
        )
        self.assertEqual(entry["provider_id"], "claude")
        self.assertEqual(config.load_config()["current_provider"], "deepseek")
        self.client.post(f"/api/sessions/{sid}/stop")

    def test_session_switch_restart_not_ready_leaves_running(self):
        sid = self.client.post(
            "/api/sessions",
            json={"cwd": str(self.cwd_a), "provider_id": "deepseek"},
        ).get_json()["session"]["id"]
        self.assertTrue(self.client.post(f"/api/sessions/{sid}/start", json={}).get_json()["ok"])

        response = self.client.post(
            f"/api/sessions/{sid}/switch-restart",
            json={"provider": "custom"},  # 无 base_url / key → not_ready
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "not_ready")
        # 进程未受影响
        status = self.client.get(f"/api/sessions/{sid}/status").get_json()
        self.assertTrue(status["running"])
        # 会话 provider 未被改动
        entry = next(
            s for s in config.load_config()["sessions"] if s["id"] == sid
        )
        self.assertEqual(entry["provider_id"], "deepseek")
        self.client.post(f"/api/sessions/{sid}/stop")

    # ---- legacy 兼容：default 会话补写 ----

    def test_legacy_start_backfills_default_session_entry(self):
        self.assertEqual(config.load_config()["sessions"], [])
        response = self.client.post(
            "/api/agent/start", json={"rows": 24, "cols": 80}
        )
        self.assertTrue(response.get_json()["ok"])

        entries = config.load_config()["sessions"]
        default = next(s for s in entries if s["id"] == "default")
        self.assertEqual(default["provider_id"], "claude")

        # /api/sessions 也列出 default
        listed = self.client.get("/api/sessions").get_json()["sessions"]
        self.assertEqual([s["id"] for s in listed], ["default"])
        self.assertTrue(listed[0]["running"])

        # healthz 反映会话数
        self.assertEqual(self.client.get("/healthz").get_json()["session_count"], 1)

    def test_session_survives_panel_restart(self):
        """面板重启（新注册表）后，config 中的会话仍可启动（proc 自动重建注册）。"""
        sid = self.client.post(
            "/api/sessions",
            json={"cwd": str(self.cwd_a), "provider_id": "deepseek"},
        ).get_json()["session"]["id"]
        # 模拟面板重启：新 app 共享同一配置/状态目录
        app2 = create_app(max_sessions=8)
        app2.testing = True
        client2 = app2.test_client()
        try:
            listed = client2.get("/api/sessions").get_json()["sessions"]
            self.assertEqual([s["id"] for s in listed], [sid])
            self.assertFalse(listed[0]["running"])

            response = client2.post(f"/api/sessions/{sid}/start", json={})
            self.assertTrue(response.get_json()["ok"])
            self.assertTrue(response.get_json()["status"]["running"])
            client2.post(f"/api/sessions/{sid}/stop")
        finally:
            for proc in app2._sessions.list():
                proc.keepalive = False
                proc.stop()

    def test_session_watchdog_rebuilds_from_session_provider(self):
        """看门狗按会话自身 provider 重建（而非全局 current_provider）。"""
        cfg = config.load_config()
        cfg["current_provider"] = "claude"  # 全局选择与会话不同
        config.save_config(cfg)

        sid = self.client.post(
            "/api/sessions",
            json={"cwd": str(self.cwd_a), "provider_id": "deepseek"},
        ).get_json()["session"]["id"]
        self.client.post(
            f"/api/sessions/{sid}/keepalive", json={"enabled": True}
        )
        self.assertTrue(
            self.client.post(f"/api/sessions/{sid}/start", json={}).get_json()["ok"]
        )
        proc = self.app._sessions.get(sid)
        self.assertTrue(proc.is_running())
        old_pid = proc.proc.pid

        # 杀掉子进程，触发看门狗（rebuild 钩子已按会话 provider 挂载）
        os.kill(old_pid, signal.SIGKILL)
        deadline = time.time() + 20
        while time.time() < deadline:
            if proc.is_running() and proc.proc.pid != old_pid:
                break
            time.sleep(0.1)
        self.assertTrue(proc.is_running())
        self.assertNotEqual(proc.proc.pid, old_pid)
        # 重启后的 launch 仍来自会话绑定的 deepseek，而非全局 claude
        self.assertEqual(proc.launch_snapshot["provider_id"], "deepseek")
        self.client.post(f"/api/sessions/{sid}/stop")


    # ---- 4.2 档案（profile）联动 ----

    def test_create_session_from_profile_materializes_fields(self):
        profile = self.client.post(
            "/api/profiles",
            json={
                "name": "p1",
                "cwd": str(self.cwd_b),
                "provider_id": "deepseek",
                "account_id": "ds-1",
                "session_mode": "continue",
            },
        ).get_json()["profile"]

        session = self.client.post(
            "/api/sessions", json={"profile_id": profile["id"]}
        ).get_json()["session"]
        self.assertEqual(session["cwd"], str(self.cwd_b))
        self.assertEqual(session["provider_id"], "deepseek")
        self.assertEqual(session["session_mode"], "continue")
        self.assertEqual(session["account_id"], "ds-1")

        # 会话条目已持久化物化字段
        entry = next(
            s for s in config.load_config()["sessions"] if s["id"] == session["id"]
        )
        self.assertEqual(entry["cwd"], str(self.cwd_b))
        self.assertEqual(entry["provider_id"], "deepseek")
        self.assertEqual(entry["session_mode"], "continue")
        self.assertEqual(entry["account_id"], "ds-1")

    def test_create_session_explicit_body_wins_over_profile(self):
        profile = self.client.post(
            "/api/profiles",
            json={
                "name": "p1",
                "cwd": str(self.cwd_b),
                "provider_id": "deepseek",
                "account_id": "ds-1",
                "session_mode": "resume",
            },
        ).get_json()["profile"]

        session = self.client.post(
            "/api/sessions",
            json={
                "profile_id": profile["id"],
                "cwd": str(self.cwd_a),
                "provider_id": "deepseek",
                "session_mode": "new",
            },
        ).get_json()["session"]
        # 显式 body 字段优先
        self.assertEqual(session["cwd"], str(self.cwd_a))
        self.assertEqual(session["session_mode"], "new")
        # body 未给 account_id → 取档案账号
        self.assertEqual(session["account_id"], "ds-1")

    def test_create_session_profile_alias_and_unknown_404(self):
        profile = self.client.post(
            "/api/profiles",
            json={"name": "p1", "provider_id": "deepseek"},
        ).get_json()["profile"]
        # profile 别名同样可用
        session = self.client.post(
            "/api/sessions", json={"profile": profile["id"]}
        ).get_json()["session"]
        self.assertEqual(session["provider_id"], "deepseek")

        response = self.client.post("/api/sessions", json={"profile_id": "nope"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], "unknown_profile")

    def test_start_with_profile_account_override_uses_that_key(self):
        """start 带 profile：该档案账号的 key 进入 launch env（一次性覆盖）。"""
        env_dump = self.root / "env.dump"
        (self.bin_dir / "claude").write_text(
            "#!/bin/sh\n"
            "echo MARKER-CLAUDE\n"
            "echo \"$ANTHROPIC_AUTH_TOKEN\" > \"$ENV_DUMP\"\n"
            "cat\n",
            encoding="utf-8",
        )
        # 追加第二个账号（会话未指定账号 → 回落激活的 ds-1；档案钉住 ds-2）
        cfg = config.load_config()
        cfg["providers"]["deepseek"]["accounts"].append(
            {"id": "ds-2", "name": "second", "api_key": "deepseek-key-2"}
        )
        config.save_config(cfg)
        profile = self.client.post(
            "/api/profiles",
            json={"name": "p2", "provider_id": "deepseek", "account_id": "ds-2"},
        ).get_json()["profile"]

        sid = self.client.post(
            "/api/sessions",
            json={"cwd": str(self.cwd_a), "provider_id": "deepseek"},
        ).get_json()["session"]["id"]

        with mock.patch.dict(os.environ, {"ENV_DUMP": str(env_dump)}):
            response = self.client.post(
                f"/api/sessions/{sid}/start", json={"profile_id": profile["id"]}
            )
        self.assertTrue(response.get_json()["ok"])
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not (
                env_dump.exists()
                and "deepseek-key-2" in env_dump.read_text(encoding="utf-8")
            ):
                time.sleep(0.1)
            self.assertIn("deepseek-key-2", env_dump.read_text(encoding="utf-8"))
        finally:
            self.client.post(f"/api/sessions/{sid}/stop")

        # 会话条目未被污染（覆盖是一次性的）
        entry = next(
            s for s in config.load_config()["sessions"] if s["id"] == sid
        )
        self.assertIsNone(entry["account_id"])

    def test_delete_profile_keeps_session_working(self):
        profile = self.client.post(
            "/api/profiles",
            json={"name": "p1", "cwd": str(self.cwd_a), "provider_id": "deepseek"},
        ).get_json()["profile"]
        sid = self.client.post(
            "/api/sessions", json={"profile_id": profile["id"]}
        ).get_json()["session"]["id"]

        self.assertEqual(
            self.client.delete(f"/api/profiles/{profile['id']}").status_code, 200
        )

        # 会话已物化字段，删除档案不影响其启动/停止/列出
        start = self.client.post(f"/api/sessions/{sid}/start", json={})
        self.assertTrue(start.get_json()["ok"])
        self.assertTrue(start.get_json()["status"]["running"])
        self.assertTrue(
            self.client.post(f"/api/sessions/{sid}/stop").get_json()["ok"]
        )
        remaining = self.client.get("/api/sessions").get_json()["sessions"]
        self.assertEqual([s["id"] for s in remaining], [sid])


if __name__ == "__main__":
    unittest.main()
