"""
Flask 应用工厂 + 全部路由。
"""

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from .config import (
    AUTH_VARS,
    build_env_for_active,
    get_lock,
    load_config,
    public_state,
    save_config,
)
from .process import ClaudeProcess

# --------------------------------------------------------------------------- #
# 应用工厂
# --------------------------------------------------------------------------- #

# 包内 static 目录（index.html 所在）
_PKG_DIR = Path(__file__).resolve().parent


def create_app():
    app = Flask(__name__, static_folder=None)
    claude_proc = ClaudeProcess()

    # ------------------------------------------------------------------- #
    # 路由
    # ------------------------------------------------------------------- #

    @app.route("/")
    def index():
        return send_from_directory(_PKG_DIR, "index.html")

    @app.get("/api/state")
    def api_state():
        state = public_state()
        state["claude_status"] = claude_proc.status()
        return jsonify(state)

    @app.post("/api/provider/switch")
    def api_switch_provider():
        data = request.get_json(force=True)
        pid = data.get("provider")
        with get_lock():
            cfg = load_config()
            if pid not in cfg["providers"]:
                return jsonify({"ok": False, "error": "未知供应商"}), 400
            cfg["current_provider"] = pid
            save_config(cfg)

        # 若系统存在 ccm，则调用其切换命令（不强制依赖）
        ccm_output = None
        if shutil.which("ccm"):
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

    @app.post("/api/claude/start")
    def api_claude_start():
        data = request.get_json(silent=True) or {}
        args = data.get("args")
        if isinstance(args, str):
            args = args.split()
        rows = int(data.get("rows") or 24)
        cols = int(data.get("cols") or 80)
        cwd = (data.get("cwd") or "").strip() or None

        env, label, has_key = build_env_for_active()
        if not has_key:
            return jsonify(
                {"ok": False, "error": f"当前供应商({label})未配置可用 API Key"}
            ), 400
        # 记住本次启动参数，供「重启」复用
        claude_proc.last_launch = {"args": args, "rows": rows, "cols": cols, "cwd": cwd}
        ok, msg = claude_proc.start(env, label, args=args, rows=rows, cols=cols, cwd=cwd)
        return jsonify({"ok": ok, "message": msg, "status": claude_proc.status()})

    @app.post("/api/claude/restart")
    def api_claude_restart():
        data = request.get_json(silent=True) or {}
        claude_proc.stop()
        time.sleep(0.3)
        env, label, has_key = build_env_for_active()
        if not has_key:
            return jsonify(
                {"ok": False, "error": f"当前供应商({label})未配置可用 API Key"}
            ), 400
        # 复用上次启动参数；允许前端用新的终端尺寸覆盖
        last = dict(getattr(claude_proc, "last_launch", None) or {})
        if data.get("rows"):
            last["rows"] = int(data["rows"])
        if data.get("cols"):
            last["cols"] = int(data["cols"])
        ok, msg = claude_proc.start(
            env, label,
            args=last.get("args"),
            rows=last.get("rows", 24),
            cols=last.get("cols", 80),
            cwd=last.get("cwd"),
        )
        return jsonify({"ok": ok, "message": msg, "status": claude_proc.status()})

    @app.post("/api/claude/resize")
    def api_claude_resize():
        data = request.get_json(force=True)
        claude_proc.set_winsize(data.get("rows", 24), data.get("cols", 80))
        return jsonify({"ok": True})

    @app.post("/api/claude/keepalive")
    def api_claude_keepalive():
        data = request.get_json(force=True)
        enabled = bool(data.get("enabled"))
        claude_proc.keepalive = enabled
        if enabled:
            claude_proc._fast_fail = 0  # 重新开启时清空熔断计数
        return jsonify({"ok": True, "keepalive": enabled})

    @app.post("/api/claude/stop")
    def api_claude_stop():
        ok, msg = claude_proc.stop()
        return jsonify({"ok": ok, "message": msg, "status": claude_proc.status()})

    @app.post("/api/claude/input")
    def api_claude_input():
        data = request.get_json(force=True)
        if "raw" in data:
            # xterm 原始按键流（含方向键/控制字符/转义序列），原样透传
            text = data["raw"]
        else:
            text = data.get("text", "")
            if not text.endswith("\n"):
                text += "\n"
        ok, msg = claude_proc.send_input(text)
        return jsonify({"ok": ok, "message": msg})

    @app.get("/api/claude/status")
    def api_claude_status():
        return jsonify(claude_proc.status())

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

    @app.get("/api/claude/stream")
    def api_claude_stream():
        def gen():
            q = claude_proc.subscribe()
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
                claude_proc.unsubscribe(q)

        return Response(gen(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        })

    # 把 claude_proc 挂到 app 上，供 app.py 清理用
    app._claude_proc = claude_proc
    return app
