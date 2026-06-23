"""
配置管理 —— 读写 ~/.ccm_config，管理供应商与账号。
"""

import json
import os
import shutil
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
        "base_url": "https://api.anthropic.com",
        "auth_var": "ANTHROPIC_API_KEY",
        "model": "",
        "accounts": [],
        "active_account": None,
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/anthropic",
        "auth_var": "ANTHROPIC_AUTH_TOKEN",
        "model": "deepseek-chat",
        "accounts": [],
        "active_account": None,
    },
    "kimi": {
        "label": "Kimi (Moonshot)",
        "base_url": "https://api.moonshot.cn/anthropic",
        "auth_var": "ANTHROPIC_AUTH_TOKEN",
        "model": "kimi-k2-0905-preview",
        "accounts": [],
        "active_account": None,
    },
    "glm": {
        "label": "GLM (智谱)",
        "base_url": "https://open.bigmodel.cn/api/anthropic",
        "auth_var": "ANTHROPIC_AUTH_TOKEN",
        "model": "glm-4.6",
        "accounts": [],
        "active_account": None,
    },
    "qwen": {
        "label": "Qwen (通义千问)",
        "base_url": "https://dashscope.aliyuncs.com/api/v2/apps/claude-code-proxy",
        "auth_var": "ANTHROPIC_AUTH_TOKEN",
        "model": "qwen3-coder-plus",
        "accounts": [],
        "active_account": None,
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "auth_var": "ANTHROPIC_AUTH_TOKEN",
        "model": "anthropic/claude-3.5-sonnet",
        "accounts": [],
        "active_account": None,
    },
    "custom": {
        "label": "自定义",
        "base_url": "",
        "auth_var": "ANTHROPIC_AUTH_TOKEN",
        "model": "",
        "accounts": [],
        "active_account": None,
    },
}

AUTH_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

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
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)  # 含密钥，限制权限
    except OSError:
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
    }


def build_env_for_active():
    """根据当前供应商 + 激活账号构造注入环境变量。"""
    cfg = load_config()
    pid = cfg["current_provider"]
    provider = cfg["providers"].get(pid, {})
    acc = active_account_of(provider)
    label = provider.get("label", pid)

    env = {}
    base_url = provider.get("base_url", "")
    model = provider.get("model", "")
    auth_var = provider.get("auth_var", "ANTHROPIC_API_KEY")
    api_key = acc.get("api_key", "") if acc else ""

    if pid != "claude" and base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    if api_key:
        env[auth_var] = api_key
    if model:
        env["ANTHROPIC_MODEL"] = model
    return env, label, bool(api_key)


def get_lock():
    return _config_lock
