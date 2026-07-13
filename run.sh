#!/usr/bin/env bash
# CC Switch 守护脚本 —— 无需 root、不依赖 systemd，任意普通用户可用。
#   - setsid 让服务脱离当前终端会话：SSH 断开 / 终端关闭都不会被 SIGHUP 杀掉
#   - while 循环实现崩溃自动重启（app.py 退出后 2 秒拉起）
#   - app 退出时会自行清理它的 Agent 子进程（见 app.py 的 SIGTERM 处理）
#
# 用法: ./run.sh {start|stop|restart|status|log|fg}
set -u
cd "$(dirname "$0")" || exit 1

HOST="${CC_HOST:-127.0.0.1}"
PORT="${CC_PORT:-8765}"
ALLOW_REMOTE="${CC_ALLOW_REMOTE:-0}"
ALLOW_CLI_MANAGEMENT="${CC_ALLOW_CLI_MANAGEMENT:-0}"
PIDFILE=".cc-switch.pid"
LOG="cc-switch.log"

is_running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

start() {
  if is_running; then
    echo "已在运行 (pid $(cat "$PIDFILE"))  →  http://$HOST:$PORT"; return 0
  fi
  case "$HOST" in
    localhost|127.*|::1) ;;
    *) if [ "$ALLOW_REMOTE" != "1" ]; then
         echo "拒绝远程监听：请用 SSH 隧道，或显式设置 CC_ALLOW_REMOTE=1"; return 1
       fi ;;
  esac
  case "$HOST" in
    localhost|127.*|::1) ;;
    *) if [ "$ALLOW_CLI_MANAGEMENT" = "1" ]; then
         echo "拒绝启用 CLI 管理：该功能只能监听回环地址，请使用 SSH 隧道"; return 1
       fi ;;
  esac
  # setsid: 新会话，脱离终端；内部 while 循环：app 崩了自动重启
  setsid bash -c '
    host="$1"; port="$2"; allow_remote="$3"; allow_cli_management="$4"; log="$5"
    args=()
    [ "$allow_remote" = "1" ] && args+=("--allow-remote")
    [ "$allow_cli_management" = "1" ] && args+=("--allow-cli-management")
    while true; do
      uv run cc-switch-ui --host "$host" --port "$port" "${args[@]}" >> "$log" 2>&1
      echo "[$(date "+%F %T")] app.py 退出(code $?)，2 秒后自动重启…" >> "$log"
      sleep 2
    done
  ' _ "$HOST" "$PORT" "$ALLOW_REMOTE" "$ALLOW_CLI_MANAGEMENT" "$LOG" </dev/null >/dev/null 2>&1 &
  echo $! > "$PIDFILE"
  sleep 2
  if is_running; then
    echo "已启动  →  http://$HOST:$PORT   (pid $(cat "$PIDFILE"), 日志: $LOG)"
  else
    echo "启动失败，请看日志: $LOG"; rm -f "$PIDFILE"; return 1
  fi
}

stop() {
  if ! is_running; then echo "未运行"; rm -f "$PIDFILE"; return 0; fi
  local pid; pid="$(cat "$PIDFILE")"
  # 负号 = 杀整个进程组（守护循环 + uv + app.py）；app 收到 SIGTERM 会清理 claude
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  sleep 2
  kill -KILL -- "-$pid" 2>/dev/null
  rm -f "$PIDFILE"
  echo "已停止"
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  if is_running; then echo "运行中 (pid $(cat "$PIDFILE"))  →  http://$HOST:$PORT";
           else echo "未运行"; fi ;;
  log)     tail -n 60 -f "$LOG" ;;
  fg)      args=()
           [ "$ALLOW_REMOTE" = "1" ] && args+=("--allow-remote")
           [ "$ALLOW_CLI_MANAGEMENT" = "1" ] && args+=("--allow-cli-management")
           exec uv run cc-switch-ui --host "$HOST" --port "$PORT" "${args[@]}" ;;  # 前台调试用
  *)       echo "用法: ./run.sh {start|stop|restart|status|log|fg}";
           echo "可用环境变量覆盖: CC_HOST CC_PORT CC_ALLOW_REMOTE CC_ALLOW_CLI_MANAGEMENT";;
esac
