#!/usr/bin/env python3
"""listener 进程(persistent Monitor 内运行):`python3 bin/listener.py <binding_id>`。
职责见 lib/listener_core.py;本文件只做真实依赖装配 + 主循环。"""
import fcntl
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from lib import constants, db, paths, procs, util  # noqa: E402
from lib.clock import SystemClock  # noqa: E402
from lib.listener_core import ListenerCore  # noqa: E402


def daemon_alive_probe():
    lock = paths.lock_path()
    if not lock.exists():
        return False
    fd = os.open(str(lock), os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True  # 锁被 daemon 长持
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def ensure_daemon():
    ctl = paths.skill_root() / "bin" / "bridgectl.py"
    subprocess.Popen(
        [sys.executable, str(ctl), "ensure-daemon"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)


def printer(s):
    print(s, flush=True)


def main():
    if len(sys.argv) < 2:
        printer(util.jdumps({"type": "farewell", "code": "bad-args"}))
        return 0
    binding_id = sys.argv[1]
    dbf = paths.db_path()
    if not dbf.exists():
        printer(util.jdumps({"type": "farewell", "code": "no-db"}))
        return 0
    conn = db.connect(dbf, busy_timeout_ms=constants.BUSY_TIMEOUT_LISTENER_MS)
    prober = procs.SystemProber()
    me_pid = os.getpid()
    ident = procs.self_identity(prober, me_pid)
    me_start = ident[1] if ident else f"unknown-{me_pid}"
    core = ListenerCore(conn, binding_id, SystemClock(), prober,
                        me_pid=me_pid, me_start=me_start, printer=printer,
                        daemon_alive_probe=daemon_alive_probe,
                        ensure_daemon=ensure_daemon)
    consecutive_errors = 0
    while True:
        try:
            if core.step() == "exit":
                break
            consecutive_errors = 0
        except BrokenPipeError:
            break  # Monitor 管道没了,进程无意义
        except Exception:
            consecutive_errors += 1
            if consecutive_errors >= 30:
                break  # 持续异常(如 DB 损坏):退出,由 daemon 判死收口
        time.sleep(constants.LISTENER_TICK_S)
    return 0


if __name__ == "__main__":
    sys.exit(main())
