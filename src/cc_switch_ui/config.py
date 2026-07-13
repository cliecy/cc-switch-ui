"""
配置管理 —— 读写 ~/.ccm_config，管理供应商与账号。
"""

import json
import os
import shutil
import tempfile
import threading
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


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #

def _default_config():
    """深拷贝预置项，避免运行期被修改污染默认值。"""
    return {
        "current_provider": "claude",
        "providers": json.loads(json.dumps(DEFAULT_PROVIDERS)),
    }


def load_config():
    if not CONFIG_PATH.exists():
        cfg = _default_config()
        save_config(cfg)
        return cfg
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
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


def public_state():
    """返回给前端的状态（密钥脱敏）。"""
    cfg = load_config()
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
        }
    return {
        "current_provider": cfg["current_provider"],
        "providers": out_providers,
        "ccm_available": shutil.which("ccm") is not None,
        "claude_available": shutil.which("claude") is not None,
        "codex_available": shutil.which("codex") is not None,
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

    if session_mode not in ("new", "continue", "resume"):
        return {"ready": False, "error": "未知会话模式", "label": label, "client": client}

    if client == "codex":
        if not base_url:
            return {
                "ready": False,
                "error": f"当前供应商({label})未配置 Base URL",
                "label": label,
                "client": client,
            }
        if not model:
            return {
                "ready": False,
                "error": f"当前供应商({label})未配置模型 ID",
                "label": label,
                "client": client,
            }
        if not api_key:
            return {
                "ready": False,
                "error": f"当前供应商({label})未配置可用 API Key",
                "label": label,
                "client": client,
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
            "client": client,
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
    if pid != "claude" and not api_key:
        return {
            "ready": False,
            "error": f"当前供应商({label})未配置可用 API Key",
            "label": label,
            "client": client,
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
        "client": client,
    }


def get_lock():
    return _config_lock
