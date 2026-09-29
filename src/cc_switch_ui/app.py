"""
CC Switch Web UI —— CLI 入口点。
"""

import argparse
import atexit
import ipaddress
import os
import signal
from pathlib import Path

import cc_switch_ui.config as config
from cc_switch_ui.logutil import setup_logging
from cc_switch_ui.server import create_app


def _is_loopback_host(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _read_auth_token(args, parser):
    """--auth-token 与 --auth-token-file 互斥；file 内容去除首尾空白。"""
    if args.auth_token and args.auth_token_file:
        parser.error("--auth-token 与 --auth-token-file 互斥，只能指定其一")
    if args.auth_token_file:
        return Path(args.auth_token_file).expanduser().read_text(
            encoding="utf-8"
        ).strip()
    return args.auth_token


def main():
    parser = argparse.ArgumentParser(
        description="CC Switch Web UI —— Claude Code / Codex 多供应商管理面板",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="监听端口 (默认 8765)")
    parser.add_argument(
        "--allow-remote", action="store_true",
        help="允许监听非回环地址（配合 --auth-token 使用）",
    )
    parser.add_argument(
        "--allow-cli-management", action="store_true",
        help="允许面板安装/更新 Claude Code 和 Codex CLI（默认关闭）",
    )
    parser.add_argument(
        "--config-dir", default=None,
        help="配置与状态目录（默认 ~/.ccm_config + ~/.cc-switch-ui）",
    )
    parser.add_argument(
        "--auth-token", default=None,
        help="Bearer token 鉴权（设置后所有请求必须携带 token）",
    )
    parser.add_argument(
        "--auth-token-file", default=None,
        help="从文件读取 token（内容去除首尾空白）；与 --auth-token 互斥",
    )
    parser.add_argument(
        "--trust-proxy-loopback", action="store_true",
        help="信任 X-Forwarded-For 首段判断回环（反代部署时）",
    )
    parser.add_argument(
        "--read-only", action="store_true",
        help="只读模式：仅允许查看与终端操作，禁止修改供应商/账号/CLI/配置",
    )
    parser.add_argument(
        "--fs-root", default=None,
        help="目录选择器可浏览的根目录（默认家目录）",
    )
    parser.add_argument(
        "--max-sessions", type=int, default=8,
        help="并行 Agent 会话数上限（默认 8）",
    )
    args = parser.parse_args()

    if args.config_dir:
        config.configure_paths(Path(args.config_dir))
    auth_token = _read_auth_token(args, parser)

    if not _is_loopback_host(args.host) and not args.allow_remote:
        parser.error(
            "拒绝监听非回环地址：请使用 SSH 端口转发；如已配置外部鉴权，"
            "可显式传入 --allow-remote"
        )
    if args.allow_cli_management and not _is_loopback_host(args.host):
        parser.error("CLI 安装/更新只能在回环地址启用；请通过 SSH 端口转发访问")

    setup_logging()

    app = create_app(
        allow_cli_management=args.allow_cli_management,
        auth_token=auth_token,
        trust_proxy_loopback=args.trust_proxy_loopback,
        read_only=args.read_only,
        fs_root=args.fs_root,
        max_sessions=args.max_sessions,
    )
    agent_proc = app._agent_proc

    def _cleanup_children(*_):
        """app 退出时连带停掉 Agent 子进程，避免留下「失联孤儿」。"""
        agent_proc.keepalive = False
        agent_proc.stop()

    # 进程退出 / 被 systemd 或守护脚本 SIGTERM 时，清理 Agent 子进程
    atexit.register(_cleanup_children)
    signal.signal(signal.SIGTERM, lambda *a: (_cleanup_children(), os._exit(0)))

    print(f"配置文件: {config.CONFIG_PATH}")
    if args.allow_remote and not _is_loopback_host(args.host):
        if auth_token:
            print("远程监听已启用 token 鉴权；请确保外层使用 TLS 并妥善保管 token。")
        else:
            print("警告：远程监听未启用内置鉴权，请确保外层已有访问控制和 TLS。")
    if args.read_only:
        print("只读模式：仅可查看状态与操作终端，修改类操作已禁用。")
    if args.allow_cli_management:
        print("CLI 安装/更新功能已启用；仅允许 npm 官方源或显式选择的第三方中国镜像。")
    print(f"CC Switch Web UI 已启动 →  http://{args.host}:{args.port}")
    # threaded=True 保证 SSE 长连接不阻塞其它请求
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
