"""修复项5:ensure_daemon 挂死恢复(锁被持有但心跳陈旧→pid+start 精确匹配 SIGTERM→接管);
listener 探活升级为 锁+心跳新鲜;新拉起要求 last_loop_at 晚于 spawn。"""
import signal

from tests.conftest import CHAT
from tests.helpers import FakeClock, FakeProber
from lib import ctl, db as dbmod
from lib.ctl import DaemonSupervisor, HUNG_THRESHOLD_MS

DPID = 5555
DSTART = "Tue Jul 15 00:00:00 2026"


class World:
    """模拟 锁/daemon_state/进程 的小世界。"""

    def __init__(self, clock, held=False, last_loop=None, pid=DPID, pstart=DSTART):
        self.clock = clock
        self.held = held
        self.last_loop = last_loop
        self.pid = pid
        self.pstart = pstart
        self.kills = []
        self.spawns = 0
        self.prober = FakeProber()
        if pid is not None:
            self.prober.set(pid, 1, pstart, "python3")

    def lock_held(self):
        return self.held

    def read_state(self):
        return {"last_loop_at": self.last_loop, "daemon_pid": self.pid,
                "daemon_proc_start": self.pstart}

    def kill(self, pid, sig):
        self.kills.append((pid, sig))
        self.held = False  # SIGTERM 后 daemon 退出释放锁
        self.prober.remove(pid)

    def spawn(self):
        self.spawns += 1
        self.held = True
        self.last_loop = self.clock.wall_ms() + 1  # 新 daemon 心跳晚于 spawn

    def supervisor(self, **kw):
        return DaemonSupervisor(
            lock_held=self.lock_held, read_state=self.read_state,
            spawn=self.spawn, kill=self.kill, prober=self.prober,
            now_ms=self.clock.wall_ms, sleep=lambda s: None, **kw)


def test_not_held_spawns_and_requires_fresh_heartbeat():
    clock = FakeClock()
    w = World(clock, held=False, last_loop=None)
    assert w.supervisor().ensure() == "started"
    assert w.spawns == 1 and w.kills == []


def test_held_fresh_is_running():
    clock = FakeClock()
    w = World(clock, held=True, last_loop=clock.wall_ms() - 1000)
    assert w.supervisor().ensure() == "running"
    assert w.spawns == 0 and w.kills == []


def test_hung_daemon_precise_kill_then_recover():
    clock = FakeClock()
    w = World(clock, held=True, last_loop=clock.wall_ms() - HUNG_THRESHOLD_MS - 1)
    assert w.supervisor().ensure() == "recovered"
    assert w.kills == [(DPID, signal.SIGTERM)]  # pid+start 精确匹配后才 SIGTERM
    assert w.spawns == 1


def test_hung_but_identity_mismatch_no_kill():
    clock = FakeClock()
    w = World(clock, held=True, last_loop=clock.wall_ms() - HUNG_THRESHOLD_MS - 1)
    w.prober.set(DPID, 1, "DIFFERENT-START", "python3")  # pid 复用:身份不匹配
    assert w.supervisor().ensure() == "failed"
    assert w.kills == [] and w.spawns == 0


def test_hung_without_recorded_identity_no_kill():
    clock = FakeClock()
    w = World(clock, held=True, last_loop=clock.wall_ms() - HUNG_THRESHOLD_MS - 1,
              pid=None, pstart=None)
    assert w.supervisor().ensure() == "failed"
    assert w.kills == []


def test_stale_spawn_heartbeat_not_accepted():
    """新拉起要求 last_loop_at 晚于本次 spawn(旧残留心跳不算 ready)。"""
    clock = FakeClock()
    w = World(clock, held=False, last_loop=None)

    def bad_spawn():
        w.spawns += 1
        w.held = True
        w.last_loop = clock.wall_ms() - 999_999  # 陈旧心跳(spawn 前的残留)

    w.spawn = bad_spawn
    assert w.supervisor(wait_s=1).ensure() == "failed"


def test_daemon_healthy_needs_lock_and_fresh_heartbeat(env, monkeypatch):
    monkeypatch.setattr(ctl, "daemon_lock_held", lambda: True)
    dbmod.set_state(env.conn, "last_loop_at", env.clock.wall_ms() - 1000)
    assert ctl.daemon_healthy(env.conn, now_ms=env.clock.wall_ms()) is True
    dbmod.set_state(env.conn, "last_loop_at",
                    env.clock.wall_ms() - HUNG_THRESHOLD_MS - 1)
    assert ctl.daemon_healthy(env.conn, now_ms=env.clock.wall_ms()) is False
    monkeypatch.setattr(ctl, "daemon_lock_held", lambda: False)
    dbmod.set_state(env.conn, "last_loop_at", env.clock.wall_ms())
    assert ctl.daemon_healthy(env.conn, now_ms=env.clock.wall_ms()) is False


class FakeSingleflight:
    def __init__(self, available=True):
        self.available = available
        self.acquired = 0
        self.released = 0

    def try_acquire(self):
        if self.available:
            self.acquired += 1
            return True
        return False

    def release(self):
        self.released += 1


def test_long_download_stale_200s_not_taken_over():
    """r2-M1 回归:阈值 ≥300s —— 200s 陈旧(单次合法下载 120s 量级)不算挂死。"""
    clock = FakeClock()
    assert HUNG_THRESHOLD_MS >= 300_000
    w = World(clock, held=True, last_loop=clock.wall_ms() - 200_000)
    assert w.supervisor().ensure() == "running"
    assert w.kills == [] and w.spawns == 0


def test_singleflight_busy_no_kill_no_spawn():
    """r2-M1④:并发接管者持锁 → 本次绝不 kill/spawn,只等对方结果。"""
    clock = FakeClock()
    w = World(clock, held=True, last_loop=clock.wall_ms() - HUNG_THRESHOLD_MS - 1)
    sf = FakeSingleflight(available=False)
    sup = w.supervisor(wait_s=1)
    sup.singleflight = sf

    def healing_sleep(s):
        w.last_loop = clock.wall_ms()  # 对方接管成功,世界变健康

    sup.sleep = healing_sleep
    assert sup.ensure() == "running"
    assert w.kills == [] and w.spawns == 0


def test_singleflight_busy_and_still_unhealthy_fails():
    clock = FakeClock()
    w = World(clock, held=True, last_loop=clock.wall_ms() - HUNG_THRESHOLD_MS - 1)
    sf = FakeSingleflight(available=False)
    sup = w.supervisor(wait_s=1)
    sup.singleflight = sf
    assert sup.ensure() == "failed"
    assert w.kills == [] and w.spawns == 0


def test_singleflight_acquired_released_around_takeover():
    clock = FakeClock()
    w = World(clock, held=True, last_loop=clock.wall_ms() - HUNG_THRESHOLD_MS - 1)
    sf = FakeSingleflight(available=True)
    sup = w.supervisor()
    sup.singleflight = sf
    assert sup.ensure() == "recovered"
    assert sf.acquired == 1 and sf.released == 1
