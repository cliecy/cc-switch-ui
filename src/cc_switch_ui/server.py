"""
Flask 应用工厂 + 全部路由。
"""

import ipaddress
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from .cli_manager import CliManager, CliManagerError
from .config import (
    AUTH_VARS,
    build_launch_for_active,
    get_lock,
    load_config,
    public_state,
    save_config,
)
from .process import AgentProcess

# --------------------------------------------------------------------------- #
# 应用工厂
# --------------------------------------------------------------------------- #

# 包内 static 目录（index.html 所在）
_PKG_DIR = Path(__file__).resolve().parent


def create_app(*, allow_cli_management=False, cli_manager=None):
    app = Flask(__name__, static_folder=None)
    agent_proc = AgentProcess()
    cli_manager = cli_manager or CliManager()

    def terminal_size(data):
        try:
            rows = int(data.get("rows", 24))
            cols = int(data.get("cols", 80))
        except (TypeError, ValueError):
            return None, None, "终端尺寸必须是整数"
        if not (2 <= rows <= 500 and 2 <= cols <= 500):
            return None, None, "终端尺寸超出范围"
        return rows, cols, None

    def session_mode(data):
        mode = data.get("session_mode")
        if mode:
            return mode
        # Compatibility with the original API's Claude argument list.
        args = data.get("args") or []
        if args == ["--continue"]:
            return "continue"
        if args == ["--resume"]:
            return "resume"
        return "new"

    def request_is_loopback():
        try:
            return ipaddress.ip_address(request.remote_addr or "").is_loopback
        except ValueError:
            return False

    # ------------------------------------------------------------------- #
    # 路由
    # ------------------------------------------------------------------- #

    @app.route("/")
    def index():
        return send_from_directory(_PKG_DIR, "index.html")

    @app.get("/api/state")
    def api_state():
        state = public_state()
        state["agent_status"] = agent_proc.status()
        state["claude_status"] = state["agent_status"]  # backward compatibility
        return jsonify(state)

    @app.get("/api/cli/status")
    def api_cli_status():
        state = cli_manager.status()
        state["management_enabled"] = bool(allow_cli_management)
        return jsonify(state)

    @app.post("/api/cli/check")
    def api_cli_check():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        try:
            result = cli_manager.latest_versions(data.get("registry", "official"))
        except CliManagerError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, **result})

    @app.post("/api/cli/manage")
    def api_cli_manage():
        if not request_is_loopback():
            return jsonify({
                "ok": False,
                "error": "CLI 安装/更新只接受回环地址请求；请使用 SSH 隧道",
            }), 403
        if not allow_cli_management:
            return jsonify({
                "ok": False,
                "error": "CLI 管理未启用；请用 --allow-cli-management 重启面板",
            }), 403
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        if agent_proc.status().get("running"):
            return jsonify({
                "ok": False,
                "error": "请先停止正在运行的 Agent，再安装或更新 CLI",
            }), 409
        expected = f'{data.get("action", "")}:{data.get("agent", "")}'
        if data.get("confirm") != expected:
            return jsonify({"ok": False, "error": "缺少明确操作确认"}), 400
        try:
            result = cli_manager.manage(
                agent=data.get("agent", ""),
                action=data.get("action", ""),
                version=data.get("version", "latest"),
                registry=data.get("registry", "official"),
            )
        except CliManagerError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify(result)

    @app.post("/api/provider/switch")
    def api_switch_provider():
        data = request.get_json(force=True)
        pid = data.get("provider")
        with get_lock():
            cfg = load_config()
            if pid not in cfg["providers"]:
                return jsonify({"ok": False, "error": "未知供应商"}), 400
            cfg["current_provider"] = pid
            client = cfg["providers"][pid].get("client", "claude")
            save_config(cfg)

        # 若系统存在 ccm，则调用其切换命令（不强制依赖）
        ccm_output = None
        if client == "claude" and shutil.which("ccm"):
            try:
                res = subprocess.run(
                    ["ccm", "use", pid],
                    capture_output=True, text=True, timeout=15,
                )
                ccm_output = (res.stdout + res.stderr).strip()
            except Exception as e:  # noqa: BLE001
                ccm_output = f"ccm 调用失败: {e}"

        return jsonify({"ok": True, "current_provider": pid, "ccm_output": ccm_output})

    @app.put("/api/provider/<pid>")
    def api_edit_provider(pid):
        """编辑供应商端点：base_url / model / 鉴权变量 / 显示名。"""
        data = request.get_json(force=True)
        with get_lock():
            cfg = load_config()
            p = cfg["providers"].get(pid)
            if not p:
                return jsonify({"ok": False, "error": "未知供应商"}), 400
            for field in ("label", "base_url", "model"):
                if field in data and data[field] is not None:
                    p[field] = str(data[field]).strip()
            if data.get("auth_var") in AUTH_VARS:
                p["auth_var"] = data["auth_var"]
            if not p.get("label"):
                p["label"] = pid
            save_config(cfg)
        return jsonify({"ok": True})

    @app.post("/api/account")
    def api_add_account():
        data = request.get_json(force=True)
        pid = data.get("provider")
        name = (data.get("name") or "").strip() or "未命名账号"
        api_key = (data.get("api_key") or "").strip()
        with get_lock():
            cfg = load_config()
            if pid not in cfg["providers"]:
                return jsonify({"ok": False, "error": "未知供应商"}), 400
            acc = {"id": uuid.uuid4().hex[:12], "name": name, "api_key": api_key}
            cfg["providers"][pid]["accounts"].append(acc)
            if not cfg["providers"][pid].get("active_account"):
                cfg["providers"][pid]["active_account"] = acc["id"]
            save_config(cfg)
        return jsonify({"ok": True, "id": acc["id"]})

    @app.put("/api/account/<aid>")
    def api_edit_account(aid):
        data = request.get_json(force=True)
        pid = data.get("provider")
        with get_lock():
            cfg = load_config()
            provider = cfg["providers"].get(pid)
            if not provider:
                return jsonify({"ok": False, "error": "未知供应商"}), 400
            for acc in provider["accounts"]:
                if acc["id"] == aid:
                    if "name" in data:
                        acc["name"] = (data.get("name") or "").strip() or acc["name"]
                    # 仅在传入非空 key 时更新，避免脱敏回传覆盖真实值
                    if data.get("api_key"):
                        acc["api_key"] = data["api_key"].strip()
                    save_config(cfg)
                    return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "账号不存在"}), 404

    @app.delete("/api/account/<aid>")
    def api_delete_account(aid):
        pid = request.args.get("provider")
        with get_lock():
            cfg = load_config()
            provider = cfg["providers"].get(pid)
            if not provider:
                return jsonify({"ok": False, "error": "未知供应商"}), 400
            before = len(provider["accounts"])
            provider["accounts"] = [a for a in provider["accounts"] if a["id"] != aid]
            if provider.get("active_account") == aid:
                provider["active_account"] = (
                    provider["accounts"][0]["id"] if provider["accounts"] else None
                )
            save_config(cfg)
            if len(provider["accounts"]) == before:
                return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True})

    @app.post("/api/account/activate")
    def api_activate_account():
        data = request.get_json(force=True)
        pid = data.get("provider")
        aid = data.get("account_id")
        with get_lock():
            cfg = load_config()
            provider = cfg["providers"].get(pid)
            if not provider:
                return jsonify({"ok": False, "error": "未知供应商"}), 400
            if aid not in [a["id"] for a in provider["accounts"]]:
                return jsonify({"ok": False, "error": "账号不存在"}), 404
            provider["active_account"] = aid
            save_config(cfg)
        return jsonify({"ok": True})

    @app.post("/api/agent/start")
    @app.post("/api/claude/start")
    def api_agent_start():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        rows, cols, error = terminal_size(data)
        if error:
            return jsonify({"ok": False, "error": error}), 400
        raw_cwd = data.get("cwd") or ""
        if not isinstance(raw_cwd, str):
            return jsonify({"ok": False, "error": "工作目录必须是字符串"}), 400
        cwd = raw_cwd.strip() or None

        mode = session_mode(data)
        launch = build_launch_for_active(mode)
        if not launch.get("ready"):
            return jsonify({"ok": False, "error": launch["error"]}), 400
        # 记住本次启动参数，供「重启」复用
        agent_proc.last_launch = {
            "session_mode": mode, "rows": rows, "cols": cols, "cwd": cwd,
        }
        ok, msg = agent_proc.start(
            launch["env"], launch["label"], rows=rows, cols=cols, cwd=cwd,
            command=launch["command"], clear_env=launch["clear_env"],
            client=launch["client"],
        )
        return jsonify({"ok": ok, "message": msg, "status": agent_proc.status()})

    @app.post("/api/agent/restart")
    @app.post("/api/claude/restart")
    def api_agent_restart():
        data = request.get_json(silent=True) or {}
        # 先验证全部参数和新 provider，避免无效请求先停掉正在运行的进程。
        last = dict(getattr(agent_proc, "last_launch", None) or {})
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        merged = {**last, **{k: data[k] for k in ("rows", "cols") if k in data}}
        rows, cols, error = terminal_size(merged)
        if error:
            return jsonify({"ok": False, "error": error}), 400
        launch = build_launch_for_active(last.get("session_mode", "new"))
        if not launch.get("ready"):
            return jsonify({"ok": False, "error": launch["error"]}), 400
        agent_proc.stop()
        time.sleep(0.3)
        ok, msg = agent_proc.start(
            launch["env"], launch["label"],
            rows=rows, cols=cols,
            cwd=last.get("cwd"),
            command=launch["command"], clear_env=launch["clear_env"],
            client=launch["client"],
        )
        return jsonify({"ok": ok, "message": msg, "status": agent_proc.status()})

    @app.post("/api/agent/resize")
    @app.post("/api/claude/resize")
    def api_agent_resize():
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        rows, cols, error = terminal_size(data)
        if error:
            return jsonify({"ok": False, "error": error}), 400
        agent_proc.set_winsize(rows, cols)
        return jsonify({"ok": True})

    @app.post("/api/agent/keepalive")
    @app.post("/api/claude/keepalive")
    def api_agent_keepalive():
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        enabled = bool(data.get("enabled"))
        agent_proc.keepalive = enabled
        if enabled:
            agent_proc._fast_fail = 0  # 重新开启时清空熔断计数
        return jsonify({"ok": True, "keepalive": enabled})

    @app.post("/api/agent/stop")
    @app.post("/api/claude/stop")
    def api_agent_stop():
        ok, msg = agent_proc.stop()
        return jsonify({"ok": ok, "message": msg, "status": agent_proc.status()})

    @app.post("/api/agent/input")
    @app.post("/api/claude/input")
    def api_agent_input():
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        if "raw" in data:
            # xterm 原始按键流（含方向键/控制字符/转义序列），原样透传
            text = data["raw"]
        else:
            text = data.get("text", "")
            if not isinstance(text, str):
                return jsonify({"ok": False, "error": "输入必须是字符串"}), 400
            if not text.endswith("\n"):
                text += "\n"
        if not isinstance(text, str):
            return jsonify({"ok": False, "error": "输入必须是字符串"}), 400
        ok, msg = agent_proc.send_input(text)
        return jsonify({"ok": ok, "message": msg})

    @app.get("/api/agent/status")
    @app.get("/api/claude/status")
    def api_agent_status():
        return jsonify(agent_proc.status())

    @app.get("/api/fs/list")
    def api_fs_list():
        """列出某目录下的子目录，供前端目录选择器使用。"""
        raw = request.args.get("path", "~")
        path = Path(os.path.expanduser(raw or "~")).resolve()
        if not path.is_dir():
            path = Path.home()
        try:
            dirs = sorted(
                [p.name for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")],
                key=str.lower,
            )
        except PermissionError:
            dirs = []
        return jsonify({
            "path": str(path),
            "parent": str(path.parent) if path != path.parent else None,
            "dirs": dirs,
            "home": str(Path.home()),
        })

    @app.get("/api/agent/stream")
    @app.get("/api/claude/stream")
    def api_agent_stream():
        def gen():
            q = agent_proc.subscribe()
            try:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        chunk = q.get(timeout=15)
                        payload = json.dumps({"data": chunk})
                        yield f"data: {payload}\n\n"
                    except Exception:
                        yield ": keepalive\n\n"  # 心跳，保持连接
            finally:
                agent_proc.unsubscribe(q)

        return Response(gen(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        })

    # 挂到 app 上供入口清理；保留旧属性名兼容现有集成。
    app._agent_proc = agent_proc
    app._claude_proc = agent_proc
    app._cli_manager = cli_manager
    return app
