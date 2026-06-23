"""
CC Switch Web UI —— CLI 入口点。
"""

import argparse
import atexit
import os
import signal

from .config import CONFIG_PATH
from .server import create_app


def main():
    parser = argparse.ArgumentParser(
        description="CC Switch Web UI —— Claude Code 多供应商 / 多账号管理面板",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="监听端口 (默认 8765)")
    args = parser.parse_args()

    app = create_app()
    claude_proc = app._claude_proc

    def _cleanup_children(*_):
        """app 退出时连带停掉 claude 子进程，避免留下「失联孤儿」。"""
        claude_proc.keepalive = False
        claude_proc.stop()

    # 进程退出 / 被 systemd 或守护脚本 SIGTERM 时，清理 claude 子进程
    atexit.register(_cleanup_children)
    signal.signal(signal.SIGTERM, lambda *a: (_cleanup_children(), os._exit(0)))

    print(f"配置文件: {CONFIG_PATH}")
    print(f"CC Switch Web UI 已启动 →  http://{args.host}:{args.port}")
    # threaded=True 保证 SSE 长连接不阻塞其它请求
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
