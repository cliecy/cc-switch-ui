"""Guarded installation and version management for supported Agent CLIs."""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
from pathlib import Path


CLI_SPECS = {
    "claude": {
        "label": "Claude Code",
        "package": "@anthropic-ai/claude-code",
        "executable": "claude",
    },
    "codex": {
        "label": "Codex CLI",
        "package": "@openai/codex",
        "executable": "codex",
    },
}

NPM_REGISTRIES = {
    "official": {
        "label": "npm 官方源",
        "url": "https://registry.npmjs.org",
        "third_party": False,
    },
    "china": {
        "label": "npmmirror 中国镜像（第三方）",
        "url": "https://registry.npmmirror.com",
        "third_party": True,
    },
}

_VERSION_RE = re.compile(r"^(?:v)?[0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z.-]+)?$")
_VERSION_IN_OUTPUT_RE = re.compile(
    r"(?<![0-9])([0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z.-]+)?)"
)
_URL_CREDENTIALS_RE = re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)


class CliManagerError(RuntimeError):
    """A safe, user-facing CLI management failure."""


def _trim_output(text: str, limit: int = 8000) -> str:
    text = _URL_CREDENTIALS_RE.sub(r"\1***:***@", text or "").strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _version_from_output(output: str) -> str | None:
    match = _VERSION_IN_OUTPUT_RE.search(output or "")
    return match.group(1) if match else None


def _install_method(path: str | None) -> str | None:
    if not path:
        return None
    try:
        resolved = str(Path(path).resolve())
    except OSError:
        resolved = path
    return "npm" if "node_modules" in resolved else "native_or_other"


class CliManager:
    """Runs only allowlisted npm and built-in updater commands, without a shell."""

    def __init__(self):
        self._operation_lock = threading.Lock()

    @staticmethod
    def _spec(agent: str) -> dict:
        try:
            return CLI_SPECS[agent]
        except KeyError as exc:
            raise CliManagerError("只支持 claude 或 codex") from exc

    @staticmethod
    def _registry(registry: str) -> dict:
        try:
            return NPM_REGISTRIES[registry]
        except KeyError as exc:
            raise CliManagerError("未知 npm 源；只允许 official 或 china") from exc

    @staticmethod
    def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CliManagerError(f"命令超时（{timeout} 秒）") from exc
        except OSError as exc:
            raise CliManagerError(f"无法执行命令：{exc}") from exc

    def tool_status(self, agent: str) -> dict:
        spec = self._spec(agent)
        path = shutil.which(spec["executable"])
        raw_version = ""
        version = None
        error = None
        if path:
            try:
                result = self._run([path, "--version"], timeout=8)
                raw_version = _trim_output(result.stdout + result.stderr, 500)
                version = _version_from_output(raw_version)
                if result.returncode != 0:
                    error = raw_version or f"版本命令退出码 {result.returncode}"
            except CliManagerError as exc:
                error = str(exc)
        return {
            "id": agent,
            "label": spec["label"],
            "package": spec["package"],
            "installed": bool(path),
            "path": path,
            "version": version,
            "version_output": raw_version,
            "install_method": _install_method(path),
            "error": error,
        }

    def status(self) -> dict:
        return {
            "tools": {agent: self.tool_status(agent) for agent in CLI_SPECS},
            "npm_available": shutil.which("npm") is not None,
            "registries": NPM_REGISTRIES,
        }

    def latest_versions(self, registry: str) -> dict:
        registry_spec = self._registry(registry)
        npm = shutil.which("npm")
        if not npm:
            raise CliManagerError("未找到 npm；请先安装 Node.js/npm")

        versions = {}
        for agent, spec in CLI_SPECS.items():
            result = self._run(
                [
                    npm,
                    "view",
                    spec["package"],
                    "version",
                    "--registry",
                    registry_spec["url"],
                ],
                timeout=20,
            )
            output = _trim_output(result.stdout + result.stderr, 1000)
            if result.returncode == 0:
                versions[agent] = {
                    "version": _version_from_output(output),
                    "error": None,
                }
            else:
                versions[agent] = {
                    "version": None,
                    "error": output or f"npm 退出码 {result.returncode}",
                }
        return {
            "registry": registry,
            "third_party": registry_spec["third_party"],
            "versions": versions,
        }

    def manage(self, agent: str, action: str, version: str, registry: str) -> dict:
        spec = self._spec(agent)
        if not self._operation_lock.acquire(blocking=False):
            raise CliManagerError("已有 CLI 安装或更新任务正在运行")
        try:
            if action == "npm_install":
                registry_spec = self._registry(registry)
                npm = shutil.which("npm")
                if not npm:
                    raise CliManagerError("未找到 npm；请先安装 Node.js/npm")
                target = (version or "latest").strip()
                if target != "latest" and not _VERSION_RE.fullmatch(target):
                    raise CliManagerError("版本必须是 latest 或明确版本号，例如 1.2.3")
                command = [
                    npm,
                    "install",
                    "--global",
                    f'{spec["package"]}@{target}',
                    "--registry",
                    registry_spec["url"],
                ]
            elif action == "self_update":
                path = shutil.which(spec["executable"])
                if not path:
                    raise CliManagerError(f'{spec["label"]} 尚未安装')
                command = [path, "update"]
            elif action == "native_install":
                if agent != "claude":
                    raise CliManagerError("原生版本安装目前只支持 Claude Code")
                path = shutil.which(spec["executable"])
                if not path:
                    raise CliManagerError("请先通过 npm 安装 Claude Code，再切换原生版本")
                target = (version or "latest").strip()
                if target not in {"latest", "stable"} and not _VERSION_RE.fullmatch(target):
                    raise CliManagerError(
                        "Claude 原生版本必须是 latest、stable 或明确版本号"
                    )
                command = [path, "install", target]
            else:
                raise CliManagerError("未知操作")

            result = self._run(command, timeout=300)
            output = _trim_output(result.stdout + result.stderr)
            if result.returncode != 0:
                raise CliManagerError(
                    output or f"安装/更新命令退出码 {result.returncode}"
                )
            return {
                "ok": True,
                "agent": agent,
                "action": action,
                "output": output,
                "tool": self.tool_status(agent),
            }
        finally:
            self._operation_lock.release()
