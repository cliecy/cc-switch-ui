import unittest
from pathlib import Path


class AssetTests(unittest.TestCase):
    @staticmethod
    def _frontend() -> str:
        root = Path(__file__).resolve().parents[1]
        return (root / "src" / "cc_switch_ui" / "index.html").read_text(encoding="utf-8")

    def test_root_and_packaged_frontends_are_synchronized(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(
            (root / "index.html").read_text(encoding="utf-8"),
            (root / "src" / "cc_switch_ui" / "index.html").read_text(encoding="utf-8"),
        )

    def test_frontend_exposes_primary_views_and_next_launch_semantics(self):
        frontend = self._frontend()

        for element_id in (
            "view-run",
            "view-connections",
            "view-tools",
            "running-summary",
            "run-meta",
            "switch-notice",
        ):
            self.assertIn(f'id="{element_id}"', frontend)
        for view in ("run", "connections", "tools"):
            self.assertIn(f'data-view="{view}"', frontend)

        self.assertIn('role="dialog"', frontend)
        self.assertIn('type="password"', frontend)
        self.assertIn("CC_SWITCH_CODEX_API_KEY（Codex 自动管理）", frontend)
        self.assertIn("ANTHROPIC_AUTH_TOKEN（多数第三方）", frontend)
        self.assertIn("留空 = 当前工作目录", frontend)
        for obsolete_label in ("设为当前", "终端已连接"):
            self.assertNotIn(obsolete_label, frontend)

    def test_frontend_uses_local_static_assets_without_cdn(self):
        frontend = self._frontend()

        self.assertNotIn("cdn.jsdelivr.net", frontend)
        self.assertIn('href="/static/xterm.min.css"', frontend)
        self.assertIn('src="/static/xterm.min.js"', frontend)
        self.assertIn('src="/static/addon-fit.min.js"', frontend)

    def test_frontend_exposes_remediation_ui_elements(self):
        frontend = self._frontend()

        for element_id in (
            "in-use-line",
            "onboarding",
            "onboard-step-1",
            "onboard-step-2",
            "onboard-step-3",
            "cost-hint",
            "m-shared",
            "import-modal",
            "import-file",
            "btn-apply-import",
            "btn-apply-switch",
        ):
            self.assertIn(f'id="{element_id}"', frontend)
        for label in (
            "已删除（可恢复）",
            "共享 key（多人使用）",
            "导出配置",
            "导入配置",
            "导出 CSV",
        ):
            self.assertIn(label, frontend)
        self.assertIn("cc-switch-ui.ever-ran", frontend)
        self.assertIn('"/switch-restart"', frontend)
        self.assertIn("/api/account/trash", frontend)
        self.assertIn("/api/config/export", frontend)
        self.assertIn("/api/config/import", frontend)
        self.assertIn("/api/account/export.csv", frontend)

    def test_frontend_plain_language_user_strings(self):
        frontend = self._frontend()

        self.assertNotIn("wire_api", frontend)
        self.assertNotIn("Responses API", frontend)
        self.assertIn(
            "Codex 自定义连接：密钥在启动时自动注入，不会修改本机的 Codex 配置文件。",
            frontend,
        )

    def test_frontend_exposes_session_view(self):
        frontend = self._frontend()

        for element_id in (
            "session-list",
            "session-new-btn",
            "session-modal",
            "sm-name",
            "sm-cwd",
            "sm-provider",
            "sm-mode-seg",
            "btn-create-session",
        ):
            self.assertIn(f'id="{element_id}"', frontend)
        self.assertIn("class=\"idle-badge\"", frontend)
        self.assertIn("/api/sessions", frontend)
        self.assertIn("+ 新会话", frontend)
        self.assertIn("秒无输出", frontend)


if __name__ == "__main__":
    unittest.main()
