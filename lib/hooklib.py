"""Stop / SessionEnd hook 核心(plan 4.7 / 4.1.6)。
I5:hook 不联网,只读写 bridge.db(busy_timeout 有界);异常总则=统一抑制 +
hook_drops.log 固定文案一行(不含正文)+ 计数镜像;双双不可写 → stderr 固定告警。"""
import datetime
import sqlite3
import sys
import time

from . import constants, db, jobs, lifecycle, paths, procs, texts, util


def _touch_hook_heartbeat(event):
    """plugin 化:每次 hook 运行都写各自哨兵(先于 no-db 早退)=「plugin hooks 已生效」正向信号,
    供 preflight/bind 检测(替代读 settings.json 判断手装 hooks)。记 event/时间/plugin version/
    pkg_root(MAJOR 2:防「另一 install/旧版本/仅 SessionEnd」假阳性)。绝不抛异常(hook 永不因此失败)。"""
    try:
        paths.ensure_data_dir()
        from . import version as versionmod
        root, ver = versionmod.install_identity()
        util.atomic_write(paths.hook_heartbeat_path(event), util.jdumps({
            "event": event, "ts": int(time.time() * 1000),
            "plugin_version": ver, "pkg_root": root}))
    except Exception:
        pass


# ---------------------------------------------------------------- 入口(fail-closed 包装)
def stop_hook_entry(payload, conn=None, prober=None, clock=None, start_pid=None):
    _touch_hook_heartbeat("stop")
    own_conn = None
    try:
        if conn is None:
            dbf = paths.db_path()
            if not dbf.exists():
                return {"suppressed": False, "reason": "no-db"}
            own_conn = db.connect(dbf, busy_timeout_ms=constants.BUSY_TIMEOUT_HOOK_MS)
            conn = own_conn
        if prober is None:
            prober = procs.SystemProber()
        if clock is None:
            from .clock import SystemClock
            clock = SystemClock()
        return run_stop_hook(payload, conn=conn, prober=prober, clock=clock,
                             start_pid=start_pid)
    except Exception as e:
        _fail_closed_drop(e, hook="stop")
        return {"suppressed": True, "reason": "exception"}
    finally:
        if own_conn is not None:
            try:
                own_conn.close()
            except Exception:
                pass


def session_end_entry(payload, conn=None, prober=None, clock=None, start_pid=None):
    _touch_hook_heartbeat("session_end")
    own_conn = None
    try:
        if conn is None:
            dbf = paths.db_path()
            if not dbf.exists():
                return {"closed": [], "reason": "no-db"}
            # minor③:SessionEnd 关绑定是 **best-effort**(daemon cc_gone 兜底,非安全问题);
            # 用更短的等锁超时 → 锁竞争时快速让路,绝不拖住 CC 退出。
            own_conn = db.connect(dbf, busy_timeout_ms=constants.BUSY_TIMEOUT_SESSION_END_MS)
            conn = own_conn
        if prober is None:
            prober = procs.SystemProber()
        if clock is None:
            from .clock import SystemClock
            clock = SystemClock()
        return run_session_end(payload, conn=conn, prober=prober, clock=clock,
                               start_pid=start_pid)
    except Exception as e:
        _fail_closed_drop(e, hook="session_end")
        return {"closed": [], "reason": "exception"}
    finally:
        if own_conn is not None:
            try:
                own_conn.close()
            except Exception:
                pass


def _bump_drop_counter():
    try:
        # minor②:可观测计数用**短等锁**(BUSY_TIMEOUT_OBS_MS)——否则 SessionEnd 首次等锁失败(1.5s)
        # 后又用 3s 等锁补 counter = 总 ~4.5s,会拖住 CC 退出。计数只是观测,拿不到锁就算了。
        c = db.connect(paths.db_path(), busy_timeout_ms=constants.BUSY_TIMEOUT_OBS_MS)
        try:
            db.bump_counter(c, "hook_drop_count")
        finally:
            c.close()
        return True
    except Exception:
        return False


def _fail_closed_drop(exc, hook="stop"):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    logged = False
    try:
        util.append_log_line(paths.hook_drops_path(),
                             f"{ts} {hook}_hook drop code={type(exc).__name__}")
        logged = True
    except Exception:
        pass
    counted = _bump_drop_counter()
    if not logged and not counted:
        # 可观测性降级:stderr 固定一行(4.7 异常总则)
        print("feishu-bridge: hook fail-closed drop (observability degraded)",
              file=sys.stderr)


# ---------------------------------------------------------------- Stop(4.7)
def run_stop_hook(payload, *, conn, prober, clock, start_pid=None):
    now = clock.wall_ms()
    session_id = payload.get("session_id")
    msg = payload.get("last_assistant_message") or ""
    sha = bool(payload.get("stop_hook_active"))
    inst = procs.find_cc_instance(prober, start_pid)
    if inst is None:
        return {"suppressed": True, "reason": "no-instance"}  # S3 运行时失败 → fail-closed
    pid, lstart = inst

    # ① 抑制链闩(r5-B1):续写 Stop 全抑制;fresh Stop 先关闩再正常处理
    latches = conn.execute(
        "SELECT 1 FROM pending_bind WHERE cc_pid=? AND cc_start=? AND latch_open=1 LIMIT 1",
        (pid, lstart)).fetchone()
    if latches:
        if sha:
            return {"suppressed": True, "reason": "latch-continuation"}
        with db.tx(conn):
            conn.execute(
                "UPDATE pending_bind SET latch_open=0 "
                "WHERE cc_pid=? AND cc_start=? AND latch_open=1", (pid, lstart))

    # ② bind 握手(4.1.6)
    pend = conn.execute(
        "SELECT * FROM pending_bind WHERE cc_pid=? AND cc_start=? AND state='pending' "
        "ORDER BY created_at DESC LIMIT 1", (pid, lstart)).fetchone()
    if pend is not None and (pend["expires_at"] or 0) > now:
        if util.marker_for(pend["nonce"]) in msg:
            return _handshake(conn, pend, session_id, now)
        # nonce 不命中:本 turn 属 bind turn → failed(latch 仍置)+ close starting
        with db.tx(conn):
            if db.cas(conn,
                      "UPDATE pending_bind SET state='failed', latch_open=1 "
                      "WHERE request_id=? AND state='pending'", (pend["request_id"],)):
                lifecycle._terminate_in_tx(conn, pend["request_id"], "bind_failed", now)
        return {"suppressed": True, "reason": "bind-nonce-miss"}

    # ③ tombstone:任意状态 pending_bind 行的完整 marker ∈ 消息
    for r in conn.execute(
            "SELECT nonce FROM pending_bind WHERE cc_pid=? AND cc_start=?",
            (pid, lstart)).fetchall():
        if util.marker_for(r["nonce"]) in msg:
            return {"suppressed": True, "reason": "tombstone"}

    # ④ 前缀纵深(member 诱导只会抑制转发 = fail-closed 方向,无害)
    if constants.MARKER_PREFIX in msg:
        return {"suppressed": True, "reason": "marker-prefix"}

    # ⑤ 正常路径:active 绑定 ∧ session_id ∧ cc 实例全匹配
    if not session_id:
        return {"suppressed": False, "reason": "no-session-id"}
    b = conn.execute(
        "SELECT * FROM bindings WHERE session_id=? AND status='active'",
        (session_id,)).fetchone()
    if b is None:
        return {"suppressed": False, "reason": "no-binding"}
    if (b["cc_pid"], b["cc_start"]) != (pid, lstart):
        _fail_closed_drop(RuntimeError("instance-mismatch"), hook="stop")
        return {"suppressed": True, "reason": "instance-mismatch"}
    if not msg.strip():
        return {"suppressed": False, "reason": "empty-message"}
    chunks = util.chunk_text(msg, constants.CHUNK_LIMIT)
    # 每轮转发页脚(context/model/effort)= best-effort 装饰,完全隔离于转发关键路径(fail-open 铁律):
    #   延迟导入 → 即便 ctxmeter 损坏(如 3.9 导入错)也只丢页脚、不炸 hook(不在顶层 import,把导入面挡在关键路径外);
    #   本地 try → import/计算/拼接任何异常都不冒泡到 stop_hook_entry 的 fail-closed 包装。
    #   拼到 chunks[-1] → 页脚完整、只一次、落最大 index;(turn_group,chunk_index) 唯一索引不受影响。
    try:
        from . import ctxmeter
        footer = ctxmeter.footer_for(payload)
        if footer and chunks:
            chunks[-1] = chunks[-1] + footer
    except Exception:
        pass                    # chunks 不变 → 原样转发
    group = util.new_id()
    with db.tx(conn):
        cur = conn.execute("SELECT status FROM bindings WHERE binding_id=?",
                           (b["binding_id"],)).fetchone()
        if cur is None or cur["status"] != "active":
            return {"suppressed": False, "reason": "binding-terminated"}
        for i, c in enumerate(chunks):
            jobs.create_job(
                conn, kind="session_turn", chat_id=b["chat_id"],
                binding_id=b["binding_id"], idempotency_key=jobs.key_turn(group, i),
                turn_group=group, chunk_index=i, body=c, now=now)
    return {"suppressed": False, "reason": "enqueued",
            "turn_group": group, "chunks": len(chunks)}


def _handshake(conn, pend, session_id, now):
    """单事务:pending→consumed+开闩 + confirmed+回填 session_id + (listener 就绪则)激活。"""
    rid = pend["request_id"]
    if not session_id:
        with db.tx(conn):
            if db.cas(conn,
                      "UPDATE pending_bind SET state='failed', latch_open=1 "
                      "WHERE request_id=? AND state='pending'", (rid,)):
                lifecycle._terminate_in_tx(conn, rid, "bind_failed", now)
        return {"suppressed": True, "reason": "no-session-id-at-handshake"}
    try:
        with db.tx(conn):
            if not db.cas(conn,
                          "UPDATE pending_bind SET state='consumed', latch_open=1 "
                          "WHERE request_id=? AND state='pending' AND expires_at>?",
                          (rid, now)):
                return {"suppressed": True, "reason": "handshake-race"}
            b = conn.execute(
                "SELECT * FROM bindings WHERE binding_id=? AND status='starting'",
                (rid,)).fetchone()
            if b is None:
                db.cas(conn,
                       "UPDATE pending_bind SET state='failed' "
                       "WHERE request_id=? AND state='consumed'", (rid,))
                return {"suppressed": True, "reason": "binding-gone"}
            conn.execute(
                "UPDATE bindings SET bind_phase='confirmed', confirmed_at=?, session_id=? "
                "WHERE binding_id=? AND status='starting'", (now, session_id, rid))
            b2 = conn.execute("SELECT * FROM bindings WHERE binding_id=?", (rid,)).fetchone()
            # listener 排他已建立(当前 epoch 有新鲜心跳,非仅 pid 非空)→ 同事务激活
            if lifecycle.heartbeat_fresh(b2, now):
                if db.cas(conn,
                          "UPDATE bindings SET status='active', bound_at=? "
                          "WHERE binding_id=? AND status='starting' AND session_id IS NOT NULL",
                          (now, rid)):
                    jobs.create_job(
                        conn, kind="lifecycle_notice", chat_id=b2["chat_id"],
                        binding_id=rid, idempotency_key=jobs.key_lc(rid, "bound"),
                        expected_state="active", body=texts.LC_BOUND, now=now)
            return {"suppressed": True, "reason": "bind-handshake"}
    except sqlite3.IntegrityError:
        # b_sess 冲突:该 session 已绑他群 → close starting + pending failed + 失败通知
        with db.tx(conn):
            if db.cas(conn,
                      "UPDATE pending_bind SET state='failed', latch_open=1 "
                      "WHERE request_id=? AND state='pending'", (rid,)):
                lifecycle._terminate_in_tx(conn, rid, "bind_failed", now)
        return {"suppressed": True, "reason": "session-conflict"}


# ---------------------------------------------------------------- SessionEnd(4.6 快路径)
def run_session_end(payload, *, conn, prober, clock, start_pid=None):
    now = clock.wall_ms()
    sid = payload.get("session_id")
    closed = []
    if sid:
        for b in conn.execute(
                "SELECT binding_id FROM bindings WHERE session_id=? "
                "AND status IN ('starting','active')", (sid,)).fetchall():
            if lifecycle.terminate_binding(conn, b["binding_id"], "session_end", clock):
                closed.append(b["binding_id"])
    inst = procs.find_cc_instance(prober, start_pid)
    if inst is not None:
        pid, lstart = inst
        for r in conn.execute(
                "SELECT request_id FROM pending_bind WHERE cc_pid=? AND cc_start=? "
                "AND state='pending'", (pid, lstart)).fetchall():
            with db.tx(conn):
                if db.cas(conn,
                          "UPDATE pending_bind SET state='expired', latch_open=0 "
                          "WHERE request_id=? AND state='pending'", (r["request_id"],)):
                    if lifecycle._terminate_in_tx(conn, r["request_id"], "session_end", now):
                        closed.append(r["request_id"])
        with db.tx(conn):
            conn.execute(
                "UPDATE pending_bind SET latch_open=0 "
                "WHERE cc_pid=? AND cc_start=? AND latch_open=1", (pid, lstart))
    return {"closed": closed}
