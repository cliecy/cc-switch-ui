"""
cc-switch TUI —— curses 终端交互界面：查看 / 切换供应商与账号、启停默认 Agent。

- 零第三方依赖（仅标准库 curses）；
- 与 Web UI 共享 config.py 的全部语义（切换核心、脱敏、原子写、审计日志）；
- 纯函数（build_rows / move_selection / apply_enter）与 UI 壳分离，便于无头测试。

用法：cc-switch-ui tui [--read-only]
"""

from __future__ import annotations

import curses
import locale

from . import config

# --------------------------------------------------------------------------- #
# 纯逻辑层（可测试）
# --------------------------------------------------------------------------- #


def build_rows(state: dict) -> list[dict]:
    """public_state 输出 → 可导航行列表。

    每行: {"kind": "provider"|"account", "provider_id": str,
           "account_id": str|None, "active": bool, "ready": bool, "text": str}
    顺序与配置中 providers 一致；每个 provider 行后紧跟其账号行。
    """
    rows: list[dict] = []
    for pid, p in state.get("providers", {}).items():
        is_current = pid == state.get("current_provider")
        ready = bool(p.get("readiness", {}).get("ready"))
        flag = "●" if is_current else " "
        mark = "✓" if ready else "✗"
        rows.append({
            "kind": "provider",
            "provider_id": pid,
            "account_id": None,
            "active": is_current,
            "ready": ready,
            "text": f"{flag} [{mark}] {p.get('label', pid)}  ({p.get('client', 'claude')})",
        })
        for acc in p.get("accounts", []):
            is_active_acc = acc.get("id") == p.get("active_account")
            acc_mark = "‣" if is_active_acc else " "
            key_disp = acc.get("key_masked") or "（未配置）"
            name_disp = acc.get("name") or acc.get("id", "")
            rows.append({
                "kind": "account",
                "provider_id": pid,
                "account_id": acc.get("id"),
                "active": is_current and is_active_acc,
                "ready": ready,
                "text": f"    {acc_mark} {name_disp}  {key_disp}",
            })
    return rows


def move_selection(rows: list, idx: int, key: str) -> int:
    """键盘导航（纯函数）：up/down/home/end；越界钳制；未知键不动。"""
    n = len(rows)
    if n == 0:
        return 0
    if key == "up":
        return max(0, idx - 1)
    if key == "down":
        return min(n - 1, idx + 1)
    if key == "home":
        return 0
    if key == "end":
        return n - 1
    return idx


def apply_enter(provider_id: str, account_id=None) -> tuple[bool, str]:
    """Enter 行为：切换供应商（在账号行则一并激活该账号）。返回 (ok, message)。"""
    ok, err = config.switch_current(provider_id, account_id)
    if ok:
        target = provider_id if account_id is None else f"{provider_id}:{account_id}"
        config.append_audit("tui_switch", target=target)
        return True, "已切换"
    if err == "unknown_provider":
        return False, "未知供应商"
    if err == "unknown_account":
        return False, "账号不存在"
    return False, err or "切换失败"


# --------------------------------------------------------------------------- #
# Agent 控制（默认会话的 claude/codex 进程）
# --------------------------------------------------------------------------- #


class _AgentCtl:
    """轻封装：默认 Agent 进程的启停与状态行。"""

    def __init__(self):
        from .process import AgentProcess  # 延迟导入，纯逻辑测试不需要 pty

        self.proc = AgentProcess()

    def start(self) -> tuple[bool, str]:
        launch = config.build_launch_for_active("new")
        if not launch.get("ready"):
            return False, launch.get("error", "当前选择未就绪")
        ok, msg = self.proc.start(
            launch.get("env") or {},
            launch.get("label") or "?",
            command=launch.get("command"),
            clear_env=launch.get("clear_env"),
            client=launch.get("client") or "claude",
        )
        return ok, msg

    def stop(self) -> tuple[bool, str]:
        return self.proc.stop()

    def status_text(self) -> str:
        st = self.proc.status()
        if st.get("running"):
            return f"agent: 运行中 pid={st.get('pid')}"
        return "agent: 未运行"


# --------------------------------------------------------------------------- #
# UI 壳
# --------------------------------------------------------------------------- #


def _keyname(key: int) -> str:
    """把 curses 键码归一为移动/动作名。"""
    named = {
        curses.KEY_UP: "up",
        curses.KEY_DOWN: "down",
        curses.KEY_HOME: "home",
        curses.KEY_END: "end",
        curses.KEY_ENTER: "enter",
        10: "enter",
        13: "enter",
    }
    if key in named:
        return named[key]
    if 0 <= key < 256:
        ch = chr(key)
        if ch in "k":
            return "up"
        if ch in "j":
            return "down"
        if ch in "g":
            return "home"
        if ch in "G":
            return "end"
        return ch
    return "?"


def _init_styles() -> dict:
    """初始化 color pair；不支持颜色时返回纯转义 fallback。"""
    if not (hasattr(curses, "has_colors") and curses.has_colors()):
        return {"current": curses.A_REVERSE, "active": curses.A_BOLD, "dim": curses.A_DIM}
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_GREEN, -1)
        return {
            "current": curses.A_REVERSE | curses.color_pair(1),
            "active": curses.color_pair(3) | curses.A_BOLD,
            "dim": curses.color_pair(2),
        }
    except curses.error:
        return {"current": curses.A_REVERSE, "active": curses.A_BOLD, "dim": curses.A_DIM}


def _render(stdscr, rows, idx, read_only, agent: _AgentCtl, message: str, styles: dict):
    """整屏重绘。行数超屏时安全截断。"""
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()

    def put(y, x, text, style=0):
        if 0 <= y < max_y and x < max_x:
            try:
                stdscr.addstr(y, x, text[: max(0, max_x - x - 1)], style)
            except curses.error:
                pass

    put(0, 0, f" cc-switch TUI  ·  {config.CONFIG_PATH}  ·  {config.STATE_DIR}", styles["dim"])
    body = rows[: max(0, max_y - 3)]
    for i, row in enumerate(body):
        style = styles["active"] if row["active"] else 0
        if i == idx:
            style = styles["current"]
        put(2 + i, 0, " " + row["text"], style)
    help_line = " ↑/↓ j/k 移动 · Enter 切换 · s 启动 · x 停止 · r 刷新 · q 退出"
    if read_only:
        help_line = " [只读] 查看专用 · q 退出"
    put(max_y - 2, 0, help_line, styles["dim"])
    put(max_y - 1, 0, f" {agent.status_text()}  {message}", styles["active"] if message else styles["dim"])
    try:
        stdscr.refresh()
    except curses.error:
        pass


def _tui_main(stdscr, *, read_only=False):
    curses.curs_set(0)
    styles = _init_styles()
    agent = _AgentCtl()
    idx = 0
    message = ""
    while True:
        rows = build_rows(config.public_state())
        idx = min(idx, max(0, len(rows) - 1))
        _render(stdscr, rows, idx, read_only, agent, message, styles)
        message = ""
        key = stdscr.getch()
        if key in (ord("q"), ord("Q"), 27):
            return
        name = _keyname(key)
        idx = move_selection(rows, idx, name)
        if name == "enter":
            if read_only:
                message = "只读模式：禁止切换"
                continue
            if not rows:
                continue
            row = rows[idx]
            ok, msg = apply_enter(row["provider_id"], row["account_id"])
            message = ("✓ " if ok else "✗ ") + msg
        elif name == "s":
            if read_only:
                message = "只读模式：禁止启动"
                continue
            ok, msg = agent.start()
            message = ("✓ " if ok else "✗ ") + msg
        elif name == "x":
            if read_only:
                message = "只读模式：禁止停止"
                continue
            ok, msg = agent.stop()
            message = ("✓ " if ok else "✗ ") + msg
        elif name == "r":
            message = "已刷新"


def run_tui(*, read_only=False):
    """启动 TUI（curses wrapper）。退出时清理 Agent 子进程。"""
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    import atexit

    ctl = _AgentCtl()

    def _cleanup_children(*_):
        ctl.proc.keepalive = False
        ctl.proc.stop()

    atexit.register(_cleanup_children)
    try:
        curses.wrapper(lambda stdscr: _tui_main(stdscr, read_only=read_only))
    finally:
        _cleanup_children()