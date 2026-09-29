import json
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cc_switch_ui.process import AgentProcess, SessionRegistry


class AgentProcessTests(unittest.TestCase):
    def test_running_status_exposes_non_secret_launch_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "fake-agent"
            script.write_text("#!/bin/sh\ncat >/dev/null\n", encoding="utf-8")
            script.chmod(0o700)
            process = AgentProcess()

            ok, _ = process.start(
                {"SECRET": "not-for-status"},
                "Custom OpenAI",
                command=[str(script)],
                client="codex",
                launch_snapshot={
                    "provider_id": "codex_custom",
                    "provider_label": "Custom OpenAI",
                    "model": "custom-model",
                    "account_name": "server",
                },
                launch_signature="private-signature",
            )
            status = process.status()

            self.assertTrue(ok)
            self.assertEqual(status["launch"]["client"], "codex")
            self.assertEqual(status["launch"]["provider_id"], "codex_custom")
            self.assertEqual(status["launch"]["account_name"], "server")
            self.assertNotIn("SECRET", status["launch"])
            self.assertEqual(process._launch_signature, "private-signature")
            self.assertEqual(process._last_launch_signature, "private-signature")
            self.assertNotIn("private-signature", status["launch"])
            process.stop()
            process.reader_thread.join(timeout=2)

    def test_child_environment_is_sanitized_and_pty_is_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "fake-agent"
            captured = Path(temp_dir) / "environment.txt"
            script.write_text("#!/bin/sh\nenv | sort > \"$1\"\n", encoding="utf-8")
            script.chmod(0o700)

            process = AgentProcess()
            with mock.patch.dict(
                os.environ,
                {
                    "ANTHROPIC_BASE_URL": "https://stale.invalid",
                    "OPENAI_API_KEY": "stale-key",
                },
            ):
                ok, _ = process.start(
                    {"CC_SWITCH_CODEX_API_KEY": "fresh-key"},
                    "test",
                    command=[str(script), str(captured)],
                    clear_env=("ANTHROPIC_BASE_URL", "OPENAI_API_KEY"),
                    client="codex",
                )

            self.assertTrue(ok)
            process.reader_thread.join(timeout=3)
            self.assertFalse(process.reader_thread.is_alive())
            self.assertIsNone(process.master_fd)
            self.assertEqual(process.last_exit_code, 0)

            environment = captured.read_text(encoding="utf-8")
            self.assertNotIn("ANTHROPIC_BASE_URL=", environment)
            self.assertNotIn("OPENAI_API_KEY=", environment)
            self.assertIn("CC_SWITCH_CODEX_API_KEY=fresh-key", environment)

    def test_restart_does_not_let_old_reader_close_new_pty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "fake-agent"
            script.write_text("#!/bin/sh\ncat >/dev/null\n", encoding="utf-8")
            script.chmod(0o700)

            process = AgentProcess()
            first_ok, _ = process.start({}, "first", command=[str(script)])
            self.assertTrue(first_ok)
            first_reader = process.reader_thread
            self.assertTrue(process.stop()[0])

            second_ok, _ = process.start({}, "second", command=[str(script)])
            self.assertTrue(second_ok)
            second_fd = process.master_fd
            first_reader.join(timeout=2)
            time.sleep(0.1)

            self.assertTrue(process.is_running())
            self.assertEqual(process.master_fd, second_fd)
            self.assertTrue(process.send_input("ping\n")[0])
            process.stop()
            process.reader_thread.join(timeout=2)

    def test_watchdog_reuses_launch_signature(self):
        process = AgentProcess()
        process.proc = object()
        process.keepalive = True
        process.started_at = time.time()
        process.last_launch = {"rows": 30, "cols": 100, "cwd": "/tmp"}
        process._last_launch_signature = "private-signature"

        with (
            mock.patch("cc_switch_ui.process.time.sleep"),
            mock.patch.object(process, "start") as start,
        ):
            process._on_exit(process.proc)

        self.assertEqual(start.call_args.kwargs["launch_signature"], "private-signature")

    # ---- 0.10 看门狗用最新配置重启 ----
    def test_watchdog_rebuilds_launch_from_latest_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir = Path(temp_dir)
            bin_dir = temp_dir / "bin"
            bin_dir.mkdir()
            env_dump = temp_dir / "env.dump"
            agent = bin_dir / "claude"
            agent.write_text(
                "#!/bin/sh\necho \"$ANTHROPIC_AUTH_TOKEN\" > \"$ENV_DUMP\"\ncat\n",
                encoding="utf-8",
            )
            agent.chmod(0o700)
            cfg_file = temp_dir / "config.json"

            def write_config(key):
                cfg_file.write_text(json.dumps({"api_key": key}), encoding="utf-8")

            def rebuild():
                # 模拟 server 钩子：每次退出后按最新配置重建 launch
                cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
                key = cfg.get("api_key") or ""
                if not key:
                    return None
                return (
                    {
                        "ready": True,
                        "command": ["claude"],
                        "env": {"ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_API_KEY": ""},
                        "clear_env": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
                        "label": "测试供应商",
                        "provider_id": "test",
                        "provider_label": "测试供应商",
                        "client": "claude",
                        "base_url": "",
                        "model": "",
                        "account_id": "a1",
                        "account_name": "acct",
                        "session_mode": "new",
                    },
                    f"fp-{key}",
                )

            write_config("key-A")
            process = AgentProcess()
            process.set_rebuild_launch(rebuild)
            process.keepalive = True
            process.last_launch = {"rows": 24, "cols": 80, "cwd": str(temp_dir)}

            with mock.patch.dict(
                os.environ,
                {
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "ENV_DUMP": str(env_dump),
                },
            ):
                ok, _ = process.start(
                    {"ANTHROPIC_AUTH_TOKEN": "key-A"},
                    "测试供应商",
                    command=["claude"],
                    client="claude",
                    cwd=str(temp_dir),
                    clear_env=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
                )
                self.assertTrue(ok)

                def dump_contains(key):
                    return (
                        env_dump.exists()
                        and key in env_dump.read_text(encoding="utf-8")
                    )

                deadline = time.time() + 10
                while time.time() < deadline and not dump_contains("key-A"):
                    time.sleep(0.05)
                self.assertTrue(dump_contains("key-A"))

                write_config("key-B")
                os.kill(process.proc.pid, signal.SIGKILL)

                deadline = time.time() + 15
                while time.time() < deadline and not dump_contains("key-B"):
                    time.sleep(0.05)

                process.stop()
                process.reader_thread.join(timeout=3)

            self.assertTrue(dump_contains("key-B"))
            self.assertEqual(process._launch_signature, "fp-key-B")

    def test_watchdog_stops_when_rebuild_reports_unavailable(self):
        process = AgentProcess()
        process.proc = object()
        process.keepalive = True
        process.started_at = time.time()
        process.last_launch = {"rows": 24, "cols": 80, "cwd": "/tmp"}
        process.set_rebuild_launch(lambda: None)
        queue = process.subscribe()

        with (
            mock.patch("cc_switch_ui.process.time.sleep"),
            mock.patch.object(process, "start") as start,
        ):
            process._on_exit(process.proc)

        start.assert_not_called()
        self.assertFalse(process.keepalive)
        messages = []
        while not queue.empty():
            messages.append(queue.get_nowait())
        self.assertTrue(
            any(
                "[看门狗] 当前选择不可用，已停止自动重启。请检查配置后手动启动。" in message
                for message in messages
            )
        )
        process.unsubscribe(queue)

    def test_watchdog_rebuild_success_uses_new_launch_and_fingerprint(self):
        process = AgentProcess()
        process.proc = object()
        process.keepalive = True
        process.started_at = time.time()
        process.last_launch = {"rows": 30, "cols": 100, "cwd": "/tmp"}
        process.set_rebuild_launch(
            lambda: (
                {
                    "ready": True,
                    "env": {"ANTHROPIC_AUTH_TOKEN": "new-key"},
                    "label": "新供应商",
                    "command": ["claude"],
                    "clear_env": ("ANTHROPIC_API_KEY",),
                    "client": "claude",
                    "provider_id": "test",
                    "provider_label": "新供应商",
                    "base_url": "",
                    "model": "",
                    "account_id": "a1",
                    "account_name": "acct",
                    "session_mode": "new",
                },
                "new-fingerprint",
            )
        )

        with (
            mock.patch("cc_switch_ui.process.time.sleep"),
            mock.patch.object(process, "start") as start,
        ):
            process._on_exit(process.proc)

        call = start.call_args
        self.assertEqual(call.args[0], {"ANTHROPIC_AUTH_TOKEN": "new-key"})
        self.assertEqual(call.args[1], "新供应商")
        self.assertEqual(call.kwargs["rows"], 30)
        self.assertEqual(call.kwargs["cols"], 100)
        self.assertEqual(call.kwargs["cwd"], "/tmp")
        self.assertEqual(call.kwargs["command"], ["claude"])
        self.assertEqual(call.kwargs["clear_env"], ("ANTHROPIC_API_KEY",))
        self.assertEqual(call.kwargs["client"], "claude")
        self.assertEqual(call.kwargs["launch_signature"], "new-fingerprint")
        self.assertEqual(call.kwargs["launch_snapshot"]["provider_id"], "test")

    # ---- 0.11 停止后状态清理 + 熔断回归 ----
    def test_stop_clears_pty_state_and_winsize_is_noop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "fake-agent"
            script.write_text("#!/bin/sh\ncat >/dev/null\n", encoding="utf-8")
            script.chmod(0o700)
            process = AgentProcess()
            ok, _ = process.start({}, "test", command=[str(script)])
            self.assertTrue(ok)
            self.assertIsNotNone(process.launch_snapshot)
            self.assertTrue(process.stop()[0])
            process.reader_thread.join(timeout=3)

            self.assertIsNone(process.master_fd)
            self.assertIsNone(process.launch_snapshot)
            self.assertIsNotNone(process.proc)  # 保留死引用
            self.assertFalse(process.is_running())
            process.set_winsize(24, 80)  # no-op，不得抛错

    def test_reset_fast_fail_clears_counter(self):
        process = AgentProcess()
        process._fast_fail = 4
        process.reset_fast_fail()
        self.assertEqual(process._fast_fail, 0)

    def test_fast_fail_circuit_still_trips_after_five_quick_exits(self):
        process = AgentProcess()
        process.proc = object()
        process.keepalive = True

        with (
            mock.patch("cc_switch_ui.process.time.sleep"),
            mock.patch.object(process, "start") as start,
        ):
            for _ in range(5):
                process.started_at = time.time()
                process._on_exit(process.proc)

        self.assertFalse(process.keepalive)
        self.assertEqual(start.call_count, 4)

    # ---- 2.2 pty 输出落盘 ----
    def test_pty_output_is_written_to_log_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "fake-agent"
            script.write_text("#!/bin/sh\ncat\n", encoding="utf-8")
            script.chmod(0o700)
            log_file = Path(temp_dir) / "agent.log"
            process = AgentProcess()
            ok, _ = process.start(
                {}, "test", command=[str(script)], log_path=log_file
            )
            self.assertTrue(ok)
            self.assertEqual(process.status()["log_path"], str(log_file))

            self.assertTrue(process.send_input("log-marker-12345\n")[0])
            deadline = time.time() + 5
            while time.time() < deadline:
                if log_file.exists() and b"log-marker-12345" in log_file.read_bytes():
                    break
                time.sleep(0.05)
            self.assertIn(b"log-marker-12345", log_file.read_bytes())

            process.stop()
            process.reader_thread.join(timeout=3)
            self.assertIn(b"log-marker-12345", log_file.read_bytes())
            # 停止后仍展示最近一次日志路径
            self.assertEqual(process.status()["log_path"], str(log_file))

    def test_log_writing_stops_at_cap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "fake-agent"
            script.write_text("#!/bin/sh\ncat\n", encoding="utf-8")
            script.chmod(0o700)
            log_file = Path(temp_dir) / "agent.log"
            process = AgentProcess()
            ok, _ = process.start(
                {}, "test", command=[str(script)], log_path=log_file, log_cap=64
            )
            self.assertTrue(ok)

            self.assertTrue(process.send_input("x" * 200 + "\n")[0])
            process.stop()
            process.reader_thread.join(timeout=3)

            self.assertEqual(log_file.stat().st_size, 64)

    # ---- 4.6 「等输入」空闲时长 ----
    def test_idle_seconds_grows_and_drops_after_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "fake-agent"
            script.write_text("#!/bin/sh\ncat >/dev/null\n", encoding="utf-8")
            script.chmod(0o700)
            process = AgentProcess()
            ok, _ = process.start({}, "test", command=[str(script)])
            self.assertTrue(ok)

            time.sleep(1.2)
            first = process.status()["idle_seconds"]
            self.assertGreaterEqual(first, 1)
            time.sleep(1.2)
            self.assertGreater(process.status()["idle_seconds"], first)

            # pty 回显即输出，idle 应回落
            self.assertTrue(process.send_input("wake-up\n")[0])
            deadline = time.time() + 3
            while time.time() < deadline:
                if process.status()["idle_seconds"] <= 1:
                    break
                time.sleep(0.1)
            self.assertLessEqual(process.status()["idle_seconds"], 1)

            process.stop()
            process.reader_thread.join(timeout=3)
            self.assertNotIn("idle_seconds", process.status())

    # ---- 4.12 资源占用 ----
    def test_rss_kb_reported_for_running_process(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "fake-agent"
            script.write_text("#!/bin/sh\ncat >/dev/null\n", encoding="utf-8")
            script.chmod(0o700)
            process = AgentProcess()
            ok, _ = process.start({}, "test", command=[str(script)])
            self.assertTrue(ok)

            status = process.status()
            self.assertIsInstance(status["rss_kb"], int)
            self.assertGreater(status["rss_kb"], 0)
            # 5 秒缓存：紧接着的第二次查询返回同值
            self.assertEqual(process.status()["rss_kb"], status["rss_kb"])

            process.stop()
            process.reader_thread.join(timeout=3)
            self.assertNotIn("rss_kb", process.status())


class SessionRegistryTests(unittest.TestCase):
    """3.2 SessionRegistry：上限、默认会话、remove 停止子进程。"""

    def test_get_or_create_default_is_stable(self):
        registry = SessionRegistry()
        first = registry.get_or_create_default()
        second = registry.get_or_create_default()
        self.assertIs(first, second)
        self.assertIs(registry.get(SessionRegistry.DEFAULT_ID), first)
        self.assertEqual(len(registry), 1)

    def test_create_raises_at_limit(self):
        registry = SessionRegistry(max_sessions=2)
        registry.get_or_create_default()
        registry.create()
        with self.assertRaises(ValueError):
            registry.create()
        self.assertEqual(len(registry), 2)

    def test_create_generates_sid_when_none(self):
        registry = SessionRegistry()
        registry.get_or_create_default()
        proc = registry.create()
        self.assertEqual(len(registry), 2)
        created = [p for p in registry.list() if p is proc]
        self.assertEqual(len(created), 1)
        self.assertNotEqual(proc, registry.get(SessionRegistry.DEFAULT_ID))

    def test_remove_stops_running_child(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "fake-agent"
            script.write_text("#!/bin/sh\ncat >/dev/null\n", encoding="utf-8")
            script.chmod(0o700)
            registry = SessionRegistry()
            registry.get_or_create_default()
            proc = registry.create("victim")
            ok, _ = proc.start({}, "test", command=[str(script)])
            self.assertTrue(ok)
            pid = proc.proc.pid

            removed = registry.remove("victim")

            self.assertIs(removed, proc)
            self.assertIsNone(registry.get("victim"))
            deadline = time.time() + 10
            while time.time() < deadline and proc.proc.poll() is None:
                time.sleep(0.05)
            self.assertIsNotNone(proc.proc.poll())
            self.assertFalse(proc.is_running())
            self.assertEqual(len(registry), 1)

    def test_remove_unknown_returns_none(self):
        registry = SessionRegistry()
        registry.get_or_create_default()
        self.assertIsNone(registry.remove("nope"))


if __name__ == "__main__":
    unittest.main()
