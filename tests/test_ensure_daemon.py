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

    def __init__(self, clock, held=False, last_loop=None, pid=DPID, pstart=DSTART,
                 startup="running:g1", generation="g1"):
        self.clock = clock
        self.held = held
        self.last_loop = last_loop
        self.pid = pid
        self.pstart = pstart
        self.startup = startup
        self.generation = generation
        self.kills = []
        self.spawns = 0
        self.prober = FakeProber()
        if pid is not None:
            self.prober.set(pid, 1, pstart, "python3")

    def lock_held(self):
        return self.held

    def read_state(self):
        return {"last_loop_at": self.last_loop, "daemon_pid": self.pid,
                "daemon_proc_start": self.pstart, "startup": self.startup,
                "daemon_generation": self.generation}

    def kill(self, pid, sig):
        self.kills.append((pid, sig))
        self.held = False  # SIGTERM 后 daemon 退出释放锁
        self.prober.remove(pid)

    def spawn(self):
        self.spawns += 1
        self.held = True
        self.last_loop = self.clock.wall_ms() + 1  # 新 daemon 心跳晚于 spawn
        self.startup = "running:g2"  # 默认 spawn 出的 daemon 就绪
        self.generation = "g2"

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
    dbmod.set_state(env.conn, "daemon_generation", "g1")
    dbmod.set_state(env.conn, "startup", "running:g1")
    dbmod.set_state(env.conn, "last_loop_at", env.clock.wall_ms() - 1000)
    assert ctl.daemon_healthy(env.conn, now_ms=env.clock.wall_ms()) is True
    # r4-1:probing 不算就绪
    dbmod.set_state(env.conn, "startup", "probing:g1")
    assert ctl.daemon_healthy(env.conn, now_ms=env.clock.wall_ms()) is False
    dbmod.set_state(env.conn, "startup", "running:g1")
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


class TestStartupStateGate:
    """r4-1:probing/refused 不算就绪;running/degraded 才算。"""

    def test_probing_not_ready_running_fast_path_requires_startup(self):
        from lib import ctl
        st = {"last_loop_at": 1000, "daemon_generation": "g1", "startup": "probing:g1"}
        assert ctl.state_ready(st, now_ms=1000) is False
        st["startup"] = "running:g1"
        assert ctl.state_ready(st, now_ms=1000) is True
        st["startup"] = "degraded:g1"
        assert ctl.state_ready(st, now_ms=1000) is True
        st["startup"] = "refused:g1"
        assert ctl.state_ready(st, now_ms=1000) is False

    def test_generation_mismatch_not_ready(self):
        from lib import ctl
        st = {"last_loop_at": 1000, "daemon_generation": "gNEW", "startup": "running:gOLD"}
        assert ctl.state_ready(st, now_ms=1000) is False

    def test_stale_heartbeat_not_ready_even_if_running(self):
        from lib import ctl
        st = {"last_loop_at": 0, "daemon_generation": "g1", "startup": "running:g1"}
        assert ctl.state_ready(st, now_ms=ctl.HUNG_THRESHOLD_MS + 1) is False

    def test_slow_probe_concludes_degraded_bind_can_continue(self):
        """慢探测最终 degraded → ensure 视为 started(就绪),bind 可继续。"""
        clock = FakeClock()
        # 起始:刚 spawn,probing;几步后转 degraded
        w = World(clock, held=False, last_loop=None)
        steps = {"n": 0}
        orig_spawn = w.spawn

        def slow_spawn():
            w.spawns += 1
            w.held = True
            w.startup = "probing:g2"
            w.generation = "g2"
            w.last_loop = clock.wall_ms()  # 首心跳(仅表活着)

        def transitioning_sleep(s):
            steps["n"] += 1
            if steps["n"] >= 3:
                w.startup = "degraded:g2"  # 探测最终结论=degraded
                w.last_loop = clock.wall_ms()

        w.spawn = slow_spawn
        sup = w.supervisor(wait_s=5)
        sup.sleep = transitioning_sleep
        assert sup.ensure() == "started"

    def test_slow_probe_concludes_mismatch_daemon_refused_failed(self):
        """慢探测最终 mismatch → daemon refused 退出 → ensure failed(bind 不会继续)。"""
        clock = FakeClock()
        w = World(clock, held=False, last_loop=None)
        steps = {"n": 0}

        def slow_spawn():
            w.spawns += 1
            w.held = True
            w.startup = "probing:g2"
            w.generation = "g2"
            w.last_loop = clock.wall_ms()

        def refusing_sleep(s):
            steps["n"] += 1
            if steps["n"] >= 3:
                w.startup = "refused:g2"  # 结论=mismatch
                w.held = False            # daemon 退出释放锁

        w.spawn = slow_spawn
        sup = w.supervisor(wait_s=5)
        sup.sleep = refusing_sleep
        assert sup.ensure() == "failed"
