"""
CC Switch Web UI —— CLI 入口点。
"""

import argparse
import atexit
import ipaddress
import os
import signal

from .config import CONFIG_PATH
from .server import create_app


def _is_loopback_host(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main():
    parser = argparse.ArgumentParser(
        description="CC Switch Web UI —— Claude Code / Codex 多供应商管理面板",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="监听端口 (默认 8765)")
    parser.add_argument(
        "--allow-remote", action="store_true",
        help="允许监听非回环地址（无内置鉴权；仅应在受保护网络或反向代理后使用）",
    )
    parser.add_argument(
        "--allow-cli-management", action="store_true",
        help="允许面板安装/更新 Claude Code 和 Codex CLI（默认关闭）",
    )
    args = parser.parse_args()

    if not _is_loopback_host(args.host) and not args.allow_remote:
        parser.error(
            "拒绝监听非回环地址：请使用 SSH 端口转发；如已配置外部鉴权，"
            "可显式传入 --allow-remote"
        )
    if args.allow_cli_management and not _is_loopback_host(args.host):
        parser.error("CLI 安装/更新只能在回环地址启用；请通过 SSH 端口转发访问")

    app = create_app(allow_cli_management=args.allow_cli_management)
    agent_proc = app._agent_proc

    def _cleanup_children(*_):
        """app 退出时连带停掉 Agent 子进程，避免留下「失联孤儿」。"""
        agent_proc.keepalive = False
        agent_proc.stop()

    # 进程退出 / 被 systemd 或守护脚本 SIGTERM 时，清理 Agent 子进程
    atexit.register(_cleanup_children)
    signal.signal(signal.SIGTERM, lambda *a: (_cleanup_children(), os._exit(0)))

    print(f"配置文件: {CONFIG_PATH}")
    if args.allow_remote and not _is_loopback_host(args.host):
        print("警告：远程监听未启用内置鉴权，请确保外层已有访问控制和 TLS。")
    if args.allow_cli_management:
        print("CLI 安装/更新功能已启用；仅允许 npm 官方源或显式选择的第三方中国镜像。")
    print(f"CC Switch Web UI 已启动 →  http://{args.host}:{args.port}")
    # threaded=True 保证 SSE 长连接不阻塞其它请求
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
