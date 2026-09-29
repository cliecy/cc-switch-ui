"""cc-switch —— cc-switch-ui 的命令行客户端。

通过 HTTP 与 cc-switch-ui 服务通信（urllib，无额外依赖）。
全局参数：--url（服务地址，默认 http://127.0.0.1:8765）、--token（Bearer 鉴权）。
任何非 2xx 或 body ok:false 都打印 `code: message` 到 stderr 并以退出码 1 结束。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

_TIMEOUT = 30


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _request(base_url: str, path: str, *, method: str = "GET",
             body: dict | None = None, token: str | None = None):
    """发送请求并返回解析后的 JSON body；失败时打印错误并 exit(1)。"""
    url = base_url.rstrip("/") + path
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    except urllib.error.URLError as e:
        print(f"network: 无法连接 {base_url}（{e.reason}）", file=sys.stderr)
        sys.exit(1)

    payload = None
    if raw:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            payload = None

    if status >= 400:
        code, message = _error_fields(payload) or ("http_error", f"HTTP {status}")
        print(f"{code}: {message}", file=sys.stderr)
        sys.exit(1)
    if isinstance(payload, dict) and payload.get("ok") is False:
        code, message = _error_fields(payload) or ("error", "请求失败")
        print(f"{code}: {message}", file=sys.stderr)
        sys.exit(1)
    return payload if payload is not None else {}


def _error_fields(payload) -> tuple[str, str] | None:
    """从错误响应体提取 (code, message)。"""
    if not isinstance(payload, dict):
        return None
    message = payload.get("error") or payload.get("message") or ""
    code = payload.get("code") or ""
    if not message and not code:
        return None
    return (code or "error", message or code)


# --------------------------------------------------------------------------- #
# 输出工具
# --------------------------------------------------------------------------- #

def _print_table(headers: list[str], rows: list[list]) -> None:
    cells = [[str(c) for c in row] for row in rows]
    widths = [
        max([len(h)] + [len(row[i]) for row in cells])
        for i, h in enumerate(headers)
    ]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    print(line)
    for row in cells:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())


def _yes_no(value) -> str:
    return "yes" if value else "no"


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #

def _cmd_status(args) -> None:
    state = _request(args.url, "/api/state", token=args.token)
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return
    launch = state.get("selected_launch", {})
    agent = state.get("agent_status", {}) or {}
    running = bool(agent.get("running"))
    pid = agent.get("pid")
    try:
        sessions = _request(args.url, "/api/sessions", token=args.token)
        count = len(sessions.get("sessions", []))
    except SystemExit:
        count = None

    ready = launch.get("ready")
    ready_txt = "ready" if ready else f"not ready（缺少 {', '.join(launch.get('missing', []) or ['未知'])}）"
    account = launch.get("account_name") or "未配置账号"
    print(f"Provider: {launch.get('provider_label', state.get('current_provider'))} "
          f"({state.get('current_provider')})")
    print(f"Account:  {account} [{ready_txt}]")
    if running:
        print(f"Agent:    running (pid {pid}, uptime {int(agent.get('uptime', 0))}s)")
    else:
        print("Agent:    not running")
    if count is not None:
        print(f"Sessions: {count}")


def _cmd_use(args) -> None:
    body = _request(args.url, "/api/provider/switch", method="POST",
                    body={"provider": args.provider}, token=args.token)
    message = body.get("message") if isinstance(body, dict) else None
    print(f"已切换到 {args.provider}" + (f"（{message}）" if message else ""))


def _cmd_sessions(args) -> None:
    body = _request(args.url, "/api/sessions", token=args.token)
    rows = [
        [
            s.get("id", ""),
            s.get("name") or s.get("id", ""),
            s.get("provider_id") or s.get("provider_label") or "",
            _yes_no(s.get("running")),
            s.get("pid") or "-",
            int(s.get("uptime") or 0),
        ]
        for s in body.get("sessions", [])
    ]
    _print_table(["ID", "NAME", "PROVIDER", "RUNNING", "PID", "UPTIME(s)"], rows)


def _cmd_session_create(args) -> None:
    body_post = {"name": args.name}
    if args.cwd:
        body_post["cwd"] = args.cwd
    if args.provider:
        body_post["provider_id"] = args.provider
    if args.profile:
        body_post["profile_id"] = args.profile
    result = _request(args.url, "/api/sessions", method="POST",
                      body=body_post, token=args.token)
    session = result.get("session", {}) if isinstance(result, dict) else {}
    print(f"会话已创建: {session.get('id', '')}（{args.name}）")


def _resolve_sid(args, value: str) -> str:
    """先按会话 id 查；查不到再按档案名查（取其 id）。"""
    body = _request(args.url, "/api/sessions", token=args.token)
    for s in body.get("sessions", []):
        if s.get("id") == value:
            return value
    profiles = _request(args.url, "/api/profiles", token=args.token)
    for p in profiles.get("profiles", []):
        if p.get("name") == value:
            return p.get("id", "")
    print(f"validation: 找不到会话或档案「{value}」", file=sys.stderr)
    sys.exit(1)


def _cmd_session_start(args) -> None:
    sid = _resolve_sid(args, args.target)
    body_post: dict = {}
    if args.cwd:
        body_post["cwd"] = args.cwd
    if args.mode:
        body_post["session_mode"] = args.mode
    result = _request(args.url, f"/api/sessions/{urllib.parse.quote(sid, safe='')}/start",
                      method="POST", body=body_post, token=args.token)
    message = result.get("message", "") if isinstance(result, dict) else ""
    print(f"会话 {sid} 已启动" + (f"（{message}）" if message else ""))


def _cmd_session_action(action: str):
    def _impl(args) -> None:
        sid = args.sid
        _request(args.url, f"/api/sessions/{urllib.parse.quote(sid, safe='')}/{action}",
                 method="POST", body={}, token=args.token)
        verb = "停止" if action == "stop" else "重启"
        print(f"会话 {sid} 已{verb}")
    return _impl


def _cmd_account_ls(args) -> None:
    state = _request(args.url, "/api/state", token=args.token)
    providers = state.get("providers", {})
    rows = []
    for pid, p in providers.items():
        if args.provider and pid != args.provider:
            continue
        active = p.get("active_account")
        for acc in p.get("accounts", []):
            rows.append([
                acc.get("name", ""),
                acc.get("id", ""),
                _yes_no(acc.get("id") == active),
                _yes_no(acc.get("shared")),
            ])
    _print_table(["NAME", "ID", "ACTIVE", "SHARED"], rows)


def _cmd_account_add(args) -> None:
    result = _request(args.url, "/api/account", method="POST",
                      body={"provider": args.provider, "name": args.name,
                            "api_key": args.key},
                      token=args.token)
    acc_id = result.get("id", "") if isinstance(result, dict) else ""
    print(f"账号已添加: {args.name}（id {acc_id}，provider {args.provider}）")


def _cmd_account_activate(args) -> None:
    _request(args.url, "/api/account/activate", method="POST",
             body={"provider": args.provider, "account_id": args.account_id},
             token=args.token)
    print(f"已激活账号 {args.account_id}（provider {args.provider}）")


def _cmd_account_rm(args) -> None:
    if not args.yes:
        if not sys.stdin.isatty():
            print("validation: 非交互环境删除账号必须加 --yes", file=sys.stderr)
            sys.exit(1)
        answer = input(f"确认删除账号 {args.account_id}（provider {args.provider}）？[y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("已取消")
            return
    _request(args.url,
             f"/api/account/{urllib.parse.quote(args.account_id, safe='')}?provider="
             f"{urllib.parse.quote(args.provider, safe='')}",
             method="DELETE", token=args.token)
    print(f"账号 {args.account_id} 已删除（7 天内可恢复）")


def _cmd_export(args) -> None:
    path = "/api/config/export"
    if args.include_keys:
        path += "?include_keys=1"
    body = _request(args.url, path, token=args.token)
    print(json.dumps(body, ensure_ascii=False, indent=2))


def _cmd_import(args) -> None:
    try:
        with open(args.file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"validation: 无法读取导入文件（{e}）", file=sys.stderr)
        sys.exit(1)
    # 兼容两种输入：裸配置 或 export 输出的 {ok, config}
    config = data.get("config") if isinstance(data, dict) and isinstance(data.get("config"), dict) else data
    _request(args.url, "/api/config/import", method="POST",
             body={"config": config, "mode": "replace" if args.replace else "merge"},
             token=args.token)
    print(f"配置已导入（mode={'replace' if args.replace else 'merge'}）")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cc-switch",
        description="cc-switch-ui 命令行客户端",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8765",
                        help="服务地址（默认 http://127.0.0.1:8765）")
    parser.add_argument("--token", default=None, help="Bearer 鉴权 token")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="查看当前状态")
    p.add_argument("--json", action="store_true", help="输出原始 JSON")
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("use", help="切换当前供应商")
    p.add_argument("provider")
    p.set_defaults(func=_cmd_use)

    p = sub.add_parser("sessions", help="列出全部会话")
    p.set_defaults(func=_cmd_sessions)

    p = sub.add_parser("session", help="会话操作")
    sess = p.add_subparsers(dest="session_command", required=True)
    q = sess.add_parser("create", help="新建会话")
    q.add_argument("name")
    q.add_argument("--cwd", default=None)
    q.add_argument("--provider", default=None)
    q.add_argument("--profile", default=None, help="从档案物化（发 profile_id）")
    q.set_defaults(func=_cmd_session_create)
    q = sess.add_parser("start", help="启动会话（会话 id 或档案名）")
    q.add_argument("target", metavar="sid|profile")
    q.add_argument("--cwd", default=None)
    q.add_argument("--mode", choices=("new", "continue", "resume"), default=None)
    q.set_defaults(func=_cmd_session_start)
    q = sess.add_parser("stop", help="停止会话")
    q.add_argument("sid")
    q.set_defaults(func=_cmd_session_action("stop"))
    q = sess.add_parser("restart", help="重启会话")
    q.add_argument("sid")
    q.set_defaults(func=_cmd_session_action("restart"))

    p = sub.add_parser("account", help="账号操作")
    acc = p.add_subparsers(dest="account_command", required=True)
    q = acc.add_parser("ls", help="列出账号")
    q.add_argument("--provider", default=None)
    q.set_defaults(func=_cmd_account_ls)
    q = acc.add_parser("add", help="添加账号")
    q.add_argument("provider")
    q.add_argument("name")
    q.add_argument("key")
    q.set_defaults(func=_cmd_account_add)
    q = acc.add_parser("activate", help="激活账号")
    q.add_argument("provider")
    q.add_argument("account_id")
    q.set_defaults(func=_cmd_account_activate)
    q = acc.add_parser("rm", help="删除账号（进回收站，7 天可恢复）")
    q.add_argument("provider")
    q.add_argument("account_id")
    q.add_argument("--yes", action="store_true", help="跳过二次确认")
    q.set_defaults(func=_cmd_account_rm)

    p = sub.add_parser("export", help="导出配置到 stdout")
    p.add_argument("--include-keys", action="store_true", help="包含原始密钥")
    p.set_defaults(func=_cmd_export)

    p = sub.add_parser("import", help="从文件导入配置")
    p.add_argument("file")
    p.add_argument("--replace", action="store_true", help="整体替换（服务端会先备份）")
    p.set_defaults(func=_cmd_import)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
