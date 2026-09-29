#!/usr/bin/env bash
# CC Switch 守护脚本 —— 无需 root、不依赖 systemd，任意普通用户可用。
#   - 有 setsid 时用 setsid 让服务脱离当前终端会话：SSH 断开 / 终端关闭都不会被 SIGHUP 杀掉
#   - 无 setsid（如 macOS）回退 nohup：免 SIGHUP，SSH 断开同样存活；
#     内层 trap 保证 TERM 传导给 app 子进程，stop 仍能杀掉
#   - 内层 while 循环实现崩溃自动重启（app 退出后 2 秒拉起）
#   - 日志超过 10MB 自动轮转（保留 3 份）
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

is_running() {
  local pid
  [ -f "$PIDFILE" ] || return 1
  pid="$(cat "$PIDFILE")" || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # pid 复用校验：命令行必须含 cc-switch 字样，防止误判已复用的 pid
  ps -p "$pid" -o command= | grep -q "cc-switch"
}

# 脱离终端的启动方式：有 setsid 用 setsid（新会话），否则 nohup（免 SIGHUP）
pick_launcher() {
  if command -v setsid >/dev/null 2>&1; then echo setsid; else echo nohup; fi
}

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
  # 内层脚本：崩溃自动重启 + 日志轮转 + TERM/INT 传导给 app 子进程
  local inner
  inner=$(cat <<'INNER_SCRIPT'
host="$1"; port="$2"; allow_remote="$3"; allow_cli_management="$4"; cfgdir="$5"; log="$6"
args=()
[ "$allow_remote" = "1" ] && args+=("--allow-remote")
[ "$allow_cli_management" = "1" ] && args+=("--allow-cli-management")
[ -n "$cfgdir" ] && args+=("--config-dir" "$cfgdir")
rotate_log() {
  local size
  size=$(stat -f%z "$log" 2>/dev/null || stat -c%s "$log" 2>/dev/null || echo 0)
  if [ "$size" -gt 10485760 ]; then
    [ -f "$log.2" ] && mv "$log.2" "$log.3"
    [ -f "$log.1" ] && mv "$log.1" "$log.2"
    mv "$log" "$log.1"
  fi
}
stopping=0; child=
trap 'stopping=1; [ -n "$child" ] && kill "$child" 2>/dev/null' TERM INT
while true; do
  rotate_log
  uv run cc-switch-ui --host "$host" --port "$port" ${args[@]+"${args[@]}"} >> "$log" 2>&1 &
  child=$!
  wait "$child"; code=$?
  [ "$stopping" = "1" ] && exit 0
  echo "[$(date "+%F %T")] app 退出(code $code)，2 秒后自动重启…" >> "$log"; sleep 2
done
INNER_SCRIPT
  )
  if [ "$(pick_launcher)" = "setsid" ]; then
    setsid bash -c "$inner" _ "$HOST" "$PORT" "$ALLOW_REMOTE" "$ALLOW_CLI_MANAGEMENT" "${CC_CONFIG_DIR:-}" "$LOG" </dev/null >/dev/null 2>&1 &
  else
    nohup bash -c "$inner" _ "$HOST" "$PORT" "$ALLOW_REMOTE" "$ALLOW_CLI_MANAGEMENT" "${CC_CONFIG_DIR:-}" "$LOG" </dev/null >/dev/null 2>&1 & disown
  fi
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
  # 无 setsid 时进程组不存在，回退为杀守护循环本身（其 trap 会传导 TERM 给 app）
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  sleep 2
  kill -KILL -- "-$pid" 2>/dev/null
  rm -f "$PIDFILE"
  echo "已停止"
}

# 被 source 时只定义函数，不执行分发（供单元测试使用）
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
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
           exec uv run cc-switch-ui --host "$HOST" --port "$PORT" ${args[@]+"${args[@]}"} ;;  # 前台调试用
  *)       echo "用法: ./run.sh {start|stop|restart|status|log|fg}";
           echo "可用环境变量覆盖: CC_HOST CC_PORT CC_ALLOW_REMOTE CC_ALLOW_CLI_MANAGEMENT CC_CONFIG_DIR";;
esac
fi
