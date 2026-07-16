"""daemon 核心:flock 单例 + 单线程事件循环(consumer 输出、恢复 tick、出站 tick 全部串行)。
consumer = `lark-cli event consume <key> --as bot --timeout 0`(stdin 持有;stderr 监控
ready/WARN;SIGTERM 管理,绝不 kill -9;退避重启;快速退出循环 → 告警)。"""
import json
import os
import selectors
import signal
import subprocess

from . import constants, db

RECEIVE_KEY = "im.message.receive_v1"
CARD_KEY = "card.action.trigger"

RESTART_BACKOFF_START_MS = 1_000
RESTART_BACKOFF_MAX_MS = 60_000
STABLE_RUN_MS = 60_000
RAPID_EXIT_ALERT_THRESHOLD = 5


class _Consumer:
    __slots__ = ("key", "proc", "out_buf", "err_buf", "ready", "restarts",
                 "started_at", "next_restart_at", "backoff", "exited")

    def __init__(self, key):
        self.key = key
        self.proc = None
        self.out_buf = b""
        self.err_buf = b""
        self.ready = False
        self.restarts = 0
        self.started_at = None
        self.next_restart_at = 0
        self.backoff = RESTART_BACKOFF_START_MS
        self.exited = True


class ConsumerManager:
    def __init__(self, profile, clock, on_line, on_status,
                 lark_bin="lark-cli", keys=(RECEIVE_KEY, CARD_KEY), argv_builder=None):
        self.profile = profile
        self.clock = clock
        self.on_line = on_line      # (key, line_str) -> None
        self.on_status = on_status  # (key, status, detail) -> None
        self.lark_bin = lark_bin
        self.keys = keys
        self.argv_builder = argv_builder or self._default_argv
        self.selector = selectors.DefaultSelector()
        self.consumers = {k: _Consumer(k) for k in keys}

    def _default_argv(self, key):
        return [self.lark_bin, "event", "consume", key, "--as", "bot",
                "--timeout", "0", "--profile", self.profile]

    # ------------------------------------------------------------------
    def start_all(self):
        now = self.clock.mono_ms()
        for c in self.consumers.values():
            self._spawn(c, now)

    def _spawn(self, c, now):
        argv = self.argv_builder(c.key)
        try:
            c.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True)
        except OSError as e:
            c.exited = True
            c.next_restart_at = now + c.backoff
            c.backoff = min(c.backoff * 2, RESTART_BACKOFF_MAX_MS)
            self.on_status(c.key, "spawn-failed", str(e))
            return
        c.exited = False
        c.ready = False
        c.started_at = now
        for stream, tag in ((c.proc.stdout, "stdout"), (c.proc.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ, (c, tag))
        self.on_status(c.key, "spawned", f"pid={c.proc.pid}")

    # ------------------------------------------------------------------
    def poll(self, timeout_s):
        """select 一轮并分发行;返回处理的行数。"""
        n = 0
        try:
            events = self.selector.select(timeout_s)
        except OSError:
            return 0
        for key, _mask in events:
            c, tag = key.data
            stream = key.fileobj
            try:
                chunk = os.read(stream.fileno(), 65536)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                chunk = b""
            if chunk == b"":
                try:
                    self.selector.unregister(stream)
                except (KeyError, ValueError):
                    pass
                self._mark_exited(c)
                continue
            n += self._feed(c, tag, chunk)
        return n

    def _feed(self, c, tag, chunk):
        n = 0
        if tag == "stdout":
            c.out_buf += chunk
            while b"\n" in c.out_buf:
                line, c.out_buf = c.out_buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self.on_line(c.key, text)
                    n += 1
        else:
            c.err_buf += chunk
            while b"\n" in c.err_buf:
                line, c.err_buf = c.err_buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                if "[event] ready" in text:
                    c.ready = True
                    self.on_status(c.key, "ready", text)
                else:
                    self.on_status(c.key, "stderr", text)
        return n

    def _mark_exited(self, c):
        if c.exited:
            return
        c.exited = True
        now = self.clock.mono_ms()
        stable = c.started_at is not None and now - c.started_at >= STABLE_RUN_MS
        if stable:
            c.backoff = RESTART_BACKOFF_START_MS
            c.restarts = 0
        c.restarts += 1
        c.next_restart_at = now + c.backoff
        c.backoff = min(c.backoff * 2, RESTART_BACKOFF_MAX_MS)
        if c.restarts >= RAPID_EXIT_ALERT_THRESHOLD:
            self.on_status(c.key, "rapid-exit-alert", f"restarts={c.restarts}")
        else:
            self.on_status(c.key, "exited", f"restarts={c.restarts}")
        if c.proc is not None:
            try:
                c.proc.wait(timeout=0.1)
            except Exception:
                pass

    def tick(self):
        """重启到期的 dead consumer;探测静默退出的进程。"""
        now = self.clock.mono_ms()
        for c in self.consumers.values():
            if not c.exited and c.proc is not None and c.proc.poll() is not None:
                for stream in (c.proc.stdout, c.proc.stderr):
                    try:
                        self.selector.unregister(stream)
                    except (KeyError, ValueError):
                        pass
                self._mark_exited(c)
            if c.exited and now >= c.next_restart_at:
                self._spawn(c, now)

    def shutdown(self):
        """SIGTERM(勿 -9,防服务端订阅泄漏)→ 等 5s → 放弃(绝不 SIGKILL consume)。"""
        for c in self.consumers.values():
            if c.proc is None or c.proc.poll() is not None:
                continue
            try:
                os.killpg(os.getpgid(c.proc.pid), signal.SIGTERM)
            except OSError:
                try:
                    c.proc.terminate()
                except OSError:
                    pass
        for c in self.consumers.values():
            if c.proc is None:
                continue
            try:
                c.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        try:
            self.selector.close()
        except OSError:
            pass


class DaemonCore:
    """事件路由 + 节奏编排(可测:route_line / loop_iteration 均纯函数式入口)。"""

    def __init__(self, conn, cfg, clock, inbound, approval, outbound, recovery, log=None):
        self.conn = conn
        self.cfg = cfg
        self.clock = clock
        self.inbound = inbound
        self.approval = approval
        self.outbound = outbound
        self.recovery = recovery
        self.log = log or (lambda s: None)
        self._last_fast = 0
        self._last_slow = 0
        self._last_checkpoint = 0

    # ------------------------------------------------------------------
    def route_line(self, kind, line):
        try:
            obj = json.loads(line)
        except ValueError:
            db.bump_counter(self.conn, "malformed_event_lines")
            return
        if not isinstance(obj, dict):
            db.bump_counter(self.conn, "malformed_event_lines")
            return
        try:
            if kind == RECEIVE_KEY:
                self.inbound.process_event(obj)
            elif kind == CARD_KEY:
                self.approval.process_event(obj)
        except Exception as e:
            # 单条事件失败不拖垮循环;fail-closed(不产生任何外发)+ 计数
            db.bump_counter(self.conn, "event_processing_errors")
            db.set_state(self.conn, "last_error", f"{type(e).__name__}: {e}")
            self.log(f"event error: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    def update_suspect_window(self, now):
        """睡眠/时钟回拨检测:loop 间隔异常 → 开 suspect 窗(判死宽限)。返回是否在窗内。"""
        prev = db.get_state(self.conn, "last_loop_at")
        suspect_until = int(db.get_state(self.conn, "suspect_until", "0") or 0)
        if prev is not None:
            prev = int(prev)
            if now < prev or now - prev > constants.DAEMON_GAP_MS:
                suspect_until = now + constants.SUSPECT_WINDOW_MS
                db.set_state(self.conn, "suspect_until", suspect_until)
        db.set_state(self.conn, "last_loop_at", now)
        return now < suspect_until

    def loop_iteration(self):
        now = self.clock.wall_ms()
        in_suspect = self.update_suspect_window(now)
        if now - self._last_fast >= constants.DEATH_SCAN_INTERVAL_MS or self._last_fast == 0 \
                or now < self._last_fast:
            self._last_fast = now
            self.recovery.fast_tick(in_suspect_window=in_suspect)
        else:
            # 每轮仍推进 waiting 行与激活(轻量)
            self.inbound.drive_waiting_rows()
        self.outbound.tick()
        if now - self._last_slow >= constants.RECOVERY_INTERVAL_MS or self._last_slow == 0 \
                or now < self._last_slow:
            self._last_slow = now
            self.recovery.slow_tick()
        if now - self._last_checkpoint >= constants.CHECKPOINT_INTERVAL_MS \
                or now < self._last_checkpoint:
            self._last_checkpoint = now
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
