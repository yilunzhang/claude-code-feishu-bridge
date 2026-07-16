"""daemon 装配层:事件路由 / 睡眠 gap suspect 窗 / ConsumerManager 真子进程管道。"""
import json
import sys
import time

from tests.conftest import APP_ID, CHAT, OWNER
from tests.helpers import FakeClock, bot_mention, mget_snapshot
from lib import constants, db as dbmod
from lib.daemon_core import ConsumerManager, DaemonCore, RECEIVE_KEY, CARD_KEY


def make_core(env):
    return DaemonCore(env.conn, env.cfg, env.clock, env.inbound, env.approval,
                      env.outbound, env.recovery)


class TestRouting:
    def test_receive_line_routes_to_inbound(self, env):
        env.make_binding(status="active")
        env.arm_mget([mget_snapshot("om_1", CHAT, OWNER, mentions=[bot_mention(APP_ID)])])
        core = make_core(env)
        line = json.dumps({"type": RECEIVE_KEY, "event_id": "ev_1", "message_id": "om_1",
                           "chat_id": CHAT, "chat_type": "group", "sender_id": OWNER,
                           "message_type": "text", "content": "@TestBot hi"})
        core.route_line(RECEIVE_KEY, line)
        assert env.inbox_row("om_1")["state"] == "enqueued"

    def test_card_line_routes_to_approval(self, env):
        from tests.test_approval import member_pending, cb
        p = member_pending(env)
        core = make_core(env)
        core.route_line(CARD_KEY, json.dumps(cb(p)))
        assert env.conn.execute("SELECT state FROM pendings").fetchone()[0] == "approved"

    def test_malformed_line_counted_not_crashing(self, env):
        core = make_core(env)
        core.route_line(RECEIVE_KEY, "not json {{{")
        core.route_line(RECEIVE_KEY, '"a bare string"')
        assert int(dbmod.get_state(env.conn, "malformed_event_lines", "0")) == 2

    def test_event_error_caught_and_counted(self, env, monkeypatch):
        core = make_core(env)

        def boom(ev):
            raise RuntimeError("processing exploded")

        monkeypatch.setattr(env.inbound, "process_event", boom)
        core.route_line(RECEIVE_KEY, json.dumps({"event_id": "e", "message_id": "m",
                                                 "chat_id": CHAT}))
        assert int(dbmod.get_state(env.conn, "event_processing_errors", "0")) == 1


class TestSuspectWindow:
    def test_gap_opens_window_and_expires(self, env):
        core = make_core(env)
        assert core.update_suspect_window(env.clock.wall_ms()) is False
        env.clock.tick(constants.DAEMON_GAP_MS + 5000)  # 模拟睡眠
        assert core.update_suspect_window(env.clock.wall_ms()) is True
        # 以正常节奏(5s/步,< gap 阈值)走完宽限窗 → 窗过期,恢复判死
        last = True
        for _ in range(constants.SUSPECT_WINDOW_MS // 5000 + 2):
            env.clock.tick(5000)
            last = core.update_suspect_window(env.clock.wall_ms())
        assert last is False

    def test_clock_rewind_opens_window(self, env):
        core = make_core(env)
        core.update_suspect_window(env.clock.wall_ms())
        env.clock.rewind_wall(60_000)
        assert core.update_suspect_window(env.clock.wall_ms()) is True

    def test_loop_iteration_smoke(self, env):
        core = make_core(env)
        core.loop_iteration()
        assert dbmod.get_state(env.conn, "last_loop_at") is not None


CHILD_OK = (
    "import sys,time\n"
    "sys.stderr.write('[event] ready event_key=x\\n'); sys.stderr.flush()\n"
    "sys.stdout.write('{\"n\":1}\\n{\"n\":2}\\n'); sys.stdout.flush()\n"
    "time.sleep(30)\n")

CHILD_EXIT = "print('{\"n\":9}')\n"


class TestConsumerManagerSubprocess:
    def _mgr(self, child_src, keys=("k1",)):
        lines, statuses = [], []
        clock = FakeClock()
        mgr = ConsumerManager(
            "main", clock,
            on_line=lambda k, l: lines.append((k, l)),
            on_status=lambda k, s, d: statuses.append((k, s)),
            keys=keys,
            argv_builder=lambda key: [sys.executable, "-u", "-c", child_src])
        return mgr, lines, statuses, clock

    def test_ready_detection_and_line_dispatch(self, env):
        mgr, lines, statuses, _ = self._mgr(CHILD_OK)
        mgr.start_all()
        deadline = time.time() + 5
        while time.time() < deadline and len(lines) < 2:
            mgr.poll(0.2)
        mgr.shutdown()
        assert [json.loads(l)["n"] for _, l in lines] == [1, 2]
        assert ("k1", "ready") in statuses
        assert mgr.consumers["k1"].ready is True

    def test_exit_schedules_backoff_restart(self, env):
        mgr, lines, statuses, clock = self._mgr(CHILD_EXIT)
        mgr.start_all()
        deadline = time.time() + 5
        while time.time() < deadline and not mgr.consumers["k1"].exited:
            mgr.poll(0.2)
            mgr.tick()
        assert mgr.consumers["k1"].exited is True
        spawned_before = len([s for s in statuses if s[1] == "spawned"])
        mgr.tick()  # 退避期内不重启
        assert len([s for s in statuses if s[1] == "spawned"]) == spawned_before
        clock.tick(120_000)  # 过退避
        mgr.tick()
        assert len([s for s in statuses if s[1] == "spawned"]) == spawned_before + 1
        mgr.shutdown()
