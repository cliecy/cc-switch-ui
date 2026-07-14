"""
Agent 进程管理（pty + SSE）。
通过伪终端运行 Claude Code 或 Codex，提供实时输出广播、看门狗、窗口大小调整。
"""

import codecs
import fcntl
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time


class AgentProcess:
    def __init__(self):
        self.proc = None
        self.master_fd = None
        self.started_at = None
        self.reader_thread = None
        self.subscribers = set()  # SSE 队列集合
        self.lock = threading.Lock()
        self.lifecycle_lock = threading.RLock()
        self.provider_label = None
        self.client = None
        self.last_launch = None   # 上次启动参数，供「重启」/看门狗复用
        self.keepalive = False    # 看门狗开关：意外退出后自动重启
        self.restart_count = 0    # 看门狗已自动重启次数
        self.last_exit_code = None
        self._stopping = False    # 区分「用户主动停止」与「意外退出」
        self._fast_fail = 0       # 连续快速失败计数，触发熔断
        self._last_env = None
        self._last_label = None
        self._last_command = None
        self._last_clear_env = None
        self._last_client = None
        self._last_launch_snapshot = None
        self.launch_snapshot = None

    # ---- 状态 ----
    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def status(self):
        running = self.is_running()
        return {
            "running": running,
            "pid": self.proc.pid if running else None,
            "started_at": self.started_at,
            "uptime": (time.time() - self.started_at) if (running and self.started_at) else 0,
            "provider": self.provider_label,
            "client": self.client,
            "exit_code": (self.proc.poll() if self.proc and not running else None),
            "keepalive": self.keepalive,
            "restart_count": self.restart_count,
            "last_exit_code": self.last_exit_code,
            "launch": dict(self.launch_snapshot) if running and self.launch_snapshot else None,
        }

    # ---- 订阅/广播 ----
    def subscribe(self):
        import queue

        q = queue.Queue(maxsize=1000)
        with self.lock:
            # 不重放历史缓冲：TUI 输出依赖光标定位 / 备用屏，旧字节无法离线重放，
            # 重放只会让(重)连接的客户端画面错乱、前后会话叠在一起。新订阅者只接实时流
            # （EventSource 网络抖动会自动重连，每次重连都会重新走到这里）。
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subscribers.discard(q)

    def _broadcast(self, text):
        with self.lock:
            dead = []
            for q in self.subscribers:
                try:
                    q.put_nowait(text)
                except Exception:
                    dead.append(q)
            for q in dead:
                self.subscribers.discard(q)

    # ---- 读取线程 ----
    def _reader(self, fd, proc):
        # 增量解码：UTF-8 多字节字符（TUI 框线 ─│└、中文、emoji）可能被 4096
        # 字节的读取边界切断，逐块独立 decode 会把切断处变成乱码。增量解码器
        # 会把读到一半的尾字节留到下一块拼回来。
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            try:
                r, _, _ = select.select([fd], [], [], 0.5)
                if r:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                    text = decoder.decode(data)
                    if text:
                        self._broadcast(text)
            except (OSError, ValueError):
                break
        tail = decoder.decode(b"", final=True)
        if tail:
            self._broadcast(tail)
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            exit_code = proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            exit_code = proc.poll()
        with self.lifecycle_lock:
            is_current = self.proc is proc
            if is_current:
                if self.master_fd == fd:
                    self.master_fd = None
                self.last_exit_code = exit_code
        if is_current:
            self._broadcast(f"\n[进程已退出 · code={exit_code}]\n")
            self._on_exit(proc)

    # ---- 看门狗：意外退出后自动重启 ----
    def _on_exit(self, proc):
        with self.lifecycle_lock:
            if proc is not self.proc or self._stopping or not self.keepalive:
                return
        ran = time.time() - (self.started_at or time.time())
        # 熔断：进程启动后 <5 秒就挂，多半是 key/网络问题，连续 5 次则停手
        self._fast_fail = self._fast_fail + 1 if ran < 5 else 0
        if self._fast_fail >= 5:
            self.keepalive = False
            self._broadcast(
                "\n[看门狗] 连续 5 次快速失败，已关闭自动重启。"
                "请检查 API Key / 网络后手动启动。\n"
            )
            return
        self.restart_count += 1
        self._broadcast(f"\n[看门狗] 3 秒后自动重启（第 {self.restart_count} 次）…\n")
        time.sleep(3)
        if self._stopping or not self.keepalive:
            return
        last = self.last_launch or {}
        self.start(
            self._last_env or {}, self._last_label or "?",
            rows=last.get("rows", 24), cols=last.get("cols", 80),
            cwd=last.get("cwd"), command=self._last_command,
            clear_env=self._last_clear_env, client=self._last_client or "claude",
            launch_snapshot=self._last_launch_snapshot,
        )

    # ---- 终端窗口大小 ----
    def set_winsize(self, rows, cols):
        with self.lifecycle_lock:
            fd = self.master_fd
        if fd is None:
            return
        try:
            winsize = struct.pack("HHHH", int(rows), int(cols), 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except (OSError, ValueError):
            pass

    # ---- 生命周期 ----
    def start(
        self, env_extra, provider_label, args=None, rows=24, cols=80, cwd=None,
        command=None, clear_env=None, client="claude", launch_snapshot=None,
    ):
        with self.lifecycle_lock:
            if self.is_running():
                return False, "Agent 进程已在运行"

            cmd = list(command or (["claude"] + (args or [])))
            executable = cmd[0] if cmd else ""
            if not executable or not shutil.which(executable):
                return False, f"未找到 {executable or 'agent'} 命令，请先安装对应 CLI"

            if cwd:
                cwd = os.path.expanduser(cwd)
                if not os.path.isdir(cwd):
                    return False, f"工作目录不存在: {cwd}"

            env = os.environ.copy()
            for key in clear_env or ():
                env.pop(key, None)
            env.update(env_extra)
            # 关闭分页器，避免输出卡在 pager 里
            env.setdefault("PAGER", "cat")

            master, slave = pty.openpty()
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env=env,
                    cwd=cwd or None,
                    start_new_session=True,
                    close_fds=True,
                )
            except Exception as e:  # noqa: BLE001
                os.close(master)
                os.close(slave)
                return False, f"启动失败: {e}"

            os.close(slave)
            self.proc = proc
            self.master_fd = master
            self.set_winsize(rows, cols)  # 按前端终端尺寸初始化，避免 TUI 错位
            self.started_at = time.time()
            self.provider_label = provider_label
            self.client = client
            self._last_env = dict(env_extra)
            self._last_label = provider_label
            self._last_command = list(cmd)
            self._last_clear_env = tuple(clear_env or ())
            self._last_client = client
            snapshot = dict(launch_snapshot or {})
            snapshot.setdefault("provider_label", provider_label)
            snapshot.setdefault("client", client)
            snapshot.setdefault("cwd", cwd or os.getcwd())
            self.launch_snapshot = snapshot
            self._last_launch_snapshot = dict(snapshot)
            self._stopping = False
            self._broadcast(f"[启动 {client} · 供应商: {provider_label}]\n")

        self.reader_thread = threading.Thread(
            target=self._reader, args=(master, proc), daemon=True
        )
        self.reader_thread.start()
        return True, "已启动"

    def send_input(self, text):
        with self.lifecycle_lock:
            fd = self.master_fd
            running = self.is_running()
        if not running or fd is None:
            return False, "进程未运行"
        try:
            os.write(fd, text.encode("utf-8"))
            return True, "ok"
        except OSError as e:
            return False, str(e)

    def stop(self):
        with self.lifecycle_lock:
            self._stopping = True  # 主动停止，告知看门狗不要自动重启
            proc = self.proc
        if proc is None or proc.poll() is not None:
            return False, "进程未运行"
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            for _ in range(20):
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        # master_fd is owned by the reader thread. Keeping it open until that
        # thread exits prevents its descriptor number being reused by a new PTY.
        return True, "已停止"


# Backward-compatible import for existing integrations.
ClaudeProcess = AgentProcess
