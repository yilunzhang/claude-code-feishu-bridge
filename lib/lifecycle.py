"""绑定生命周期:bind 建行(1:1/TOCTOU 原子闭合)/ 统一终止事务 / 判死两条独立 CAS /
confirmed→激活 / pending_bind 超时(r6:同一终止事务)。plan 4.1/4.6/4.8。"""
import sqlite3

from . import constants, db, jobs, procs, texts, util


class BindConflict(Exception):
    def __init__(self, code, msg):
        self.code = code
        super().__init__(msg)


def create_binding(conn, *, chat_id, chat_name, cwd, cc_pid, cc_start, clock):
    """单事务:INSERT pending_bind + INSERT bindings(starting, session_id=NULL)。
    b_chat/b_inst/pb_inst 任一命中即失败(1:1 与 TOCTOU 在此原子闭合,plan 4.1.4)。
    修复项2(自愈):同 cc 实例残留的 starting 绑定/pending 行(如握手前 /clear)→
    先在同一终止事务里 close(bind_superseded,映射 unbound 类)再插新行,
    消除"10 分钟占位阻止 rebind";active 绑定绝不 supersede(仍要求显式 unbind)。"""
    now = clock.wall_ms()
    rid = util.new_id()
    nonce = util.new_nonce()
    try:
        with db.tx(conn):
            stale = conn.execute(
                "SELECT binding_id FROM bindings WHERE cc_pid=? AND cc_start=? "
                "AND status='starting'", (cc_pid, cc_start)).fetchone()
            if stale:
                _terminate_in_tx(conn, stale[0], "bind_superseded", now)
            # 孤儿 pending(无对应 starting 行的异常残留)同样清掉,防 pb_inst 卡插入
            conn.execute(
                "UPDATE pending_bind SET state='expired', latch_open=0 "
                "WHERE cc_pid=? AND cc_start=? AND state='pending'", (cc_pid, cc_start))
            conn.execute(
                "INSERT INTO pending_bind(request_id,chat_id,cwd,cc_pid,cc_start,nonce,"
                "state,latch_open,created_at,expires_at) VALUES(?,?,?,?,?,?,'pending',0,?,?)",
                (rid, chat_id, cwd, cc_pid, cc_start, nonce, now,
                 now + constants.PENDING_BIND_TTL_MS))
            conn.execute(
                "INSERT INTO bindings(binding_id,chat_id,chat_name,session_id,cc_pid,cc_start,"
                "cwd,status,bind_phase) VALUES(?,?,?,NULL,?,?,?,'starting','unconfirmed')",
                (rid, chat_id, chat_name, cc_pid, cc_start, cwd))
    except sqlite3.IntegrityError as e:
        chat_busy = conn.execute(
            "SELECT binding_id FROM bindings WHERE chat_id=? AND status IN ('starting','active')",
            (chat_id,)).fetchone()
        if chat_busy:
            raise BindConflict("chat_busy",
                               f"该群已有绑定({chat_busy[0]}),先 unbind 再 bind") from e
        inst_busy = conn.execute(
            "SELECT binding_id FROM bindings WHERE cc_pid=? AND cc_start=? "
            "AND status IN ('starting','active')", (cc_pid, cc_start)).fetchone()
        if inst_busy:
            raise BindConflict("instance_busy",
                               f"本 CC 实例已有绑定({inst_busy[0]}),先 unbind 再 bind") from e
        pending = conn.execute(
            "SELECT request_id FROM pending_bind WHERE cc_pid=? AND cc_start=? AND state='pending'",
            (cc_pid, cc_start)).fetchone()
        if pending:
            raise BindConflict("pending_exists",
                               f"本 CC 实例已有进行中的 bind({pending[0]})") from e
        raise BindConflict("unknown", str(e)) from e
    return {"binding_id": rid, "nonce": nonce, "marker": util.marker_for(nonce)}


def map_terminated_to_inbox_state(binding_row):
    """§3 inbox 注释 / 4.2.4 / 4.8 唯一映射函数(r7-①:bind_timeout→unbound)。"""
    if binding_row is None:
        return "unbound"  # 该 chat 从无绑定史
    status = binding_row["status"]
    reason = binding_row["close_reason"]
    if status in ("starting", "active"):
        raise ValueError("map_terminated called on non-terminal binding")
    if status == "dead" or reason in constants.SESSION_CLOSED_REASONS:
        return "session_closed"
    if reason in constants.UNBOUND_CLOSE_REASONS:
        return "unbound"
    return "session_closed"  # 未知 reason 保守按"已关闭"提示


def _terminate_in_tx(conn, binding_id, close_reason, now, new_status="closed",
                     expect=("starting", "active"), notify=True):
    """统一终止事务函数(4.6):CAS 带旧 status,胜者才级联。要求调用方已开事务。"""
    qs = ",".join("?" for _ in expect)
    won = db.cas(
        conn,
        f"UPDATE bindings SET status=?, closed_at=?, close_reason=? "
        f"WHERE binding_id=? AND status IN ({qs})",
        (new_status, now, close_reason, binding_id, *expect))
    if not won:
        return False
    row = conn.execute("SELECT * FROM bindings WHERE binding_id=?", (binding_id,)).fetchone()

    # pendings pending→expired(其 inbox 行一并终态化)
    pend_msgs = [r[0] for r in conn.execute(
        "SELECT message_id FROM pendings WHERE binding_id=? AND state='pending'",
        (binding_id,))]
    conn.execute(
        "UPDATE pendings SET state='expired', decided_at=? WHERE binding_id=? AND state='pending'",
        (now, binding_id))
    for mid in pend_msgs:
        conn.execute(
            "UPDATE inbox SET state='expired', ts=? WHERE message_id=? "
            "AND state IN ('awaiting_approval','approved_materializing')", (now, mid))

    # 未领 deliveries → dropped(leased 由恢复工人的 lease 超时收口)
    conn.execute(
        "UPDATE deliveries SET state='dropped' WHERE binding_id=? AND state='enqueued'",
        (binding_id,))

    # 该绑定所有仍可重试(pending/unknown)的既有 job 全部 cancelled。
    # 本次终止的 lifecycle_notice 是"唯一保留的既有 job 豁免"的实现方式=先 cancel 再新建;
    # waiting 行的 inbound_notice 回执同理是本次终止事务新建的副作用(4.8),不属被豁免的存量。
    conn.execute(
        "UPDATE outbound_jobs SET state='cancelled' WHERE binding_id=? "
        "AND state IN ('pending','unknown')", (binding_id,))

    # pending_bind 终态化并关闩(仅本次终态化的行;已终态行的闩不动——
    # 4.1.6 nonce-miss 路径要求 latch 仍置,链闩由下一个 fresh Stop 自愈,SessionEnd 另行显式关)
    conn.execute(
        "UPDATE pending_bind SET state='expired', latch_open=0 "
        "WHERE request_id=? AND state='pending'", (binding_id,))

    if notify:
        jobs.create_job(
            conn, kind="lifecycle_notice", chat_id=row["chat_id"], binding_id=binding_id,
            idempotency_key=jobs.key_lc(binding_id, close_reason),
            expected_state=f"{new_status}:{close_reason}",
            body=texts.lifecycle_close_body(close_reason), now=now)

    # waiting_binding 行 → 4.2.4 同一映射终态 + 回执(r6-M1;冷却限速)
    target = map_terminated_to_inbox_state(row)
    for r in conn.execute(
            "SELECT message_id, chat_id FROM inbox WHERE binding_id=? AND state='waiting_binding'",
            (binding_id,)).fetchall():
        conn.execute(
            "UPDATE inbox SET state=?, ts=? WHERE message_id=? AND state='waiting_binding'",
            (target, now, r["message_id"]))
        jobs.create_inbound_notice(conn, chat_id=r["chat_id"], message_id=r["message_id"],
                                   code=target, binding_id=binding_id, now=now)
    return True


def terminate_binding(conn, binding_id, close_reason, clock, new_status="closed",
                      expect=("starting", "active"), notify=True):
    with db.tx(conn):
        return _terminate_in_tx(conn, binding_id, close_reason, clock.wall_ms(),
                                new_status=new_status, expect=expect, notify=notify)


def expire_stale_pending_binds(conn, clock):
    """过期 pending_bind → 同一终止事务:expired(关闩)+ close 孤儿 starting(bind_timeout)
    + 其 waiting 行终态化(r6 修永久占位)。"""
    now = clock.wall_ms()
    rows = conn.execute(
        "SELECT request_id FROM pending_bind WHERE state='pending' AND expires_at<?",
        (now,)).fetchall()
    n = 0
    for r in rows:
        with db.tx(conn):
            if not db.cas(conn,
                          "UPDATE pending_bind SET state='expired', latch_open=0 "
                          "WHERE request_id=? AND state='pending' AND expires_at<?",
                          (r["request_id"], now)):
                continue
            _terminate_in_tx(conn, r["request_id"], "bind_timeout", now)
            n += 1
    return n


def heartbeat_fresh(row, now):
    return (row["listener_pid"] is not None and row["listener_epoch"] > 0
            and row["listener_beat_at"] is not None
            and (now - row["listener_beat_at"]) <= constants.HEARTBEAT_FRESH_MS)


def activate_if_ready(conn, binding_id, clock):
    """恢复工人:confirmed ∧ 当前 epoch 心跳新鲜 → 激活;confirmed ∧ 超 30s 无 → close。"""
    now = clock.wall_ms()
    with db.tx(conn):
        b = conn.execute(
            "SELECT * FROM bindings WHERE binding_id=? AND status='starting'",
            (binding_id,)).fetchone()
        if not b:
            return "gone"
        if b["bind_phase"] != "confirmed":
            return "waiting"  # 未确认:由 pending_bind TTL 负责
        if heartbeat_fresh(b, now):
            ok = db.cas(conn,
                        "UPDATE bindings SET status='active', bound_at=? "
                        "WHERE binding_id=? AND status='starting' AND bind_phase='confirmed' "
                        "AND session_id IS NOT NULL", (now, binding_id))
            if ok:
                jobs.create_job(
                    conn, kind="lifecycle_notice", chat_id=b["chat_id"],
                    binding_id=binding_id, idempotency_key=jobs.key_lc(binding_id, "bound"),
                    expected_state="active", body=texts.LC_BOUND, now=now)
                return "activated"
            return "waiting"
        if b["confirmed_at"] is not None and now - b["confirmed_at"] > constants.ACTIVATION_TIMEOUT_MS:
            _terminate_in_tx(conn, binding_id, "listener_never_ready", now)
            return "timed_out"
        return "waiting"


def death_scan(conn, prober, clock, in_suspect_window=False):
    """两条独立 CAS(4.6):cc_gone(确定死即关,不要求心跳陈旧);
    listener_gone(心跳即租约,pid 存活不豁免;两阶段 suspect + 睡眠宽限)。"""
    now = clock.wall_ms()
    actions = []
    rows = conn.execute(
        "SELECT * FROM bindings WHERE status IN ('starting','active')").fetchall()
    for b in rows:
        bid = b["binding_id"]
        if procs.probe_alive(prober, b["cc_pid"], b["cc_start"]) == procs.DEAD:
            if terminate_binding(conn, bid, "cc_gone", clock):
                actions.append((bid, "cc_gone"))
            continue
        if b["status"] != "active":
            continue  # starting 由激活超时/pending TTL 负责
        beat = b["listener_beat_at"]
        stale = beat is None or (now - beat) > constants.HEARTBEAT_GRACE_MS
        if not stale:
            if b["suspect_since"] is not None:
                conn.execute(
                    "UPDATE bindings SET suspect_since=NULL WHERE binding_id=? AND status='active'",
                    (bid,))
            continue
        if in_suspect_window:
            continue  # 睡眠恢复宽限:等 listener 自愈
        if b["suspect_since"] is None:
            conn.execute(
                "UPDATE bindings SET suspect_since=? WHERE binding_id=? AND status='active'",
                (now, bid))
        elif now - b["suspect_since"] >= constants.SUSPECT_CONFIRM_MS:
            if terminate_binding(conn, bid, "listener_gone", clock, new_status="dead"):
                actions.append((bid, "listener_gone"))
    return actions
