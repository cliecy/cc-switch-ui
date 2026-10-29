"""0.6.1 安全加固回归测试：Host 白名单 / rebinding / 密钥脱敏 XSS / 版本同步。"""

import contextlib
import importlib.metadata
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path

from cc_switch_ui import config
from cc_switch_ui.server import _allowed_host, create_app, mask_key


class HelperTests(unittest.TestCase):
    def test_mask_key_strips_unsafe_chars(self):
        # 含 HTML/JS 载荷的异常内容不能透传（< = 空格等剔除）
        masked = mask_key("<img src=x onerror=alert(1)>")
        self.assertNotIn("<", masked)
        self.assertNotIn("=", masked)
        self.assertNotIn("onerror", masked)
        self.assertEqual(mask_key("@@!!"), "•" * 8)  # 无任何安全字符时不透传原文
        # 正常 key 的脱敏形状不受影响（≤16 位保留首尾 2 位，>16 位保留首尾 4 位）
        self.assertEqual(mask_key("abcdef123456"), "ab••••••56")
        self.assertEqual(mask_key("abcdef12345678901234"), "abcd••••••1234")
        self.assertEqual(mask_key("short"), "•" * 5)
        self.assertEqual(mask_key(""), "")

    def test_allowed_host_helper(self):
        for ok in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765",
                   "192.168.1.5:8765", "mybook.local:8765", ""):
            self.assertTrue(_allowed_host(ok), ok)
        for bad in ("evil.example.com:8765", "attacker.io", "rebind.attacker.net"):
            self.assertFalse(_allowed_host(bad), bad)

    def test_allowed_host_env_override(self):
        old = os.environ.get("CC_ALLOWED_HOSTS")
        try:
            os.environ["CC_ALLOWED_HOSTS"] = "myserver.example.com"
            self.assertTrue(_allowed_host("myserver.example.com:8765"))
            self.assertFalse(_allowed_host("other.example.org"))
        finally:
            if old is None:
                os.environ.pop("CC_ALLOWED_HOSTS", None)
            else:
                os.environ["CC_ALLOWED_HOSTS"] = old

    def test_version_metadata_matches_pyproject(self):
        root = Path(__file__).resolve().parents[1]
        decl = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            self.assertEqual(importlib.metadata.version("cc-switch-ui"), decl)
        from cc_switch_ui import __version__
        # 已安装时应与声明一致；未安装时回退字面量也应与声明一致
        self.assertIn(__version__, (decl,))


class HostGuardTests(unittest.TestCase):
    """DNS rebinding 防护：恶意域名 Host 必须被 403（Origin 校验拦不住这类请求）。"""

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

    def test_rebinding_host_rejected(self):
        r = self.client.get("/api/state", headers={"Host": "evil.example.com:8765"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["code"], "host_mismatch")

    def test_rebinding_post_rejected_even_with_matching_origin(self):
        """rebind 时 Origin 与 Host 同名 —— 旧 Origin 校验放行，新 Host 守卫必须拦截。"""
        r = self.client.post(
            "/api/claude/keepalive",
            data=json.dumps({"enabled": True}),
            content_type="application/json",
            headers={"Host": "attacker.io:8765", "Origin": "http://attacker.io:8765"},
        )
        self.assertEqual(r.status_code, 403)

    def test_loopback_host_allowed(self):
        r = self.client.get("/api/state", headers={"Host": "127.0.0.1:8765"})
        self.assertEqual(r.status_code, 200)

    def test_plain_json_body_via_valid_host(self):
        r = self.client.post(
            "/api/claude/keepalive",
            data=json.dumps({"enabled": True}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_text_plain_body_no_longer_ambiguous(self):
        """原 force=True 对 text/plain 也强行解析；现统一 _json_body()，行为一致更可预期。"""
        r = self.client.post(
            "/api/claude/keepalive",
            data=json.dumps({"enabled": False}),
            content_type="text/plain",
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["keepalive"])


if __name__ == "__main__":
    unittest.main()