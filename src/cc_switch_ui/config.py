"""
配置管理 —— 读写 ~/.ccm_config，管理供应商与账号。
"""

import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

CONFIG_PATH = Path(os.path.expanduser("~/.ccm_config"))

# 预置供应商。所有都走 Anthropic 兼容协议，claude 通过环境变量切换后端。
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
CODEX_ENV_VARS = ("CC_SWITCH_CODEX_API_KEY",)

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
    }


def _backup_invalid_config(error):
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
        "message": "配置文件格式无效，已保留备份并恢复默认配置。",
        "backup_path": str(backup_path),
        "error": str(error),
    }


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

    # 合并：确保所有预置供应商存在，且字段完整（向前兼容）
    cfg.setdefault("current_provider", "claude")
    providers = cfg.setdefault("providers", {})
    for key, preset in DEFAULT_PROVIDERS.items():
        p = providers.setdefault(key, json.loads(json.dumps(preset)))
        for field, val in preset.items():
            if field not in ("accounts", "active_account"):
                p.setdefault(field, val)
        p.setdefault("accounts", [])
        p.setdefault("active_account", None)
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
# 工具函数
# --------------------------------------------------------------------------- #

def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:4]}{'•' * 6}{key[-4:]}"


def active_account_of(provider: dict):
    aid = provider.get("active_account")
    for acc in provider.get("accounts", []):
        if acc["id"] == aid:
            return acc
    return None


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


def public_state():
    """返回给前端的状态（密钥脱敏）。"""
    cfg = load_config()
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
                "key_masked": mask_key(a.get("api_key", "")),
                "has_key": bool(a.get("api_key")),
            }
            for a in p.get("accounts", [])
        ]
        out_providers[key] = {
            "id": key,
            "label": p.get("label", key),
            "client": p.get("client", "claude"),
            "base_url": p.get("base_url", ""),
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


def build_launch_for_active(session_mode="new"):
    """Build a complete, tool-specific launch description for the active provider."""
    cfg = load_config()
    pid = cfg["current_provider"]
    provider = cfg["providers"].get(pid, {})
    account = active_account_of(provider)
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
            "-c", 'model_providers.cc_switch_ui.env_key="CC_SWITCH_CODEX_API_KEY"',
            "-c", 'model_providers.cc_switch_ui.wire_api="responses"',
            "-c", "model_providers.cc_switch_ui.requires_openai_auth=false",
            "-m", model,
        ))
        return {
            "ready": True,
            "command": command,
            "env": {"CC_SWITCH_CODEX_API_KEY": api_key},
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
        "clear_env": ANTHROPIC_ENV_VARS,
        "label": label,
        **metadata,
    }


def get_lock():
    return _config_lock
