import contextlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN_SH = ROOT / "run.sh"
LOG_LIMIT = 10485760


def _bash(script: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=60,
    )


def _write_bytes(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _write_sparse(path: Path, size: int, fill: bytes = b"C") -> None:
    with open(path, "wb") as f:
        f.write(fill * (size // len(fill)))
        f.write(fill[: size % len(fill)])


def _extract_rotate_log() -> str:
    text = RUN_SH.read_text()
    m = re.search(r"^rotate_log\(\) \{\n.*?^\}", text, re.M | re.S)
    assert m, "rotate_log 函数未找到（run.sh 结构变化？）"
    return m.group(0)


@contextlib.contextmanager
def _live_process(*argv: str):
    proc = subprocess.Popen(argv)
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


class RunShSyntaxTests(unittest.TestCase):
    def test_bash_n(self):
        r = subprocess.run(["bash", "-n", str(RUN_SH)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_guarded_empty_array_expansion_in_both_places(self):
        text = RUN_SH.read_text()
        self.assertEqual(text.count('${args[@]+"${args[@]}"}'), 2)
        self.assertIn('"${CC_CONFIG_DIR:-}"', text)
        self.assertIn("setsid bash -c", text)
        self.assertIn("nohup bash -c", text)


class EmptyArgsExpansionTests(unittest.TestCase):
    def test_guarded_expansion_empty_array_set_u(self):
        script = (
            "set -u\n"
            "args=()\n"
            'printf "ARG:[%s]\\n" ${args[@]+"${args[@]}"}\n'
            "echo done\n"
        )
        r = _bash(script)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ARG:[]", r.stdout)
        self.assertIn("done", r.stdout)

    def test_naive_expansion_fails_on_old_bash(self):
        ver = _bash('echo "${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"').stdout.split()
        major, minor = int(ver[0]), int(ver[1])
        r = _bash('set -u\nargs=()\nprintf "%s" "${args[@]}"\necho ok\n')
        if (major, minor) < (4, 4):
            # bash < 4.4 下 "${args[@]}" 空数组触发 unbound —— 正是守卫要防的
            self.assertNotEqual(r.returncode, 0)


class RotateLogTests(unittest.TestCase):
    def _rotate(self, tmp: Path, log: Path) -> subprocess.CompletedProcess:
        script = f'set -u\nlog="{log}"\n{_extract_rotate_log()}\nrotate_log\n'
        return _bash(script)

    def test_large_log_rotated_to_one(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            log = tmp / "app.log"
            _write_sparse(log, LOG_LIMIT + 1, b"C")
            r = self._rotate(tmp, log)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(log.exists())
            self.assertEqual((tmp / "app.log.1").stat().st_size, LOG_LIMIT + 1)
            self.assertFalse((tmp / "app.log.2").exists())
            self.assertFalse((tmp / "app.log.3").exists())

    def test_chain_shift_one_two_three(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            _write_bytes(tmp / "app.log.1", b"A" * 100)
            _write_bytes(tmp / "app.log.2", b"B" * 100)
            _write_sparse(tmp / "app.log", LOG_LIMIT + 1, b"C")
            r = self._rotate(tmp, tmp / "app.log")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual((tmp / "app.log.1").stat().st_size, LOG_LIMIT + 1)
            self.assertEqual((tmp / "app.log.2").read_bytes(), b"A" * 100)
            self.assertEqual((tmp / "app.log.3").read_bytes(), b"B" * 100)

    def test_at_limit_not_rotated(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            log = tmp / "app.log"
            _write_sparse(log, LOG_LIMIT, b"D")
            r = self._rotate(tmp, log)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(log.exists())
            self.assertFalse((tmp / "app.log.1").exists())

    def test_missing_log_no_error(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            r = self._rotate(tmp, tmp / "app.log")
            self.assertEqual(r.returncode, 0, r.stderr)


class LauncherDetectionTests(unittest.TestCase):
    def test_pick_launcher_matches_host(self):
        expected = "setsid" if shutil.which("setsid") else "nohup"
        r = _bash(f'source "{RUN_SH}" && pick_launcher')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), expected)


class IsRunningTests(unittest.TestCase):
    def _check(self, tmp: Path, pid: str | None) -> str:
        if pid is not None:
            (tmp / ".cc-switch.pid").write_text(pid)
        else:
            (tmp / ".cc-switch.pid").unlink(missing_ok=True)
        r = _bash(f'source "{RUN_SH}" && if is_running; then echo running; else echo stopped; fi', cwd=tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_no_pidfile_stopped(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(self._check(Path(t), None), "stopped")

    def test_pid_reuse_mismatch_stopped(self):
        with tempfile.TemporaryDirectory() as t:
            with _live_process("sleep", "60") as proc:
                self.assertEqual(self._check(Path(t), str(proc.pid)), "stopped")

    def test_matching_command_running(self):
        with tempfile.TemporaryDirectory() as t:
            with _live_process("bash", "-c", "sleep 60; : # cc-switch-ui", "_") as proc:
                self.assertEqual(self._check(Path(t), str(proc.pid)), "running")


if __name__ == "__main__":
    unittest.main()
