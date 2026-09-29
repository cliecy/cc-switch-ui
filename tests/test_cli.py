import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock
import urllib.error

import cc_switch_ui.cli as cli

URL = "http://127.0.0.1:8765"

STATE = {
    "current_provider": "deepseek",
    "providers": {
        "deepseek": {
            "id": "deepseek",
            "label": "DeepSeek",
            "client": "claude",
            "base_url": "",
            "model": "deepseek-chat",
            "auth_var": "ANTHROPIC_AUTH_TOKEN",
            "accounts": [
                {"id": "acc1", "name": "work", "shared": False,
                 "key_masked": "sk••••••x", "has_key": True},
                {"id": "acc2", "name": "shared-acct", "shared": True,
                 "key_masked": "sk••••••y", "has_key": True},
            ],
            "active_account": "acc1",
            "readiness": {"ready": True, "account_name": "work"},
        },
        "claude": {
            "id": "claude",
            "label": "Claude 官方",
            "client": "claude",
            "base_url": "",
            "model": "",
            "auth_var": "ANTHROPIC_API_KEY",
            "accounts": [
                {"id": "acc9", "name": "me", "shared": False,
                 "key_masked": "", "has_key": False},
            ],
            "active_account": "acc9",
            "readiness": {"ready": False, "account_name": ""},
        },
    },
    "selected_launch": {
        "provider_id": "deepseek",
        "provider_label": "DeepSeek",
        "account_name": "work",
        "ready": True,
        "missing": [],
    },
    "agent_status": {"running": True, "pid": 4242, "uptime": 61},
}

SESSIONS = {"ok": True, "sessions": [
    {"id": "abc123", "name": "work", "cwd": None, "provider_id": "deepseek",
     "provider_label": "DeepSeek", "session_mode": "new", "account_id": "acc1",
     "running": True, "pid": 4242, "uptime": 61, "client": "claude",
     "restart_required": False, "idle_seconds": 1, "log_path": None, "launch": {}},
    {"id": "def456", "name": "", "cwd": None, "provider_id": "claude",
     "provider_label": "Claude 官方", "session_mode": "new", "account_id": None,
     "running": False, "pid": None, "uptime": 0, "client": "claude",
     "restart_required": False, "idle_seconds": None, "log_path": None, "launch": {}},
]}

PROFILES = {"ok": True, "profiles": [
    {"id": "prof9", "name": "work-proj", "cwd": "/tmp/w", "provider_id": "deepseek",
     "account_id": "acc1", "session_mode": "new"},
]}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def req_info(req):
    return {
        "method": req.get_method(),
        "url": req.full_url,
        "headers": {k.lower(): v for k, v in req.headers.items()},
        "body": json.loads(req.data.decode("utf-8")) if req.data is not None else None,
    }


def run_cli(args, responses, url=URL):
    """Run cli.main with canned urlopen responses.

    Returns (stdout, stderr, requests, exit_code).
    """
    requests = []
    queue = list(responses)

    def fake_urlopen(req, timeout=None):
        requests.append(req)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    out, err = io.StringIO(), io.StringIO()
    code = 0
    with (
        mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
        mock.patch("sys.stdout", out),
        mock.patch("sys.stderr", err),
    ):
        try:
            cli.main(["--url", url, *args])
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return out.getvalue(), err.getvalue(), requests, code


class StatusCommandTests(unittest.TestCase):
    def test_status_json_passthrough(self):
        out, err, requests, code = run_cli(["status", "--json"], [FakeResponse(STATE)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), STATE)
        self.assertEqual([req_info(r) for r in requests], [{
            "method": "GET", "url": URL + "/api/state",
            "headers": {"accept": "application/json"}, "body": None,
        }])

    def test_status_summary(self):
        out, err, requests, code = run_cli(
            ["status"], [FakeResponse(STATE), FakeResponse(SESSIONS)]
        )
        self.assertEqual(code, 0)
        self.assertIn("Provider: DeepSeek (deepseek)", out)
        self.assertIn("Account:  work [ready]", out)
        self.assertIn("Agent:    running (pid 4242, uptime 61s)", out)
        self.assertIn("Sessions: 2", out)
        self.assertEqual(
            [r.full_url for r in requests],
            [URL + "/api/state", URL + "/api/sessions"],
        )


class UseCommandTests(unittest.TestCase):
    def test_use_posts_provider_and_bearer_token(self):
        out, err, requests, code = run_cli(
            ["--token", "s3cret", "use", "deepseek"],
            [FakeResponse({"ok": True, "current_provider": "deepseek"})],
        )
        self.assertEqual(code, 0)
        self.assertIn("已切换到 deepseek", out)
        info = req_info(requests[0])
        self.assertEqual(info["method"], "POST")
        self.assertEqual(info["url"], URL + "/api/provider/switch")
        self.assertEqual(info["body"], {"provider": "deepseek"})
        self.assertEqual(info["headers"]["authorization"], "Bearer s3cret")
        self.assertEqual(info["headers"]["content-type"], "application/json")

    def test_use_without_token_has_no_auth_header(self):
        _, _, requests, code = run_cli(
            ["use", "claude"], [FakeResponse({"ok": True})]
        )
        self.assertEqual(code, 0)
        self.assertNotIn("authorization", req_info(requests[0])["headers"])


class SessionsCommandTests(unittest.TestCase):
    def test_sessions_table(self):
        out, err, requests, code = run_cli(["sessions"], [FakeResponse(SESSIONS)])
        self.assertEqual(code, 0)
        flat = " ".join(out.split())
        self.assertIn("ID NAME PROVIDER RUNNING PID UPTIME(s)", flat)
        self.assertIn("abc123 work deepseek yes 4242 61", flat)
        self.assertIn("def456 def456 claude no - 0", flat)

    def test_session_create_sends_all_fields(self):
        out, err, requests, code = run_cli(
            ["session", "create", "work", "--cwd", "/tmp/x",
             "--provider", "deepseek", "--profile", "prof1"],
            [FakeResponse({"ok": True, "session": {"id": "new789"}})],
        )
        self.assertEqual(code, 0)
        self.assertIn("new789", out)
        info = req_info(requests[0])
        self.assertEqual(info["method"], "POST")
        self.assertEqual(info["url"], URL + "/api/sessions")
        self.assertEqual(info["body"], {
            "name": "work", "cwd": "/tmp/x",
            "provider_id": "deepseek", "profile_id": "prof1",
        })

    def test_session_create_minimal(self):
        _, _, requests, code = run_cli(
            ["session", "create", "quick"],
            [FakeResponse({"ok": True, "session": {"id": "q1"}})],
        )
        self.assertEqual(code, 0)
        self.assertEqual(req_info(requests[0])["body"], {"name": "quick"})

    def test_session_start_by_sid(self):
        out, err, requests, code = run_cli(
            ["session", "start", "abc123", "--cwd", "/tmp/x", "--mode", "continue"],
            [FakeResponse(SESSIONS), FakeResponse({"ok": True, "message": "started"})],
        )
        self.assertEqual(code, 0)
        self.assertIn("abc123", out)
        infos = [req_info(r) for r in requests]
        self.assertEqual(infos[0]["url"], URL + "/api/sessions")
        self.assertEqual(infos[1]["method"], "POST")
        self.assertEqual(infos[1]["url"], URL + "/api/sessions/abc123/start")
        self.assertEqual(infos[1]["body"], {"cwd": "/tmp/x", "session_mode": "continue"})

    def test_session_start_by_profile_name(self):
        _, _, requests, code = run_cli(
            ["session", "start", "work-proj"],
            [FakeResponse(SESSIONS), FakeResponse(PROFILES), FakeResponse({"ok": True})],
        )
        self.assertEqual(code, 0)
        infos = [req_info(r) for r in requests]
        self.assertEqual(infos[1]["url"], URL + "/api/profiles")
        self.assertEqual(infos[2]["method"], "POST")
        self.assertEqual(infos[2]["url"], URL + "/api/sessions/prof9/start")
        self.assertEqual(infos[2]["body"], {})

    def test_session_start_unknown_target_exits_1(self):
        _, err, requests, code = run_cli(
            ["session", "start", "nope"],
            [FakeResponse(SESSIONS), FakeResponse({"ok": True, "profiles": []})],
        )
        self.assertEqual(code, 1)
        self.assertIn("validation: 找不到会话或档案「nope」", err)
        self.assertEqual(len(requests), 2)  # 没有发 start 请求

    def test_session_stop_and_restart(self):
        _, _, requests, code = run_cli(
            ["session", "stop", "abc123"], [FakeResponse({"ok": True})]
        )
        self.assertEqual(code, 0)
        self.assertEqual(req_info(requests[0])["method"], "POST")
        self.assertEqual(requests[0].full_url, URL + "/api/sessions/abc123/stop")

        _, _, requests, code = run_cli(
            ["session", "restart", "abc123"], [FakeResponse({"ok": True})]
        )
        self.assertEqual(code, 0)
        self.assertEqual(requests[0].full_url, URL + "/api/sessions/abc123/restart")


class AccountCommandTests(unittest.TestCase):
    def test_account_ls_all(self):
        out, err, requests, code = run_cli(["account", "ls"], [FakeResponse(STATE)])
        self.assertEqual(code, 0)
        flat = " ".join(out.split())
        self.assertIn("NAME ID ACTIVE SHARED", flat)
        self.assertIn("work acc1 yes no", flat)
        self.assertIn("shared-acct acc2 no yes", flat)
        self.assertIn("me acc9 yes no", flat)

    def test_account_ls_filtered(self):
        out, err, requests, code = run_cli(
            ["account", "ls", "--provider", "deepseek"], [FakeResponse(STATE)]
        )
        self.assertEqual(code, 0)
        self.assertIn("acc1", out)
        self.assertNotIn("acc9", out)

    def test_account_add(self):
        out, err, requests, code = run_cli(
            ["account", "add", "deepseek", "work", "sk-12345"],
            [FakeResponse({"ok": True, "id": "acc777"})],
        )
        self.assertEqual(code, 0)
        self.assertIn("acc777", out)
        info = req_info(requests[0])
        self.assertEqual(info["method"], "POST")
        self.assertEqual(info["url"], URL + "/api/account")
        self.assertEqual(info["body"], {
            "provider": "deepseek", "name": "work", "api_key": "sk-12345",
        })

    def test_account_activate(self):
        _, _, requests, code = run_cli(
            ["account", "activate", "deepseek", "acc2"],
            [FakeResponse({"ok": True})],
        )
        self.assertEqual(code, 0)
        info = req_info(requests[0])
        self.assertEqual(info["method"], "POST")
        self.assertEqual(info["url"], URL + "/api/account/activate")
        self.assertEqual(info["body"], {"provider": "deepseek", "account_id": "acc2"})

    def test_account_rm_with_yes(self):
        out, err, requests, code = run_cli(
            ["account", "rm", "deepseek", "acc2", "--yes"],
            [FakeResponse({"ok": True})],
        )
        self.assertEqual(code, 0)
        info = req_info(requests[0])
        self.assertEqual(info["method"], "DELETE")
        self.assertEqual(info["url"], URL + "/api/account/acc2?provider=deepseek")

    def test_account_rm_non_tty_without_yes_exits_1(self):
        with mock.patch("sys.stdin", io.StringIO("")):
            out, err, requests, code = run_cli(
                ["account", "rm", "deepseek", "acc2"],
                [FakeResponse({"ok": True})],
            )
        self.assertEqual(code, 1)
        self.assertIn("非交互环境删除账号必须加 --yes", err)
        self.assertEqual(requests, [])

    def test_account_rm_confirms_yes(self):
        fake_stdin = mock.MagicMock()
        fake_stdin.isatty.return_value = True
        with mock.patch("sys.stdin", fake_stdin), mock.patch(
            "builtins.input", return_value="y"
        ):
            _, _, requests, code = run_cli(
                ["account", "rm", "deepseek", "acc2"],
                [FakeResponse({"ok": True})],
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get_method(), "DELETE")

    def test_account_rm_confirms_no_cancels(self):
        fake_stdin = mock.MagicMock()
        fake_stdin.isatty.return_value = True
        with mock.patch("sys.stdin", fake_stdin), mock.patch(
            "builtins.input", return_value="n"
        ):
            out, err, requests, code = run_cli(
                ["account", "rm", "deepseek", "acc2"],
                [FakeResponse({"ok": True})],
            )
        self.assertEqual(code, 0)
        self.assertIn("已取消", out)
        self.assertEqual(requests, [])


class ExportImportCommandTests(unittest.TestCase):
    def test_export_default(self):
        body = {"ok": True, "config": {"current_provider": "deepseek", "providers": {}}}
        out, err, requests, code = run_cli(["export"], [FakeResponse(body)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), body)
        info = req_info(requests[0])
        self.assertEqual(info["method"], "GET")
        self.assertEqual(info["url"], URL + "/api/config/export")

    def test_export_include_keys(self):
        _, _, requests, code = run_cli(
            ["export", "--include-keys"],
            [FakeResponse({"ok": True, "config": {}})],
        )
        self.assertEqual(code, 0)
        self.assertEqual(requests[0].full_url, URL + "/api/config/export?include_keys=1")

    def test_import_merge(self):
        cfg = {"current_provider": "deepseek", "providers": {"deepseek": {}}}
        with self._tmp_json(cfg) as path:
            _, _, requests, code = run_cli(["import", path], [FakeResponse({"ok": True})])
        self.assertEqual(code, 0)
        info = req_info(requests[0])
        self.assertEqual(info["method"], "POST")
        self.assertEqual(info["url"], URL + "/api/config/import")
        self.assertEqual(info["body"], {"config": cfg, "mode": "merge"})

    def test_import_replace(self):
        cfg = {"providers": {"claude": {}}}
        with self._tmp_json(cfg) as path:
            _, _, requests, code = run_cli(
                ["import", path, "--replace"], [FakeResponse({"ok": True})]
            )
        self.assertEqual(code, 0)
        self.assertEqual(req_info(requests[0])["body"]["mode"], "replace")

    def test_import_accepts_export_wrapper(self):
        wrapper = {"ok": True, "config": {"providers": {"deepseek": {}}}}
        with self._tmp_json(wrapper) as path:
            _, _, requests, code = run_cli(["import", path], [FakeResponse({"ok": True})])
        self.assertEqual(code, 0)
        self.assertEqual(
            req_info(requests[0])["body"]["config"], {"providers": {"deepseek": {}}}
        )

    def test_import_bad_file_exits_1(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{{{")
            path = f.name
        _, err, requests, code = run_cli(["import", path], [])
        self.assertEqual(code, 1)
        self.assertIn("无法读取导入文件", err)
        self.assertEqual(requests, [])

    @classmethod
    @contextlib.contextmanager
    def _tmp_json(cls, obj):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(obj, f)
        f.close()
        try:
            yield f.name
        finally:
            os.unlink(f.name)


class ErrorHandlingTests(unittest.TestCase):
    def test_ok_false_body_exits_1_with_code(self):
        _, err, requests, code = run_cli(
            ["use", "deepseek"],
            [FakeResponse({"ok": False, "code": "not_ready", "error": "未配置可用 API Key"})],
        )
        self.assertEqual(code, 1)
        self.assertIn("not_ready: 未配置可用 API Key", err)

    def test_http_400_exits_1_with_server_code(self):
        payload = {"ok": False, "code": "unknown_provider", "error": "未知供应商"}
        http_error = urllib.error.HTTPError(
            URL + "/api/provider/switch", 400, "Bad Request", {},
            io.BytesIO(json.dumps(payload).encode("utf-8")),
        )
        _, err, requests, code = run_cli(["use", "nope"], [http_error])
        self.assertEqual(code, 1)
        self.assertIn("unknown_provider: 未知供应商", err)

    def test_http_500_without_json_body(self):
        http_error = urllib.error.HTTPError(
            URL + "/api/state", 500, "Server Error", {}, io.BytesIO(b"boom")
        )
        _, err, requests, code = run_cli(["status", "--json"], [http_error])
        self.assertEqual(code, 1)
        self.assertIn("http_error: HTTP 500", err)

    def test_network_unreachable_exits_1(self):
        _, err, requests, code = run_cli(
            ["status", "--json"],
            [urllib.error.URLError("Connection refused")],
        )
        self.assertEqual(code, 1)
        self.assertIn("network: 无法连接", err)


if __name__ == "__main__":
    unittest.main()
