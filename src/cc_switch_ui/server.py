"""
Flask 应用工厂 + 全部路由。
"""

import csv
import hmac
import importlib.metadata
import io
import ipaddress
import json
import logging
import math
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, send_from_directory

from . import config
from .cli_manager import CliManager, CliManagerError
from .config import (
    AUTH_VARS,
    CODEX_AUTH_VAR,
    _load_trash,
    _normalize_provider,
    active_account_of,
    build_launch_for_active,
    build_launch_for_provider,
    get_lock,
    launch_fingerprint_for_active,
    launch_fingerprint_for_provider,
    load_config,
    mask_key,
    move_account_to_trash,
    public_state,
    restore_account,
    save_config,
)
from .process import SessionRegistry

# --------------------------------------------------------------------------- #
# 应用工厂
# --------------------------------------------------------------------------- #

# 包内 static 目录（index.html 所在）
_PKG_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _PKG_DIR / "static"

_logger = logging.getLogger("cc_switch_ui.server")

try:
    _VERSION = importlib.metadata.version("cc-switch-ui")
except importlib.metadata.PackageNotFoundError:
    _VERSION = "dev"
_STARTED_AT = time.time()

# 审计日志：超过 10MB 轮转 .1→.2→.3，最多保留 3 份
_AUDIT_FILE = "audit.jsonl"
_AUDIT_MAX_BYTES = 10 * 1024 * 1024

# 只读模式白名单：团队查看者可以操作终端，但改不了供应商/账号/CLI/配置
_TERMINAL_ACTIONS = ("start", "stop", "restart", "input", "resize", "keepalive")


def _err(code, message, status):
    """统一错误响应：{ok:false, code, error} + HTTP 状态码。"""
    return jsonify({"ok": False, "code": code, "error": message}), status


def _audit(action, target="", result="ok"):
    """追加一行结构化审计记录（仅在请求上下文中调用，失败不影响主流程）。"""
    try:
        state_dir = config.STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / _AUDIT_FILE
        if path.exists() and path.stat().st_size > _AUDIT_MAX_BYTES:
            for i in (2, 1):
                older = path.with_name(f"{_AUDIT_FILE}.{i}")
                if older.exists():
                    os.replace(older, path.with_name(f"{_AUDIT_FILE}.{i + 1}"))
            os.replace(path, path.with_name(f"{_AUDIT_FILE}.1"))
        line = json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "remote_addr": request.remote_addr,
                "origin": request.headers.get("Origin", ""),
                "action": action,
                "target": target,
                "result": result,
            },
            ensure_ascii=False,
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 —— 审计失败不能破坏主流程
        pass


def _terminal_path_allowed(path):
    """只读模式下允许写操作的终端路径（会话前缀在 3.3 落地后生效）。"""
    for prefix in ("/api/agent/", "/api/claude/"):
        if path.startswith(prefix):
            return path[len(prefix):] in _TERMINAL_ACTIONS
    if path.startswith("/api/sessions/"):
        parts = path[len("/api/sessions/"):].split("/", 1)
        return len(parts) == 2 and parts[1] in _TERMINAL_ACTIONS
    return False


def _session_log_path(sid):
    """会话 pty 输出日志路径（按启动时刻命名）。"""
    logs_dir = config.STATE_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / f"{sid}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.log"


def _trash_days_left(deleted_at):
    """回收站条目剩余可恢复天数（7 天窗口）。"""
    try:
        deleted = datetime.fromisoformat(deleted_at)
        if deleted.tzinfo is None:
            deleted = deleted.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - deleted).total_seconds() / 86400
        return max(0, -int(-math.ceil(7 - elapsed)))
    except (TypeError, ValueError):
        return 0


def create_app(
    *,
    allow_cli_management=False,
    cli_manager=None,
    auth_token=None,
    trust_proxy_loopback=False,
    read_only=False,
    fs_root=None,
    max_sessions=8,
):
    app = Flask(__name__, static_folder=None)
    registry = SessionRegistry(max_sessions)
    agent_proc = registry.get_or_create_default()
    cli_manager = cli_manager or CliManager()
    fs_root_resolved = (
        Path(fs_root).expanduser().resolve() if fs_root else Path.home().resolve()
    )

    # ------------------------------------------------------------------- #
    # 请求前置检查：时间戳 → token 鉴权 → Origin 校验 → 只读模式
    # ------------------------------------------------------------------- #

    @app.before_request
    def _stamp_request():
        request._ccm_t0 = time.monotonic()

    @app.before_request
    def _check_auth():
        if not auth_token:
            return None
        provided = None
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            provided = header[len("Bearer "):]
        if provided is None:
            provided = request.args.get("token")
        if (
            provided is None
            or not hmac.compare_digest(
                provided.encode("utf-8"), auth_token.encode("utf-8")
            )
        ):
            _audit("unauthorized", result="unauthenticated")
            return _err("unauthenticated", "token 缺失或无效", 401)
        return None

    @app.before_request
    def _check_origin():
        if request.method in ("POST", "PUT", "DELETE"):
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.headers.get("Host"):
                _audit("unauthorized", result="origin_mismatch")
                return _err("origin_mismatch", "Origin 与目标不匹配", 403)
        return None

    @app.before_request
    def _check_read_only():
        if read_only and request.method != "GET" and not _terminal_path_allowed(request.path):
            return _err("read_only", "只读模式下该操作被禁止", 403)
        return None

    @app.after_request
    def _log_request(response):
        t0 = getattr(request, "_ccm_t0", None)
        dur_ms = round((time.monotonic() - t0) * 1000, 1) if t0 is not None else None
        _logger.info(
            "request",
            extra={
                "extra_fields": {
                    "method": request.method,
                    "path": request.path,
                    "status": response.status_code,
                    "dur_ms": dur_ms,
                    "remote": request.remote_addr,
                }
            },
        )
        return response

    # ------------------------------------------------------------------- #
    # 工具
    # ------------------------------------------------------------------- #

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
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            # 反代后「直连回环」不可信：不信任代理时一律视为非回环，
            # 只有显式 --trust-proxy-loopback 才解析 XFF 首段。
            if not trust_proxy_loopback:
                return False
            first = xff.split(",")[0].strip()
            try:
                return ipaddress.ip_address(first).is_loopback
            except ValueError:
                return False
        try:
            return ipaddress.ip_address(request.remote_addr or "").is_loopback
        except ValueError:
            return False

    def launch_snapshot(launch, mode, cwd):
        """Build the non-secret description shown as the running Agent state."""
        return {
            "provider_id": launch.get("provider_id"),
            "provider_label": launch.get("provider_label", launch.get("label", "?")),
            "client": launch.get("client", "claude"),
            "base_url": launch.get("base_url", ""),
            "model": launch.get("model", ""),
            "account_id": launch.get("account_id"),
            "account_name": launch.get("account_name", ""),
            "session_mode": mode,
            "cwd": str(Path(cwd or os.getcwd()).expanduser().resolve()),
        }

    def restart_needed(
        status, selected_launch, selected_fingerprint=None, running_fingerprint=None
    ):
        """Whether the running process differs from the selected launch config."""
        if not status.get("running"):
            return False
        if selected_fingerprint is not None and running_fingerprint is not None:
            return selected_fingerprint != running_fingerprint
        running_launch = status.get("launch") or {}
        if not running_launch:
            return False
        fields = ("provider_id", "client", "base_url", "model", "account_id")
        return any(
            (running_launch.get(field) or "") != (selected_launch.get(field) or "")
            for field in fields
        )

    def current_state_and_fingerprint():
        """Read the selected state and its identity from one config snapshot."""
        with get_lock():
            cfg = load_config()
            state = public_state(cfg=cfg)
            fingerprint = launch_fingerprint_for_active(cfg=cfg)
        return state, fingerprint

    def current_launch_and_fingerprint(mode):
        """Build a launch and identity from one config snapshot."""
        with get_lock():
            cfg = load_config()
            launch = build_launch_for_active(mode, cfg=cfg)
            fingerprint = launch_fingerprint_for_active(cfg=cfg)
        return launch, fingerprint

    def _find_session(cfg, sid):
        return next(
            (
                s for s in cfg.get("sessions", [])
                if isinstance(s, dict) and s.get("id") == sid
            ),
            None,
        )

    def _find_profile(cfg, profile_id):
        return next(
            (
                p for p in cfg.get("profiles", [])
                if isinstance(p, dict) and p.get("id") == profile_id
            ),
            None,
        )

    def _ensure_default_session():
        """legacy 首次启动后把 default 会话条目补写进 config sessions（纯 additive）。"""
        with get_lock():
            cfg = load_config()
            if any(
                isinstance(s, dict) and s.get("id") == SessionRegistry.DEFAULT_ID
                for s in cfg.get("sessions", [])
            ):
                return
            cfg.setdefault("sessions", []).append({
                "id": SessionRegistry.DEFAULT_ID,
                "name": "默认会话",
                "cwd": None,
                "provider_id": cfg["current_provider"],
                "session_mode": "new",
                "account_id": None,
            })
            save_config(cfg)

    def _make_rebuild_for(sid):
        """看门狗重建钩子：按该会话的 provider/account（或 legacy 全局选择）重建 launch。"""
        def _rebuild():
            with get_lock():
                cfg = load_config()
                if sid == SessionRegistry.DEFAULT_ID:
                    proc = registry.get(sid)
                    mode = (
                        (proc.last_launch or {}).get("session_mode", "new")
                        if proc is not None else "new"
                    )
                    provider_id = cfg["current_provider"]
                    account_id = None
                else:
                    session = _find_session(cfg, sid)
                    if session is None:
                        return None
                    provider_id = session.get("provider_id") or cfg["current_provider"]
                    mode = session.get("session_mode") or "new"
                    account_id = session.get("account_id")
                if provider_id not in cfg["providers"]:
                    return None
                launch = build_launch_for_provider(
                    provider_id, mode, account_id, cfg=cfg
                )
                fingerprint = launch_fingerprint_for_provider(
                    provider_id, account_id, cfg=cfg
                )
            return (launch, fingerprint) if launch.get("ready") else None
        return _rebuild

    def _session_item(cfg, s, proc):
        """单个会话的非密快照（GET /api/sessions 与创建响应共用）。"""
        sid = s.get("id")
        status = proc.status() if proc is not None else {}
        running = bool(status.get("running"))
        provider_id = s.get("provider_id") or cfg["current_provider"]
        provider = cfg["providers"].get(provider_id, {})
        return {
            "id": sid,
            "name": s.get("name") or sid,
            "cwd": s.get("cwd"),
            "provider_id": provider_id,
            "provider_label": provider.get("label", provider_id),
            "session_mode": s.get("session_mode") or "new",
            "account_id": s.get("account_id"),
            "running": running,
            "pid": status.get("pid"),
            "uptime": status.get("uptime", 0),
            "client": status.get("client"),
            "restart_required": bool(
                running
                and proc is not None
                and launch_fingerprint_for_provider(
                    provider_id, s.get("account_id"), cfg=cfg
                ) != getattr(proc, "_launch_signature", None)
            ),
            "idle_seconds": status.get("idle_seconds"),
            "log_path": status.get("log_path"),
            "launch": status.get("launch"),
        }


    def _do_switch_restart(proc, provider_id, mode, rows, cols, cwd, *, sid=None):
        """「切换并重启」核心：锁内校验 provider + 构建 launch + readiness
        （未就绪返回 400，不触碰进程、不持久化选择），就绪则 stop → start →
        更新 last_launch。返回 (result, error_response)，二者恰有一个非 None。

        sid=None 走 legacy：更新全局 current_provider；sid 为会话 id：更新该
        会话的 provider_id（不动全局选择），并按会话绑定的 account 构建 launch。
        """
        with get_lock():
            cfg = load_config()
            if provider_id not in cfg["providers"]:
                return None, _err("unknown_provider", "未知供应商", 400)
            account_id = None
            if sid is None:
                cfg["current_provider"] = provider_id
            else:
                session = next(
                    (s for s in cfg.get("sessions", []) if s.get("id") == sid),
                    None,
                )
                if session is None:
                    return None, _err("unknown_session", "会话不存在", 404)
                session["provider_id"] = provider_id
                account_id = session.get("account_id")
            launch = build_launch_for_provider(provider_id, mode, account_id, cfg=cfg)
            fingerprint = launch_fingerprint_for_provider(provider_id, account_id, cfg=cfg)
            if not launch.get("ready"):
                return None, _err(
                    "not_ready", launch.get("error", "当前供应商未就绪"), 400
                )
            save_config(cfg)
        log_sid = sid or "default"
        snapshot = launch_snapshot(launch, mode, cwd)
        if proc.status().get("running"):
            proc.stop()
        time.sleep(0.3)
        ok, msg = proc.start(
            launch["env"], launch["label"],
            rows=rows, cols=cols, cwd=cwd,
            command=launch["command"], clear_env=launch["clear_env"],
            client=launch["client"], launch_snapshot=snapshot,
            launch_signature=fingerprint,
            log_path=_session_log_path(log_sid),
        )
        if ok:
            last = dict(getattr(proc, "last_launch", None) or {})
            proc.last_launch = {
                **last,
                "rows": rows,
                "cols": cols,
                "cwd": cwd,
                "session_mode": mode,
                "launch": snapshot,
            }
            if sid is None:
                _ensure_default_session()
        _audit(
            "agent_switch_restart",
            target=sid if sid is not None else provider_id,
            result="ok" if ok else "error",
        )
        return (ok, msg, proc.status()), None

    # ------------------------------------------------------------------- #
    # 路由
    # ------------------------------------------------------------------- #

    @app.route("/")
    def index():
        return send_from_directory(_PKG_DIR, "index.html")

    @app.get("/static/<path:name>")
    def static_asset(name):
        """随包静态资源（xterm 等，去 CDN 化）。"""
        return send_from_directory(_STATIC_DIR, name)

    @app.get("/healthz")
    def healthz():
        return jsonify({
            "ok": True,
            "version": _VERSION,
            "agent_running": registry.get_or_create_default().is_running(),
            "uptime": round(time.time() - _STARTED_AT, 1),
            "session_count": len(registry),
        })

    @app.get("/api/state")
    def api_state():
        agent_proc = registry.get_or_create_default()
        state, selected_fingerprint = current_state_and_fingerprint()
        agent_status = agent_proc.status()
        state["restart_required"] = restart_needed(
            agent_status,
            state["selected_launch"],
            selected_fingerprint,
            getattr(agent_proc, "_launch_signature", None),
        )
        state["agent_status"] = agent_status
        state["claude_status"] = state["agent_status"]  # backward compatibility
        return jsonify(state)

    @app.get("/api/cli/status")
    def api_cli_status():
        state = cli_manager.status()
        state["management_enabled"] = bool(allow_cli_management)
        return jsonify(state)

    @app.post("/api/cli/check")
    def api_cli_check():
        if not request_is_loopback():
            return _err("forbidden", "CLI 安装/更新只接受回环地址请求；请使用 SSH 隧道", 403)
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        _audit("cli_check", target=f"{data.get('agent', '')}:{data.get('action', '')}")
        try:
            result = cli_manager.latest_versions(data.get("registry", "official"))
        except CliManagerError as exc:
            return _err("validation", str(exc), 400)
        return jsonify({"ok": True, **result})

    @app.post("/api/cli/manage")
    def api_cli_manage():
        agent_proc = registry.get_or_create_default()
        if not request_is_loopback():
            return _err("forbidden", "CLI 安装/更新只接受回环地址请求；请使用 SSH 隧道", 403)
        if not allow_cli_management:
            return _err("cli_disabled", "CLI 管理未启用；请用 --allow-cli-management 重启面板", 403)
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        if agent_proc.status().get("running"):
            return _err("conflict", "请先停止正在运行的 Agent，再安装或更新 CLI", 409)
        expected = f'{data.get("action", "")}:{data.get("agent", "")}'
        if data.get("confirm") != expected:
            return _err("confirm_mismatch", "缺少明确操作确认", 400)
        target = f"{data.get('agent', '')}:{data.get('action', '')}"
        try:
            result = cli_manager.manage(
                agent=data.get("agent", ""),
                action=data.get("action", ""),
                version=data.get("version", "latest"),
                registry=data.get("registry", "official"),
            )
        except CliManagerError as exc:
            _audit("cli_manage", target=target, result="error")
            return _err("validation", str(exc), 400)
        _audit("cli_manage", target=target)
        return jsonify(result)

    @app.post("/api/provider/switch")
    def api_switch_provider():
        agent_proc = registry.get_or_create_default()
        data = request.get_json(force=True)
        pid = data.get("provider")
        with get_lock():
            cfg = load_config()
            if pid not in cfg["providers"]:
                return _err("unknown_provider", "未知供应商", 400)
            cfg["current_provider"] = pid
            client = cfg["providers"][pid].get("client", "claude")
            save_config(cfg)
        _audit("provider_switch", target=pid)

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

        status = agent_proc.status()
        state, selected_fingerprint = current_state_and_fingerprint()
        selected_launch = state["selected_launch"]
        return jsonify({
            "ok": True,
            "current_provider": pid,
            "ccm_output": ccm_output,
            "restart_required": restart_needed(
                status,
                selected_launch,
                selected_fingerprint,
                getattr(agent_proc, "_launch_signature", None),
            ),
        })

    @app.put("/api/provider/<pid>")
    def api_edit_provider(pid):
        """编辑供应商端点：base_url / model / 鉴权变量 / 显示名。"""
        data = request.get_json(force=True)
        with get_lock():
            cfg = load_config()
            p = cfg["providers"].get(pid)
            if not p:
                return _err("unknown_provider", "未知供应商", 400)
            for field in ("label", "base_url", "model"):
                if field in data and data[field] is not None:
                    p[field] = str(data[field]).strip()
            if "auth_var" in data:
                auth_var = data["auth_var"]
                allowed_auth_vars = (
                    (CODEX_AUTH_VAR,)
                    if p.get("client") == "codex"
                    else AUTH_VARS
                )
                if auth_var not in allowed_auth_vars:
                    return _err("validation", "不支持的鉴权环境变量", 400)
                p["auth_var"] = auth_var
            if not p.get("label"):
                p["label"] = pid
            save_config(cfg)
        _audit("provider_edit", target=pid)
        return jsonify({"ok": True})

    @app.post("/api/provider/<pid>/test")
    def api_test_provider(pid):
        """供应商连接测试：向固定端点发 1 token 探测请求并分类结果。"""
        with get_lock():
            cfg = load_config()
            provider = cfg["providers"].get(pid)
        if not provider:
            return _err("unknown_provider", "未知供应商", 400)
        client = provider.get("client", "claude")
        base_url = (provider.get("base_url") or "").strip()
        model = (provider.get("model") or "").strip()
        account = active_account_of(provider)
        api_key = (account.get("api_key") if account else "") or ""
        if not api_key:
            if pid == "claude":
                _audit("provider_test", target=pid, result="claude_login")
                return jsonify({
                    "ok": True,
                    "result": "claude_login",
                    "note": "Claude 登录模式无需 key 测试",
                })
            return _err("not_ready", "未配置可用 API Key", 400)
        headers = {"Content-Type": "application/json"}
        if client == "codex":
            url = f"{base_url.rstrip('/')}/responses"
            headers["Authorization"] = f"Bearer {api_key}"
            body = {"model": model, "input": "ping", "max_output_tokens": 1}
        else:
            if not model:
                if pid != "claude":
                    return _err("not_ready", "未配置模型，无法测试", 400)
                model = "claude-3-5-haiku-20241022"
            url = f"{base_url.rstrip('/')}/v1/messages"
            if provider.get("auth_var", "ANTHROPIC_API_KEY") == "ANTHROPIC_API_KEY":
                headers["x-api-key"] = api_key
            else:
                headers["Authorization"] = f"Bearer {api_key}"
            body = {
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status_code = resp.status
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                _audit("provider_test", target=pid, result="auth_failed")
                return jsonify({"ok": False, "result": "auth_failed", "status": e.code})
            _audit("provider_test", target=pid, result="other")
            return jsonify({"ok": False, "result": "other", "status": e.code})
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            _audit("provider_test", target=pid, result="network")
            return jsonify({"ok": False, "result": "network", "error": str(e)})
        _audit("provider_test", target=pid)
        return jsonify({"ok": True, "result": "ok", "status": status_code})

    @app.post("/api/account")
    def api_add_account():
        data = request.get_json(force=True)
        pid = data.get("provider")
        name = (data.get("name") or "").strip() or "未命名账号"
        api_key = (data.get("api_key") or "").strip()
        with get_lock():
            cfg = load_config()
            if pid not in cfg["providers"]:
                return _err("unknown_provider", "未知供应商", 400)
            acc = {"id": uuid.uuid4().hex[:12], "name": name, "api_key": api_key}
            cfg["providers"][pid]["accounts"].append(acc)
            if not cfg["providers"][pid].get("active_account"):
                cfg["providers"][pid]["active_account"] = acc["id"]
            save_config(cfg)
        _audit("account_add", target=f"{pid}:{acc['id']}")
        return jsonify({"ok": True, "id": acc["id"]})

    @app.put("/api/account/<aid>")
    def api_edit_account(aid):
        data = request.get_json(force=True)
        pid = data.get("provider")
        with get_lock():
            cfg = load_config()
            provider = cfg["providers"].get(pid)
            if not provider:
                return _err("unknown_provider", "未知供应商", 400)
            for acc in provider["accounts"]:
                if acc["id"] == aid:
                    if "name" in data:
                        acc["name"] = (data.get("name") or "").strip() or acc["name"]
                    # 仅在传入非空 key 时更新，避免脱敏回传覆盖真实值
                    if data.get("api_key"):
                        acc["api_key"] = data["api_key"].strip()
                    save_config(cfg)
                    break
            else:
                return _err("unknown_account", "账号不存在", 404)
        _audit("account_edit", target=f"{pid}:{aid}")
        return jsonify({"ok": True})

    @app.delete("/api/account/<aid>")
    def api_delete_account(aid):
        """删除账号：软删除进回收站（7 天可恢复），响应契约不变。"""
        pid = request.args.get("provider")
        with get_lock():
            cfg = load_config()
            provider = cfg["providers"].get(pid)
            if not provider:
                return _err("unknown_provider", "未知供应商", 400)
            account = next(
                (a for a in provider.get("accounts", []) if a.get("id") == aid), None
            )
            if account is None:
                return _err("unknown_account", "账号不存在", 404)
            provider["accounts"] = [a for a in provider["accounts"] if a["id"] != aid]
            if provider.get("active_account") == aid:
                provider["active_account"] = (
                    provider["accounts"][0]["id"] if provider["accounts"] else None
                )
            save_config(cfg)
            move_account_to_trash(pid, account)
        _audit("account_delete", target=f"{pid}:{aid}")
        return jsonify({"ok": True})

    @app.get("/api/account/trash")
    def api_account_trash():
        """回收站列表（key 永不返回）。"""
        provider = request.args.get("provider")
        items = []
        for entry in _load_trash():
            if provider and entry.get("provider") != provider:
                continue
            account = entry.get("account") or {}
            deleted_at = entry.get("deleted_at", "")
            items.append({
                "provider": entry.get("provider", ""),
                "id": account.get("id", ""),
                "name": account.get("name", ""),
                "deleted_at": deleted_at,
                "days_left": _trash_days_left(deleted_at),
            })
        return jsonify({"ok": True, "items": items})

    @app.post("/api/account/<aid>/restore")
    def api_restore_account(aid):
        pid = request.args.get("provider")
        with get_lock():
            cfg = load_config()
            provider = cfg["providers"].get(pid)
            if not provider:
                return _err("unknown_provider", "未知供应商", 400)
            account, _error = restore_account(pid, aid)
            if account is None:
                return _err("unknown_account", "回收站中没有该账号", 404)
            if not any(a.get("id") == aid for a in provider.get("accounts", [])):
                provider["accounts"].append(account)
                if not provider.get("active_account"):
                    provider["active_account"] = aid
                save_config(cfg)
        _audit("account_restore", target=f"{pid}:{aid}")
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
                return _err("unknown_provider", "未知供应商", 400)
            if aid not in [a["id"] for a in provider["accounts"]]:
                return _err("unknown_account", "账号不存在", 404)
            provider["active_account"] = aid
            save_config(cfg)
        _audit("account_activate", target=f"{pid}:{aid}")
        return jsonify({"ok": True})

    # ------------------------------------------------------------------- #
    # 多会话（3.3）
    # ------------------------------------------------------------------- #

    @app.get("/api/sessions")
    def api_list_sessions():
        with get_lock():
            cfg = load_config()
            items = [
                _session_item(cfg, s, registry.get(s.get("id")))
                for s in cfg.get("sessions", [])
                if isinstance(s, dict) and s.get("id")
            ]
        return jsonify({"ok": True, "sessions": items})

    @app.post("/api/sessions")
    def api_create_session():
        data = request.get_json(silent=True)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        raw_name = data.get("name")
        if raw_name is not None and not isinstance(raw_name, str):
            return _err("validation", "会话名称必须是字符串", 400)
        name = (raw_name or "").strip() or None
        raw_cwd = data.get("cwd")
        if raw_cwd is not None and not isinstance(raw_cwd, str):
            return _err("validation", "工作目录必须是字符串", 400)
        cwd = (raw_cwd or "").strip() or None
        provider_id = data.get("provider_id")
        if provider_id is not None and not isinstance(provider_id, str):
            return _err("validation", "供应商必须是字符串", 400)
        raw_mode = data.get("session_mode")
        if raw_mode is not None and raw_mode not in ("new", "continue", "resume"):
            return _err("validation", "会话模式必须是 new/continue/resume", 400)
        account_id = data.get("account_id")
        if account_id is not None and not isinstance(account_id, str):
            return _err("validation", "账号 ID 必须是字符串", 400)
        raw_profile = data.get("profile_id")
        if raw_profile is None:
            raw_profile = data.get("profile")
        if raw_profile is not None and not isinstance(raw_profile, str):
            return _err("validation", "档案 ID 必须是字符串", 400)
        with get_lock():
            cfg = load_config()
            profile = None
            if raw_profile is not None:
                profile = _find_profile(cfg, raw_profile)
                if profile is None:
                    return _err("unknown_profile", "档案不存在", 404)
            if provider_id is None:
                provider_id = (profile or {}).get("provider_id") or cfg["current_provider"]
            if cwd is None:
                cwd = (profile or {}).get("cwd")
            if account_id is None:
                account_id = (profile or {}).get("account_id")
            if raw_mode is None:
                session_mode = (profile or {}).get("session_mode") or "new"
            else:
                session_mode = raw_mode
            if provider_id not in cfg["providers"]:
                return _err("unknown_provider", "未知供应商", 400)
            if account_id is not None and not any(
                isinstance(a, dict) and a.get("id") == account_id
                for a in cfg["providers"][provider_id].get("accounts", [])
            ):
                return _err("unknown_account", "账号不存在或不属于该供应商", 404)
            sid = uuid.uuid4().hex[:8]
            try:
                proc = registry.create(sid)
            except ValueError:
                _audit("session_create", result="max_sessions")
                return _err("max_sessions", "会话数已达上限", 409)
            entry = {
                "id": sid,
                "name": name,
                "cwd": cwd,
                "provider_id": provider_id,
                "session_mode": session_mode,
                "account_id": account_id,
            }
            cfg.setdefault("sessions", []).append(entry)
            save_config(cfg)
        proc.set_rebuild_launch(_make_rebuild_for(sid))
        _audit("session_create", target=sid)
        return jsonify({"ok": True, "session": _session_item(cfg, entry, proc)})

    @app.patch("/api/sessions/<sid>")
    def api_update_session(sid):
        data = request.get_json(silent=True)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        for field in ("name", "cwd", "provider_id"):
            value = data.get(field)
            if value is not None and not isinstance(value, str):
                return _err("validation", f"{field} 必须是字符串", 400)
        account_id = data.get("account_id")
        if account_id is not None and not isinstance(account_id, str):
            return _err("validation", "账号 ID 必须是字符串", 400)
        session_mode = data.get("session_mode")
        if session_mode is not None and session_mode not in ("new", "continue", "resume"):
            return _err("validation", "会话模式必须是 new/continue/resume", 400)
        with get_lock():
            cfg = load_config()
            s = _find_session(cfg, sid)
            if s is None:
                return _err("unknown_session", "会话不存在", 404)
            if data.get("provider_id") is not None and data["provider_id"] not in cfg["providers"]:
                return _err("unknown_provider", "未知供应商", 400)
            if account_id is not None:
                target_pid = (
                    data.get("provider_id")
                    or s.get("provider_id")
                    or cfg["current_provider"]
                )
                if not any(
                    isinstance(a, dict) and a.get("id") == account_id
                    for a in cfg["providers"].get(target_pid, {}).get("accounts", [])
                ):
                    return _err("unknown_account", "账号不存在或不属于该供应商", 404)
            if "name" in data:
                s["name"] = (data.get("name") or "").strip() or None
            if "cwd" in data:
                s["cwd"] = (data.get("cwd") or "").strip() or None
            if data.get("provider_id") is not None:
                s["provider_id"] = data["provider_id"]
            if session_mode is not None:
                s["session_mode"] = session_mode
            if "account_id" in data:
                s["account_id"] = account_id
            save_config(cfg)
            item = _session_item(cfg, s, registry.get(sid))
        _audit("session_edit", target=sid)
        return jsonify({"ok": True, "session": item})

    @app.delete("/api/sessions/<sid>")
    def api_delete_session(sid):
        with get_lock():
            cfg = load_config()
            s = _find_session(cfg, sid)
            if s is None:
                return _err("unknown_session", "会话不存在", 404)
            cfg["sessions"] = [x for x in cfg["sessions"] if x.get("id") != sid]
            save_config(cfg)
        proc = registry.remove(sid)  # stop() 子进程后丢弃
        _audit("session_delete", target=sid)
        return jsonify({"ok": True})

    def _session_proc(sid):
        """取会话进程；config 中存在但注册表已失（如面板重启）则重建注册。
        完全未知会话返回 (None, 错误响应)。"""
        proc = registry.get(sid)
        if proc is None:
            with get_lock():
                cfg = load_config()
                known = _find_session(cfg, sid) is not None
            if not known:
                return None, _err("unknown_session", "会话不存在", 404)
            try:
                proc = registry.create(sid)
            except ValueError:
                return None, _err("max_sessions", "会话数已达上限", 409)
            proc.set_rebuild_launch(_make_rebuild_for(sid))
        return proc, None

    @app.post("/api/sessions/<sid>/start")
    def api_session_start(sid):
        proc, error = _session_proc(sid)
        if error is not None:
            return error
        data = request.get_json(silent=True)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        rows, cols, size_error = terminal_size(data)
        if size_error:
            return _err("validation", size_error, 400)
        raw_cwd = data.get("cwd")
        if raw_cwd is not None and not isinstance(raw_cwd, str):
            return _err("validation", "工作目录必须是字符串", 400)
        mode_override = data.get("session_mode")
        if mode_override is not None and mode_override not in ("new", "continue", "resume"):
            return _err("validation", "会话模式必须是 new/continue/resume", 400)
        raw_profile = data.get("profile_id")
        if raw_profile is None:
            raw_profile = data.get("profile")
        if raw_profile is not None and not isinstance(raw_profile, str):
            return _err("validation", "档案 ID 必须是字符串", 400)
        if proc.is_running():
            _audit("agent_start", target=sid, result="already_running")
            return jsonify({
                "ok": False,
                "code": "already_running",
                "message": "该会话已在运行",
                "status": proc.status(),
            })
        with get_lock():
            cfg = load_config()
            s = _find_session(cfg, sid)
            if s is None:
                return _err("unknown_session", "会话不存在", 404)
            provider_id = s.get("provider_id") or cfg["current_provider"]
            # body 覆盖会话默认值（一次性，不持久化）
            mode = mode_override or s.get("session_mode") or "new"
            account_id = s.get("account_id")
            cwd = (raw_cwd or s.get("cwd") or "").strip() or None
            # 档案字段物化为一次性启动参数（显式 body 字段优先，不持久化）
            if raw_profile is not None:
                profile = _find_profile(cfg, raw_profile)
                if profile is None:
                    return _err("unknown_profile", "档案不存在", 404)
                provider_id = profile.get("provider_id") or provider_id
                if mode_override is None:
                    mode = profile.get("session_mode") or mode
                account_id = profile.get("account_id") or account_id
                if raw_cwd is None:
                    cwd = (profile.get("cwd") or "").strip() or cwd
            launch = build_launch_for_provider(provider_id, mode, account_id, cfg=cfg)
            fingerprint = launch_fingerprint_for_provider(provider_id, account_id, cfg=cfg)
        if not launch.get("ready"):
            return _err("not_ready", launch["error"], 400)
        snapshot = launch_snapshot(launch, mode, cwd)
        proc.last_launch = {
            "session_mode": mode, "rows": rows, "cols": cols, "cwd": cwd,
            "launch": snapshot,
        }
        ok, msg = proc.start(
            launch["env"], launch["label"], rows=rows, cols=cols, cwd=cwd,
            command=launch["command"], clear_env=launch["clear_env"],
            client=launch["client"], launch_snapshot=snapshot,
            launch_signature=fingerprint,
            log_path=_session_log_path(sid),
        )
        _audit("agent_start", target=sid, result="ok" if ok else "error")
        return jsonify({"ok": ok, "message": msg, "status": proc.status()})

    @app.post("/api/sessions/<sid>/stop")
    def api_session_stop(sid):
        proc, error = _session_proc(sid)
        if error is not None:
            return error
        ok, msg = proc.stop()
        body = {"ok": ok, "message": msg, "status": proc.status()}
        if not ok:
            body["code"] = "not_running"
        _audit("agent_stop", target=sid, result="ok" if ok else "not_running")
        return jsonify(body)

    @app.post("/api/sessions/<sid>/restart")
    def api_session_restart(sid):
        proc, error = _session_proc(sid)
        if error is not None:
            return error
        data = request.get_json(silent=True)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        last = dict(getattr(proc, "last_launch", None) or {})
        mode = (
            session_mode(data)
            if "session_mode" in data or "args" in data
            else last.get("session_mode", "new")
        )
        raw_cwd = data.get("cwd", last.get("cwd") or "")
        if not isinstance(raw_cwd, str):
            return _err("validation", "工作目录必须是字符串", 400)
        cwd = raw_cwd.strip() or None
        merged = {**last, **{k: data[k] for k in ("rows", "cols") if k in data}}
        rows, cols, size_error = terminal_size(merged)
        if size_error:
            return _err("validation", size_error, 400)
        with get_lock():
            cfg = load_config()
            s = _find_session(cfg, sid)
            if s is None:
                return _err("unknown_session", "会话不存在", 404)
            provider_id = s.get("provider_id") or cfg["current_provider"]
            account_id = s.get("account_id")
            launch = build_launch_for_provider(provider_id, mode, account_id, cfg=cfg)
            fingerprint = launch_fingerprint_for_provider(provider_id, account_id, cfg=cfg)
        if not launch.get("ready"):
            return _err("not_ready", launch["error"], 400)
        snapshot = launch_snapshot(launch, mode, cwd)
        proc.stop()
        time.sleep(0.3)
        ok, msg = proc.start(
            launch["env"], launch["label"],
            rows=rows, cols=cols,
            cwd=cwd,
            command=launch["command"], clear_env=launch["clear_env"],
            client=launch["client"], launch_snapshot=snapshot,
            launch_signature=fingerprint,
            log_path=_session_log_path(sid),
        )
        if ok:
            proc.last_launch = {
                **last,
                "rows": rows,
                "cols": cols,
                "cwd": cwd,
                "session_mode": mode,
                "launch": snapshot,
            }
        _audit("agent_restart", target=sid, result="ok" if ok else "error")
        return jsonify({"ok": ok, "message": msg, "status": proc.status()})

    @app.post("/api/sessions/<sid>/switch-restart")
    def api_session_switch_restart(sid):
        proc, error = _session_proc(sid)
        if error is not None:
            return error
        data = request.get_json(silent=True)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        provider_id = data.get("provider")
        if not provider_id:
            return _err("validation", "缺少 provider 参数", 400)
        last = dict(getattr(proc, "last_launch", None) or {})
        mode = (
            session_mode(data)
            if "session_mode" in data or "args" in data
            else last.get("session_mode", "new")
        )
        raw_cwd = data.get("cwd", last.get("cwd") or "")
        if not isinstance(raw_cwd, str):
            return _err("validation", "工作目录必须是字符串", 400)
        cwd = raw_cwd.strip() or None
        merged = {**last, **{k: data[k] for k in ("rows", "cols") if k in data}}
        rows, cols, size_error = terminal_size(merged)
        if size_error:
            return _err("validation", size_error, 400)
        result, error_response = _do_switch_restart(
            proc, provider_id, mode, rows, cols, cwd, sid=sid
        )
        if error_response is not None:
            return error_response
        ok, msg, status = result
        return jsonify({"ok": ok, "message": msg, "status": status})

    @app.post("/api/sessions/<sid>/input")
    def api_session_input(sid):
        proc, error = _session_proc(sid)
        if error is not None:
            return error
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        if "raw" in data:
            # xterm 原始按键流（含方向键/控制字符/转义序列），原样透传
            text = data["raw"]
        else:
            text = data.get("text", "")
            if not isinstance(text, str):
                return _err("validation", "输入必须是字符串", 400)
            if not text.endswith("\n"):
                text += "\n"
        if not isinstance(text, str):
            return _err("validation", "输入必须是字符串", 400)
        ok, msg = proc.send_input(text)
        body = {"ok": ok, "message": msg}
        if not ok:
            body["code"] = "not_running"
        return jsonify(body)

    @app.post("/api/sessions/<sid>/resize")
    def api_session_resize(sid):
        proc, error = _session_proc(sid)
        if error is not None:
            return error
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        rows, cols, size_error = terminal_size(data)
        if size_error:
            return _err("validation", size_error, 400)
        proc.set_winsize(rows, cols)
        return jsonify({"ok": True})

    @app.post("/api/sessions/<sid>/keepalive")
    def api_session_keepalive(sid):
        proc, error = _session_proc(sid)
        if error is not None:
            return error
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        enabled = bool(data.get("enabled"))
        proc.keepalive = enabled
        if enabled:
            proc.reset_fast_fail()  # 重新开启时清空熔断计数
        _audit("agent_keepalive", target=sid, result="ok")
        return jsonify({"ok": True, "keepalive": enabled})

    @app.get("/api/sessions/<sid>/status")
    def api_session_status(sid):
        proc, error = _session_proc(sid)
        if error is not None:
            return error
        return jsonify(proc.status())

    @app.get("/api/sessions/<sid>/stream")
    def api_session_stream(sid):
        proc, error = _session_proc(sid)
        if error is not None:
            return error

        def gen():
            q = proc.subscribe()
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
                proc.unsubscribe(q)

        return Response(gen(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        })

    # ------------------------------------------------------------------- #
    # 档案（4.2）
    # ------------------------------------------------------------------- #

    @app.get("/api/profiles")
    def api_list_profiles():
        with get_lock():
            cfg = load_config()
            profiles = [
                p for p in cfg.get("profiles", [])
                if isinstance(p, dict) and p.get("id")
            ]
        return jsonify({"ok": True, "profiles": profiles})

    @app.post("/api/profiles")
    def api_create_profile():
        data = request.get_json(silent=True)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        raw_name = data.get("name")
        if raw_name is None or not isinstance(raw_name, str) or not raw_name.strip():
            return _err("validation", "档案名称必须是字符串且不能为空", 400)
        raw_cwd = data.get("cwd")
        if raw_cwd is not None and not isinstance(raw_cwd, str):
            return _err("validation", "工作目录必须是字符串", 400)
        cwd = (raw_cwd or "").strip() or None
        provider_id = data.get("provider_id")
        if provider_id is not None and not isinstance(provider_id, str):
            return _err("validation", "供应商必须是字符串", 400)
        session_mode = data.get("session_mode") or "new"
        if session_mode not in ("new", "continue", "resume"):
            return _err("validation", "会话模式必须是 new/continue/resume", 400)
        account_id = data.get("account_id")
        if account_id is not None and not isinstance(account_id, str):
            return _err("validation", "账号 ID 必须是字符串", 400)
        with get_lock():
            cfg = load_config()
            if provider_id is None:
                provider_id = cfg["current_provider"]
            if provider_id not in cfg["providers"]:
                return _err("unknown_provider", "未知供应商", 400)
            if account_id is not None and not any(
                isinstance(a, dict) and a.get("id") == account_id
                for a in cfg["providers"][provider_id].get("accounts", [])
            ):
                return _err("unknown_account", "账号不存在或不属于该供应商", 404)
            profile = {
                "id": uuid.uuid4().hex[:12],
                "name": raw_name.strip(),
                "cwd": cwd,
                "provider_id": provider_id,
                "account_id": account_id,
                "session_mode": session_mode,
            }
            cfg.setdefault("profiles", []).append(profile)
            save_config(cfg)
        _audit("profile_create", target=profile["id"])
        return jsonify({"ok": True, "profile": profile})

    @app.put("/api/profiles/<profile_id>")
    def api_update_profile(profile_id):
        data = request.get_json(silent=True)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        raw_name = data.get("name")
        if raw_name is not None and not isinstance(raw_name, str):
            return _err("validation", "档案名称必须是字符串", 400)
        raw_cwd = data.get("cwd")
        if raw_cwd is not None and not isinstance(raw_cwd, str):
            return _err("validation", "工作目录必须是字符串", 400)
        provider_id = data.get("provider_id")
        if provider_id is not None and not isinstance(provider_id, str):
            return _err("validation", "供应商必须是字符串", 400)
        session_mode = data.get("session_mode")
        if session_mode is not None and session_mode not in ("new", "continue", "resume"):
            return _err("validation", "会话模式必须是 new/continue/resume", 400)
        account_id = data.get("account_id")
        if account_id is not None and not isinstance(account_id, str):
            return _err("validation", "账号 ID 必须是字符串", 400)
        with get_lock():
            cfg = load_config()
            profile = _find_profile(cfg, profile_id)
            if profile is None:
                return _err("unknown_profile", "档案不存在", 404)
            if provider_id is not None and provider_id not in cfg["providers"]:
                return _err("unknown_provider", "未知供应商", 400)
            if account_id is not None:
                target_pid = (
                    provider_id
                    or profile.get("provider_id")
                    or cfg["current_provider"]
                )
                if not any(
                    isinstance(a, dict) and a.get("id") == account_id
                    for a in cfg["providers"].get(target_pid, {}).get("accounts", [])
                ):
                    return _err("unknown_account", "账号不存在或不属于该供应商", 404)
            if "name" in data:
                profile["name"] = (data.get("name") or "").strip() or profile["name"]
            if "cwd" in data:
                profile["cwd"] = (data.get("cwd") or "").strip() or None
            if provider_id is not None:
                profile["provider_id"] = provider_id
            if session_mode is not None:
                profile["session_mode"] = session_mode
            if "account_id" in data:
                profile["account_id"] = account_id
            save_config(cfg)
        _audit("profile_edit", target=profile_id)
        return jsonify({"ok": True, "profile": profile})

    @app.delete("/api/profiles/<profile_id>")
    def api_delete_profile(profile_id):
        """删除档案：不影响已创建会话（会话创建时已物化字段）。"""
        with get_lock():
            cfg = load_config()
            if _find_profile(cfg, profile_id) is None:
                return _err("unknown_profile", "档案不存在", 404)
            cfg["profiles"] = [
                p for p in cfg.get("profiles", [])
                if not (isinstance(p, dict) and p.get("id") == profile_id)
            ]
            save_config(cfg)
        _audit("profile_delete", target=profile_id)
        return jsonify({"ok": True})

    @app.post("/api/agent/start")
    @app.post("/api/claude/start")
    def api_agent_start():
        agent_proc = registry.get_or_create_default()
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        rows, cols, error = terminal_size(data)
        if error:
            return _err("validation", error, 400)
        raw_cwd = data.get("cwd") or ""
        if not isinstance(raw_cwd, str):
            return _err("validation", "工作目录必须是字符串", 400)
        cwd = raw_cwd.strip() or None

        mode = session_mode(data)
        if agent_proc.is_running():
            _audit("agent_start", target="default", result="already_running")
            return jsonify({
                "ok": False,
                "code": "already_running",
                "message": "Agent 进程已在运行",
                "status": agent_proc.status(),
            })
        launch, launch_signature = current_launch_and_fingerprint(mode)
        if not launch.get("ready"):
            return _err("not_ready", launch["error"], 400)
        snapshot = launch_snapshot(launch, mode, cwd)
        # 记住本次启动参数，供「重启」复用
        agent_proc.last_launch = {
            "session_mode": mode, "rows": rows, "cols": cols, "cwd": cwd,
            "launch": snapshot,
        }
        ok, msg = agent_proc.start(
            launch["env"], launch["label"], rows=rows, cols=cols, cwd=cwd,
            command=launch["command"], clear_env=launch["clear_env"],
            client=launch["client"], launch_snapshot=snapshot,
            launch_signature=launch_signature,
            log_path=_session_log_path("default"),
        )
        if ok:
            _ensure_default_session()
        _audit("agent_start", target="default", result="ok" if ok else "error")
        return jsonify({"ok": ok, "message": msg, "status": agent_proc.status()})

    @app.post("/api/agent/restart")
    @app.post("/api/claude/restart")
    def api_agent_restart():
        agent_proc = registry.get_or_create_default()
        data = request.get_json(silent=True) or {}
        # 先验证全部参数和新 provider，避免无效请求先停掉正在运行的进程。
        last = dict(getattr(agent_proc, "last_launch", None) or {})
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        mode = (
            session_mode(data)
            if "session_mode" in data or "args" in data
            else last.get("session_mode", "new")
        )
        raw_cwd = data.get("cwd", last.get("cwd") or "")
        if not isinstance(raw_cwd, str):
            return _err("validation", "工作目录必须是字符串", 400)
        cwd = raw_cwd.strip() or None
        merged = {**last, **{k: data[k] for k in ("rows", "cols") if k in data}}
        rows, cols, error = terminal_size(merged)
        if error:
            return _err("validation", error, 400)
        launch, launch_signature = current_launch_and_fingerprint(mode)
        if not launch.get("ready"):
            return _err("not_ready", launch["error"], 400)
        snapshot = launch_snapshot(launch, mode, cwd)
        agent_proc.stop()
        time.sleep(0.3)
        ok, msg = agent_proc.start(
            launch["env"], launch["label"],
            rows=rows, cols=cols,
            cwd=cwd,
            command=launch["command"], clear_env=launch["clear_env"],
            client=launch["client"], launch_snapshot=snapshot,
            launch_signature=launch_signature,
        )
        if ok:
            agent_proc.last_launch = {
                **last,
                "rows": rows,
                "cols": cols,
                "cwd": cwd,
                "session_mode": mode,
                "launch": snapshot,
            }
        _audit("agent_restart", target="default", result="ok" if ok else "error")
        return jsonify({"ok": ok, "message": msg, "status": agent_proc.status()})

    @app.post("/api/agent/switch-restart")
    def api_agent_switch_restart():
        """原子「切换并重启」：未就绪不触碰进程。"""
        agent_proc = registry.get_or_create_default()
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        provider_id = data.get("provider")
        if not provider_id:
            return _err("validation", "缺少 provider 参数", 400)
        last = dict(getattr(agent_proc, "last_launch", None) or {})
        mode = (
            session_mode(data)
            if "session_mode" in data or "args" in data
            else last.get("session_mode", "new")
        )
        raw_cwd = data.get("cwd", last.get("cwd") or "")
        if not isinstance(raw_cwd, str):
            return _err("validation", "工作目录必须是字符串", 400)
        cwd = raw_cwd.strip() or None
        merged = {**last, **{k: data[k] for k in ("rows", "cols") if k in data}}
        rows, cols, error = terminal_size(merged)
        if error:
            return _err("validation", error, 400)
        result, error_response = _do_switch_restart(
            agent_proc, provider_id, mode, rows, cols, cwd
        )
        if error_response is not None:
            return error_response
        ok, msg, status = result
        return jsonify({"ok": ok, "message": msg, "status": status})

    @app.post("/api/agent/resize")
    @app.post("/api/claude/resize")
    def api_agent_resize():
        agent_proc = registry.get_or_create_default()
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        rows, cols, error = terminal_size(data)
        if error:
            return _err("validation", error, 400)
        agent_proc.set_winsize(rows, cols)
        return jsonify({"ok": True})

    @app.post("/api/agent/keepalive")
    @app.post("/api/claude/keepalive")
    def api_agent_keepalive():
        agent_proc = registry.get_or_create_default()
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        enabled = bool(data.get("enabled"))
        agent_proc.keepalive = enabled
        if enabled:
            agent_proc.reset_fast_fail()  # 重新开启时清空熔断计数
        _audit("agent_keepalive", target="default", result="ok")
        return jsonify({"ok": True, "keepalive": enabled})

    @app.post("/api/agent/stop")
    @app.post("/api/claude/stop")
    def api_agent_stop():
        agent_proc = registry.get_or_create_default()
        ok, msg = agent_proc.stop()
        body = {"ok": ok, "message": msg, "status": agent_proc.status()}
        if not ok:
            body["code"] = "not_running"
        _audit("agent_stop", target="default", result="ok" if ok else "not_running")
        return jsonify(body)

    @app.post("/api/agent/input")
    @app.post("/api/claude/input")
    def api_agent_input():
        agent_proc = registry.get_or_create_default()
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return _err("validation", "请求体必须是 JSON 对象", 400)
        if "raw" in data:
            # xterm 原始按键流（含方向键/控制字符/转义序列），原样透传
            text = data["raw"]
        else:
            text = data.get("text", "")
            if not isinstance(text, str):
                return _err("validation", "输入必须是字符串", 400)
            if not text.endswith("\n"):
                text += "\n"
        if not isinstance(text, str):
            return _err("validation", "输入必须是字符串", 400)
        ok, msg = agent_proc.send_input(text)
        body = {"ok": ok, "message": msg}
        if not ok:
            body["code"] = "not_running"
        return jsonify(body)

    @app.get("/api/agent/status")
    @app.get("/api/claude/status")
    def api_agent_status():
        return jsonify(registry.get_or_create_default().status())

    @app.get("/api/fs/list")
    def api_fs_list():
        """列出某目录下的子目录，供前端目录选择器使用（限 fs_root 内）。"""
        raw = request.args.get("path", "~")
        try:
            path = Path(os.path.expanduser(raw or "~")).resolve()
        except OSError as exc:
            return _err("validation", f"无法解析目录：{exc}", 400)
        if not (path == fs_root_resolved or fs_root_resolved in path.parents):
            return _err("forbidden", "目录超出允许范围", 403)
        if not path.exists():
            return _err("validation", f"目录不存在：{path}", 404)
        if not path.is_dir():
            return _err("validation", f"不是目录：{path}", 400)
        try:
            dirs = sorted(
                [p.name for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")],
                key=str.lower,
            )
        except PermissionError:
            return _err("forbidden", f"无权读取目录：{path}", 403)
        return jsonify({
            "path": str(path),
            "parent": str(path.parent) if path != path.parent else None,
            "dirs": dirs,
            "home": str(Path.home()),
        })

    @app.get("/api/config/export")
    def api_config_export():
        """导出完整配置；默认密钥掩码，include_keys=1 返回原值。"""
        include_keys = request.args.get("include_keys") in ("1", "true", "yes")
        with get_lock():
            cfg = load_config()
        out = json.loads(json.dumps(cfg))
        if not include_keys:
            for p in out.get("providers", {}).values():
                for acc in p.get("accounts", []):
                    if isinstance(acc, dict) and "api_key" in acc:
                        acc["api_key"] = mask_key(acc["api_key"])
        return jsonify({"ok": True, "config": out})

    @app.post("/api/config/import")
    def api_config_import():
        """导入配置：merge（账号按 id 并集）或 replace（先备份当前配置）。"""
        data = request.get_json(force=True)
        if not isinstance(data, dict) or not isinstance(data.get("config"), dict):
            return _err("validation", "导入内容必须是 {config: {...}} 对象", 400)
        mode = data.get("mode", "merge")
        if mode not in ("merge", "replace"):
            return _err("validation", "mode 必须是 merge 或 replace", 400)
        incoming = data["config"]
        providers = incoming.get("providers")
        if not isinstance(providers, dict):
            return _err("validation", "providers 必须是 JSON 对象", 400)
        for pid, p in providers.items():
            if not _normalize_provider(p):
                return _err("validation", f"供应商 {pid} 配置格式无效", 400)
        with get_lock():
            if mode == "replace":
                if config.CONFIG_PATH.exists():
                    backup = config.CONFIG_PATH.with_name(
                        config.CONFIG_PATH.name
                        + ".pre-import-"
                        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    )
                    shutil.copy2(config.CONFIG_PATH, backup)
                    os.chmod(backup, 0o600)
                save_config(json.loads(json.dumps(incoming)))
            else:
                cfg = load_config()
                for pid, inc_p in incoming["providers"].items():
                    cur = cfg["providers"].get(pid)
                    if cur is None:
                        new_p = json.loads(json.dumps(inc_p))
                        new_p.setdefault("accounts", [])
                        new_p.setdefault("active_account", None)
                        cfg["providers"][pid] = new_p
                        continue
                    merged_accounts = {}
                    order = []
                    for acc in cur.get("accounts", []):
                        if isinstance(acc, dict) and acc.get("id"):
                            merged_accounts[acc["id"]] = acc
                            order.append(acc["id"])
                    for acc in inc_p.get("accounts", []):
                        if not isinstance(acc, dict) or not acc.get("id"):
                            continue
                        if acc["id"] not in merged_accounts:
                            order.append(acc["id"])
                        merged_accounts[acc["id"]] = json.loads(json.dumps(acc))
                    cur["accounts"] = [merged_accounts[i] for i in order]
                    inc_active = inc_p.get("active_account")
                    if isinstance(inc_active, str) and inc_active in merged_accounts:
                        cur["active_account"] = inc_active
                    for field in ("label", "base_url", "model"):
                        val = inc_p.get(field)
                        if isinstance(val, str) and val.strip():
                            cur[field] = val
                save_config(cfg)
        _audit("config_import", target=mode)
        return jsonify({"ok": True})

    @app.get("/api/account/export.csv")
    def api_export_account_csv():
        """账号导出 CSV：provider,name,api_key,active（默认掩码）。"""
        include_keys = request.args.get("include_keys") in ("1", "true", "yes")
        with get_lock():
            cfg = load_config()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["provider", "name", "api_key", "active"])
        for pid, p in cfg["providers"].items():
            active = p.get("active_account")
            for acc in p.get("accounts", []):
                if not isinstance(acc, dict):
                    continue
                key = acc.get("api_key", "")
                if not include_keys:
                    key = mask_key(key)
                writer.writerow([
                    pid,
                    acc.get("name", ""),
                    key,
                    "true" if acc.get("id") == active else "false",
                ])
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=cc-switch-accounts.csv"},
        )

    @app.get("/api/agent/stream")
    @app.get("/api/claude/stream")
    def api_agent_stream():
        agent_proc = registry.get_or_create_default()

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

    # 看门狗重建钩子：默认会话按全局选择重建（legacy 语义），
    # 其余会话在创建时各自挂上按自身 provider/account 重建的钩子。
    agent_proc.set_rebuild_launch(_make_rebuild_for(SessionRegistry.DEFAULT_ID))

    # 挂到 app 上供入口清理；保留旧属性名兼容现有集成。
    app._agent_proc = agent_proc
    app._claude_proc = agent_proc
    app._sessions = registry
    app._cli_manager = cli_manager
    return app
