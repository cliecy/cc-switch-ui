#!/usr/bin/env python3
"""
CC Switch Web UI —— 兼容入口（指向 src/cc_switch_ui/app.py）。

推荐用法（pip 安装后）：
    cc-switch-ui --host 127.0.0.1 --port 8765

也可继续用：
    uv run app.py
    python app.py
"""

from cc_switch_ui.app import main

if __name__ == "__main__":
    main()
