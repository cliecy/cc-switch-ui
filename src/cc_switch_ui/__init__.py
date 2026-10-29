"""CC Switch Web UI —— Claude Code / Codex 多供应商管理面板"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cc-switch-ui")
except PackageNotFoundError:  # 未安装（直接源码运行）时的回退，发版时与 pyproject 同步
    __version__ = "0.6.1"
