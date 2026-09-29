"""
配置管理 —— 读写 ~/.ccm_config，管理供应商与账号。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

CONFIG_PATH = Path(os.path.expanduser("~/.ccm_config"))
STATE_DIR = Path(
    os.environ.get("CC_SWITCH_HOME") or os.path.expanduser("~/.cc-switch-ui")
)


def configure_paths(config_dir: Path | None = None) -> None:
    """指定配置目录时改写模块级 CONFIG_PATH / STATE_DIR；为空时保持默认。"""
    global CONFIG_PATH, STATE_DIR
    if not config_dir:
        return
    config_dir = Path(config_dir).expanduser()
    CONFIG_PATH = config_dir / ".ccm_config"
    STATE_DIR = config_dir

# 预置供应商：Claude 走 Anthropic 兼容协议，Codex 走 OpenAI Responses 兼容协议。
DEFAULT_PROVIDERS = {
    "claude": {
        "label": "Claude 官方",
        "client": "claude",
        "base_url": "https://api.anthropic.com",
        "auth_var": "ANTHROPIC_API_KEY",
        "model": "",
        "accounts": [],
        "active_account": None,
    },
    "deepseek": {
        "label": "DeepSeek",
        "client": "claude",
        "base_url": "https://api.deepseek.com/anthropic",
        "auth_var": "ANTHROPIC_AUTH_TOKEN",
        "model": "deepseek-chat",
        "accounts": [],
        "active_account": None,
    },
    "kimi": {
        "label": "Kimi (Moonshot)",
        "client": "claude",
        "base_url": "https://api.moonshot.cn/anthropic",
        "auth_var": "ANTHROPIC_AUTH_TOKEN",
        "model": "kimi-k2-0905-preview",
        "accounts": [],
        "active_account": None,
    },
    "glm": {
        "label": "GLM (智谱)",
        "client": "claude",
        "base_url": "https://open.bigmodel.cn/api/anthropic",
        "auth_var": "ANTHROPIC_AUTH_TOKEN",
        "model": "glm-4.6",
        "accounts": [],
        "active_account": None,
    },
    "qwen": {
        "label": "Qwen (通义千问)",
        "client": "claude",
        "base_url": "https://dashscope.aliyuncs.com/api/v2/apps/claude-code-proxy",
        "auth_var": "ANTHROPIC_AUTH_TOKEN",
        "model": "qwen3-coder-plus",
        "accounts": [],
        "active_account": None,
    },
    "openrouter": {
        "label": "OpenRouter",
        "client": "claude",
        "base_url": "https://openrouter.ai/api",
        "auth_var": "ANTHROPIC_AUTH_TOKEN",
        "model": "anthropic/claude-3.5-sonnet",
        "accounts": [],
        "active_account": None,
    },
    "custom": {
        "label": "自定义",
        "client": "claude",
        "base_url": "",
        "auth_var": "ANTHROPIC_AUTH_TOKEN",
        "model": "",
        "accounts": [],
        "active_account": None,
    },
    "codex_custom": {
        "label": "Codex · 自定义 OpenAI",
        "client": "codex",
        "base_url": "",
        "auth_var": "CC_SWITCH_CODEX_API_KEY",
        "model": "",
        "accounts": [],
        "active_account": None,
    },
}

AUTH_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
CODEX_AUTH_VAR = "CC_SWITCH_CODEX_API_KEY"
_URL_CREDENTIALS_RE = re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)

# Provider-related values must not leak from the service process into a newly
# selected client. Codex receives its custom key through a private env var that
# is referenced by the per-launch provider configuration.
ANTHROPIC_ENV_VARS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)
CROSS_CLIENT_ENV_VARS = ("OPENAI_API_KEY", CODEX_AUTH_VAR)
CLAUDE_ENV_VARS = ANTHROPIC_ENV_VARS + CROSS_CLIENT_ENV_VARS
CODEX_ENV_VARS = ANTHROPIC_ENV_VARS + CROSS_CLIENT_ENV_VARS

_config_lock = threading.Lock()
_config_recovery_notice = None


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #

def _default_config():
    """深拷贝预置项，避免运行期被修改污染默认值。"""
    return {
        "current_provider": "claude",
        "providers": json.loads(json.dumps(DEFAULT_PROVIDERS)),
        "sessions": [],
        "profiles": [],
    }


def _backup_invalid_config(error, message=None):
    """Preserve an invalid config before creating a clean replacement."""
    global _config_recovery_notice

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.corrupt-{timestamp}")
    suffix = 1
    while backup_path.exists():
        backup_path = CONFIG_PATH.with_name(
            f"{CONFIG_PATH.name}.corrupt-{timestamp}-{suffix}"
        )
        suffix += 1
    os.replace(CONFIG_PATH, backup_path)
    try:
        os.chmod(backup_path, 0o600)
    except OSError:
        pass
    _config_recovery_notice = {
        "config_path": str(CONFIG_PATH),
        "message": message or "配置文件格式无效，已保留备份并恢复默认配置。",
        "backup_path": str(backup_path),
        "error": str(error),
    }


def _normalize_provider(p) -> bool:
    """校验单个供应商条目字段类型是否合法（无副作用）。"""
    if not isinstance(p, dict):
        return False
    for field in ("label", "base_url", "model", "auth_var"):
        value = p.get(field)
        if value is not None and not isinstance(value, str):
            return False
    accounts = p.get("accounts")
    if accounts is not None:
        if not isinstance(accounts, list):
            return False
        for acc in accounts:
            if not isinstance(acc, dict) or not isinstance(acc.get("id"), str):
                return False
    active = p.get("active_account")
    if active is not None and not isinstance(active, str):
        return False
    return True

_SESSION_FIELDS = ("id", "name", "cwd", "provider_id", "session_mode", "account_id")


def _normalize_session(s) -> bool:
    """校验单个会话条目字段类型是否合法（无副作用）。"""
    if not isinstance(s, dict):
        return False
    for field in _SESSION_FIELDS:
        value = s.get(field)
        if value is not None and not isinstance(value, str):
            return False
    return True

_PROFILE_FIELDS = ("id", "name", "cwd", "provider_id", "account_id", "session_mode")


def _normalize_profile(p) -> bool:
    """校验单个档案条目字段类型是否合法（无副作用）。"""
    if not isinstance(p, dict):
        return False
    for field in _PROFILE_FIELDS:
        value = p.get(field)
        if value is not None and not isinstance(value, str):
            return False
    return True

def load_config():
    if not CONFIG_PATH.exists():
        cfg = _default_config()
        save_config(cfg)
        return cfg
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
        cfg = json.loads(raw)
        if not isinstance(cfg, dict) or not isinstance(cfg.get("providers", {}), dict):
            raise ValueError("配置根节点和 providers 必须是 JSON 对象")
    except (json.JSONDecodeError, ValueError) as exc:
        _backup_invalid_config(exc)
        cfg = _default_config()
        save_config(cfg)
        return cfg

    # 隔离格式非法的供应商条目：原文件整份备份，坏条目移入 providers_broken
    if not isinstance(cfg.get("current_provider"), str):
        cfg["current_provider"] = "claude"
    providers = cfg["providers"]
    if not isinstance(cfg.get("providers_broken"), dict):
        cfg["providers_broken"] = {}
    broken_ids = [pid for pid, p in providers.items() if not _normalize_provider(p)]
    if broken_ids:
        for pid in broken_ids:
            cfg["providers_broken"][pid] = providers.pop(pid)
        _backup_invalid_config(
            ValueError(f"供应商配置格式无效: {', '.join(sorted(broken_ids))}"),
            message="部分供应商配置格式无效，已隔离到 providers_broken 并可经备份恢复",
        )

    # 合并：确保所有预置供应商存在，且字段完整（向前兼容）
    for key, preset in DEFAULT_PROVIDERS.items():
        if key in broken_ids:
            continue
        p = providers.setdefault(key, json.loads(json.dumps(preset)))
        for field, val in preset.items():
            if field not in ("accounts", "active_account"):
                p.setdefault(field, val)
        p.setdefault("accounts", [])
        p.setdefault("active_account", None)
        for acc in p["accounts"]:
            shared = acc.get("shared")
            acc["shared"] = shared if isinstance(shared, bool) else False

    # 隔离格式非法的会话条目：坏条目移入 sessions_broken，合法条目补齐默认字段
    if not isinstance(cfg.get("sessions"), list):
        cfg["sessions"] = []
    if not isinstance(cfg.get("sessions_broken"), list):
        cfg["sessions_broken"] = []
    bad_sessions = [s for s in cfg["sessions"] if not _normalize_session(s)]
    if bad_sessions:
        for s in bad_sessions:
            cfg["sessions_broken"].append(s)
        cfg["sessions"] = [s for s in cfg["sessions"] if _normalize_session(s)]
        _backup_invalid_config(
            ValueError(f"会话配置格式无效: {len(bad_sessions)} 条"),
            message="部分会话配置格式无效，已隔离到 sessions_broken 并可经备份恢复",
        )
    for s in cfg["sessions"]:
        for field in _SESSION_FIELDS:
            s.setdefault(field, None)
        if s["session_mode"] is None:
            s["session_mode"] = "new"
    # 隔离格式非法的档案条目：坏条目移入 profiles_broken，合法条目补齐默认字段
    if not isinstance(cfg.get("profiles"), list):
        cfg["profiles"] = []
    if not isinstance(cfg.get("profiles_broken"), list):
        cfg["profiles_broken"] = []
    bad_profiles = [p for p in cfg["profiles"] if not _normalize_profile(p)]
    if bad_profiles:
        for p in bad_profiles:
            cfg["profiles_broken"].append(p)
        cfg["profiles"] = [p for p in cfg["profiles"] if _normalize_profile(p)]
        _backup_invalid_config(
            ValueError(f"档案配置格式无效: {len(bad_profiles)} 条"),
            message="部分档案配置格式无效，已隔离到 profiles_broken 并可经备份恢复",
        )
    for p in cfg["profiles"]:
        for field in _PROFILE_FIELDS:
            p.setdefault(field, None)
        if p["session_mode"] is None:
            p["session_mode"] = "new"
    if broken_ids or bad_sessions or bad_profiles:
        save_config(cfg)
    return cfg


def save_config(cfg):
    """Atomically persist config with private permissions from creation time."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{CONFIG_PATH.name}.", dir=CONFIG_PATH.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, CONFIG_PATH)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- #
# 回收站（账号软删除与恢复）
# --------------------------------------------------------------------------- #


def trash_path() -> Path:
    return CONFIG_PATH.with_name(CONFIG_PATH.name + ".trash")


def _save_trash(entries):
    """Atomically persist trash with private permissions from creation time."""
    path = trash_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"deleted": entries}, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _load_trash():
    """读取回收站条目；文件损坏时重命名后按空处理；加载时剔除超期条目并回写。"""
    path = trash_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("deleted")
        if not isinstance(data, dict) or not isinstance(entries, list):
            raise ValueError("回收站文件结构无效")
        entries = [e for e in entries if isinstance(e, dict)]
    except (json.JSONDecodeError, ValueError, OSError):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        corrupted = path.with_name(f"{path.name}.corrupt-{timestamp}")
        suffix = 1
        while corrupted.exists():
            corrupted = path.with_name(f"{path.name}.corrupt-{timestamp}-{suffix}")
            suffix += 1
        os.replace(path, corrupted)
        return []
    now = datetime.now(timezone.utc)
    fresh = []
    for entry in entries:
        try:
            deleted_at = datetime.fromisoformat(entry["deleted_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if (now - deleted_at) <= timedelta(days=7):
            fresh.append(entry)
    if len(fresh) != len(entries):
        _save_trash(fresh)
    return fresh


def move_account_to_trash(provider_id, account):
    """把被删除的账号（含 api_key）整体移入回收站并落盘。"""
    entries = _load_trash()
    entries.append(
        {
            "provider": provider_id,
            "account": account,
            "deleted_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _save_trash(entries)


def restore_account(provider_id, account_id) -> tuple[dict | None, str | None]:
    """从回收站按 (provider, account id) 恢复账号；成功返回 (account, None)。"""
    entries = _load_trash()
    for i, entry in enumerate(entries):
        account = entry.get("account")
        if (
            entry.get("provider") == provider_id
            and isinstance(account, dict)
            and account.get("id") == account_id
        ):
            restored = entries.pop(i)
            _save_trash(entries)
            return restored["account"], None
    return None, "该账号不在回收站中"



# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    if len(key) <= 16:
        return f"{key[:2]}{'•' * 6}{key[-2:]}"
    return f"{key[:4]}{'•' * 6}{key[-4:]}"


def mask_url_credentials(url: str) -> str:
    """脱敏 URL 内嵌的 user:password 为 ***:***。"""
    return _URL_CREDENTIALS_RE.sub(r"\1***:***@", url or "")


def active_account_of(provider: dict):
    aid = provider.get("active_account")
    for acc in provider.get("accounts", []):
        if acc["id"] == aid:
            return acc
    return None


def _account_for_launch(provider: dict, account_id=None):
    """指定 account_id 且存在时用该账号，否则回落到激活账号。"""
    if account_id is not None:
        for acc in provider.get("accounts", []):
            if acc.get("id") == account_id:
                return acc
    return active_account_of(provider)


def launch_fingerprint_for_provider(provider_id: str, account_id=None, *, cfg=None):
    """Return a non-secret identity for a launch of the given provider.

    The API key participates only in the digest. The digest is kept inside the
    service process and is never included in public state or launch snapshots.
    """
    cfg = load_config() if cfg is None else cfg
    provider = cfg["providers"].get(provider_id, {})
    account = _account_for_launch(provider, account_id)
    api_key = (account.get("api_key") if account else "") or ""
    client = provider.get("client", "claude")
    payload = {
        "provider_id": provider_id,
        "client": client,
        "base_url": (provider.get("base_url") or "").strip(),
        "model": (provider.get("model") or "").strip(),
        "account_id": account.get("id") if account else None,
        "auth_var": provider.get("auth_var", "ANTHROPIC_API_KEY") if api_key else "",
        "api_key": api_key,
    }
    if client == "codex":
        # Codex receives the provider label as part of its per-launch command.
        payload["provider_label"] = provider.get("label", provider_id)
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def launch_fingerprint_for_active(*, cfg=None):
    """Compatibility wrapper: fingerprint of the currently selected provider."""
    cfg = load_config() if cfg is None else cfg
    return launch_fingerprint_for_provider(cfg["current_provider"], cfg=cfg)


def provider_readiness(provider_id: str, provider: dict, *, cli_available=None):
    """Return a user-facing readiness summary without exposing credentials."""
    client = provider.get("client", "claude")
    account = active_account_of(provider)
    missing = []
    if cli_available is None:
        cli_available = shutil.which(client) is not None
    if not cli_available:
        missing.append(f"{client} CLI")

    base_url = (provider.get("base_url") or "").strip()
    model = (provider.get("model") or "").strip()
    api_key = (account.get("api_key") if account else "") or ""
    if client == "codex":
        if not base_url:
            missing.append("Base URL")
        if not model:
            missing.append("模型 ID")
        if not api_key:
            missing.append("API Key")
    elif provider_id != "claude":
        if not base_url:
            missing.append("Base URL")
        if not api_key:
            missing.append("API Key")

    return {
        "ready": not missing,
        "missing": missing,
        "auth_mode": "claude_login" if provider_id == "claude" and not api_key else "api_key",
        "account_name": account.get("name", "") if account else "",
    }


def public_state(*, cfg=None):
    """返回给前端的状态（密钥脱敏）。"""
    cfg = load_config() if cfg is None else cfg
    cli_availability = {
        "claude": shutil.which("claude") is not None,
        "codex": shutil.which("codex") is not None,
    }
    out_providers = {}
    for key, p in cfg["providers"].items():
        accounts = [
            {
                "id": a["id"],
                "name": a.get("name", ""),
                "shared": bool(a.get("shared", False)),
                "key_masked": mask_key(a.get("api_key", "")),
                "has_key": bool(a.get("api_key")),
            }
            for a in p.get("accounts", [])
        ]
        out_providers[key] = {
            "id": key,
            "label": p.get("label", key),
            "client": p.get("client", "claude"),
            "base_url": mask_url_credentials(p.get("base_url", "")),
            "model": p.get("model", ""),
            "auth_var": p.get("auth_var", "ANTHROPIC_API_KEY"),
            "accounts": accounts,
            "active_account": p.get("active_account"),
            "readiness": provider_readiness(
                key,
                p,
                cli_available=cli_availability.get(p.get("client", "claude"), False),
            ),
        }
    current_provider = cfg["current_provider"]
    selected = out_providers.get(current_provider, {})
    selected_readiness = selected.get("readiness", {})
    config_warning = None
    if (
        _config_recovery_notice
        and _config_recovery_notice.get("config_path") == str(CONFIG_PATH)
    ):
        config_warning = {
            key: value
            for key, value in _config_recovery_notice.items()
            if key != "config_path"
        }
    return {
        "current_provider": current_provider,
        "providers": out_providers,
        "providers_broken": sorted(cfg.get("providers_broken", {}).keys()),
        "ccm_available": shutil.which("ccm") is not None,
        "claude_available": cli_availability["claude"],
        "codex_available": cli_availability["codex"],
        "selected_launch": {
            "provider_id": current_provider,
            "provider_label": selected.get("label", current_provider),
            "client": selected.get("client", "claude"),
            "base_url": selected.get("base_url", ""),
            "model": selected.get("model", ""),
            "account_id": selected.get("active_account"),
            "account_name": selected_readiness.get("account_name", ""),
            "auth_mode": selected_readiness.get("auth_mode", "api_key"),
            "ready": selected_readiness.get("ready", False),
            "missing": selected_readiness.get("missing", []),
        },
        "config_warning": config_warning,
    }


def build_env_for_active():
    """Compatibility wrapper for integrations using the original helper."""
    launch = build_launch_for_active("new")
    env = launch.get("env", {})
    return env, launch.get("label", "?"), bool(env)


def _toml_string(value: str) -> str:
    """Return a safely quoted TOML basic string for a Codex -c override."""
    return json.dumps(str(value), ensure_ascii=True)


def build_launch_for_provider(provider_id: str, session_mode="new", account_id=None, *, cfg=None) -> dict:
    """Build a complete, tool-specific launch description for the given provider.

    account_id 指定且存在于该供应商时优先使用该账号，否则回落到激活账号。
    """
    cfg = load_config() if cfg is None else cfg
    pid = provider_id
    provider = cfg["providers"].get(pid, {})
    account = _account_for_launch(provider, account_id)
    label = provider.get("label", pid)
    client = provider.get("client", "claude")
    base_url = (provider.get("base_url") or "").strip()
    model = (provider.get("model") or "").strip()
    api_key = (account.get("api_key") if account else "") or ""
    metadata = {
        "provider_id": pid,
        "provider_label": label,
        "client": client,
        "base_url": base_url,
        "model": model,
        "account_id": account.get("id") if account else None,
        "account_name": account.get("name", "") if account else "",
        "session_mode": session_mode,
    }

    if session_mode not in ("new", "continue", "resume"):
        return {
            "ready": False,
            "error": "未知会话模式",
            "label": label,
            **metadata,
        }

    if client == "codex":
        if not base_url:
            return {
                "ready": False,
                "error": f"当前供应商({label})未配置 Base URL",
                "label": label,
                **metadata,
            }
        if not model:
            return {
                "ready": False,
                "error": f"当前供应商({label})未配置模型 ID",
                "label": label,
                **metadata,
            }
        if not api_key:
            return {
                "ready": False,
                "error": f"当前供应商({label})未配置可用 API Key",
                "label": label,
                **metadata,
            }

        command = ["codex"]
        if session_mode == "continue":
            command.extend(("resume", "--last"))
        elif session_mode == "resume":
            command.append("resume")
        command.extend((
            "-c", 'model_provider="cc_switch_ui"',
            "-c", f"model_providers.cc_switch_ui.name={_toml_string(label)}",
            "-c", f"model_providers.cc_switch_ui.base_url={_toml_string(base_url)}",
            "-c", f'model_providers.cc_switch_ui.env_key="{CODEX_AUTH_VAR}"',
            "-c", 'model_providers.cc_switch_ui.wire_api="responses"',
            "-c", "model_providers.cc_switch_ui.requires_openai_auth=false",
            "-m", model,
        ))
        return {
            "ready": True,
            "command": command,
            "env": {CODEX_AUTH_VAR: api_key},
            "clear_env": CODEX_ENV_VARS,
            "label": label,
            **metadata,
        }

    env = {}
    auth_var = provider.get("auth_var", "ANTHROPIC_API_KEY")
    if pid != "claude" and base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    if api_key:
        env[auth_var] = api_key
        other_auth_var = (
            "ANTHROPIC_AUTH_TOKEN"
            if auth_var == "ANTHROPIC_API_KEY"
            else "ANTHROPIC_API_KEY"
        )
        env[other_auth_var] = ""
    if model:
        env["ANTHROPIC_MODEL"] = model
    if pid != "claude" and not base_url:
        return {
            "ready": False,
            "error": f"当前供应商({label})未配置 Base URL",
            "label": label,
            **metadata,
        }
    if pid != "claude" and not api_key:
        return {
            "ready": False,
            "error": f"当前供应商({label})未配置可用 API Key",
            "label": label,
            **metadata,
        }

    args = []
    if session_mode == "continue":
        args.append("--continue")
    elif session_mode == "resume":
        args.append("--resume")
    return {
        "ready": True,
        "command": ["claude", *args],
        "env": env,
        "clear_env": CLAUDE_ENV_VARS,
        "label": label,
        **metadata,
    }


def build_launch_for_active(session_mode="new", *, cfg=None):
    """Compatibility wrapper: build launch for the currently selected provider."""
    cfg = load_config() if cfg is None else cfg
    return build_launch_for_provider(cfg["current_provider"], session_mode, cfg=cfg)

def get_lock():
    return _config_lock
