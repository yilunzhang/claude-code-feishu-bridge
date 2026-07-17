"""出站(plan 4.5):daemon 唯一发送者。线性化点 = pending→sending 的 per-kind 守卫 CAS
(rowcount=1 才发);网络在事务外;结果契约解析(F10:缺字段=UNKNOWN 非成功);
unknown 同 key 自动重试一次(S4);组内/组间/同 chat 通知有序。"""
from . import constants, db, runner as runner_mod, util

NOTICE_KINDS = ("decision_notice", "lifecycle_notice", "unsupported_notice", "inbound_notice")


class Outbound:
    def __init__(self, conn, cfg, runner, clock, heartbeat=None, log=None):
        self.conn = conn
        self.cfg = cfg
        self.runner = runner
        self.clock = clock
        self.heartbeat = heartbeat  # r2-M1②:每次发送完 touch last_loop_at
        self.log = log              # E4a 可观测性:unknown/failed 留原始 rc/stdout/stderr 现场

    # ------------------------------------------------------------------
    def startup_scan(self):
        """启动扫描:遗留 sending → unknown(崩溃于发送中)。"""
        now = self.clock.wall_ms()
        self.conn.execute(
            "UPDATE outbound_jobs SET state='unknown', error='crashed-mid-send', "
            "next_attempt_at=? WHERE state='sending'", (now,))

    def tick(self, budget=constants.OUTBOUND_BATCH):
        now = self.clock.wall_ms()
        # 修复项1:指纹/版本门 degraded → 出站停摆(入站照常入库;门由 FingerprintGate 管理)
        gate = db.get_state(self.conn, "outbound_gate", "ok") or "ok"
        if gate != "ok":
            return 0
        rows = self.conn.execute(
            "SELECT * FROM outbound_jobs WHERE (state='pending' "
            "AND (next_attempt_at IS NULL OR next_attempt_at<=?)) "
            "OR (state='unknown' AND attempt_count<? AND next_attempt_at IS NOT NULL "
            "AND next_attempt_at<=?) ORDER BY job_seq",
            (now, constants.MAX_SEND_ATTEMPTS, now)).fetchall()
        sends = 0
        for job in rows:
            if sends >= budget:
                break
            if self._prepare(job) == "send":
                self._send_and_finalize(job["job_id"])
                sends += 1
                if self.heartbeat is not None:
                    try:
                        self.heartbeat()
                    except Exception:
                        pass
        return sends

    # ------------------------------------------------------------------
    def _prepare(self, job):
        """短事务:重读状态 → 顺序门(skip)→ 守卫(不满足→cancelled)→ CAS →sending。"""
        with db.tx(self.conn):
            fresh = self.conn.execute(
                "SELECT * FROM outbound_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
            if fresh is None or fresh["state"] not in ("pending", "unknown"):
                return "gone"
            # r3-1③(E2 盖全兜底):allowlist 生效且 job.chat_id 不在列 → cancelled
            allow = self.cfg.get("chat_allowlist")
            if allow and fresh["chat_id"] not in allow:
                db.cas(self.conn,
                       "UPDATE outbound_jobs SET state='cancelled' "
                       "WHERE job_id=? AND state IN ('pending','unknown')",
                       (fresh["job_id"],))
                return "cancelled"
            now = self.clock.wall_ms()
            if fresh["state"] == "unknown":
                if fresh["attempt_count"] >= constants.MAX_SEND_ATTEMPTS:
                    return "skip"
                if fresh["next_attempt_at"] is None or fresh["next_attempt_at"] > now:
                    return "skip"
            elif fresh["next_attempt_at"] is not None and fresh["next_attempt_at"] > now:
                return "skip"  # 重臂退避(修复项3):pending 也尊重 next_attempt_at
            order = self._order_gate(fresh)
            if order == "skip":
                return "skip"
            if order == "cancel" or not self._guard_ok(fresh):
                db.cas(self.conn,
                       "UPDATE outbound_jobs SET state='cancelled' "
                       "WHERE job_id=? AND state IN ('pending','unknown')",
                       (fresh["job_id"],))
                return "cancelled"
            db.cas(self.conn,
                   "UPDATE outbound_jobs SET state='sending', sending_at=?, "
                   "attempt_count=attempt_count+1 WHERE job_id=? AND state IN ('pending','unknown')",
                   (self.clock.wall_ms(), fresh["job_id"]))
            return "send"

    def _order_gate(self, job):
        # 组内 chunk 严格序:前块非 sent 后块不发;前块 failed/cancelled → 本块 cancel
        if job["turn_group"] is not None and (job["chunk_index"] or 0) > 0:
            prev = self.conn.execute(
                "SELECT state FROM outbound_jobs WHERE turn_group=? AND chunk_index=?",
                (job["turn_group"], job["chunk_index"] - 1)).fetchone()
            if prev is None:
                return "skip"
            if prev["state"] in ("failed", "cancelled"):
                return "cancel"
            if prev["state"] != "sent":
                return "skip"
        # 同 binding 的 turn_group 间有序:前组存在 unknown(或在发)后组不发
        if job["kind"] == "session_turn":
            blocker = self.conn.execute(
                "SELECT 1 FROM outbound_jobs WHERE kind='session_turn' AND binding_id=? "
                "AND job_seq<? AND turn_group IS NOT NULL AND turn_group!=? "
                "AND state IN ('unknown','sending') LIMIT 1",
                (job["binding_id"], job["job_seq"], job["turn_group"] or "")).fetchone()
            if blocker:
                return "skip"
        # 同 chat 通知按 job_seq
        if job["kind"] in NOTICE_KINDS:
            qs = ",".join("?" for _ in NOTICE_KINDS)
            blocker = self.conn.execute(
                f"SELECT 1 FROM outbound_jobs WHERE chat_id=? AND job_seq<? "
                f"AND kind IN ({qs}) AND state IN ('pending','sending','unknown') LIMIT 1",
                (job["chat_id"], job["job_seq"], *NOTICE_KINDS)).fetchone()
            if blocker:
                return "skip"
        return None

    def _guard_ok(self, job):
        """per-kind 守卫(I2/4.5;结构化字段,不解析 body)。"""
        kind = job["kind"]
        if kind == "session_turn":
            return self._binding_active(job["binding_id"])
        if kind == "approval_card":
            p = self._pending(job["ref_pending_id"])
            return (p is not None and p["state"] == "pending"
                    and p["card_message_id"] is None
                    and self._binding_active(job["binding_id"]))
        if kind == "decision_notice":
            exp = job["expected_state"]
            if exp in ("approved", "rejected", "expired"):
                p = self._pending(job["ref_pending_id"])
                return p is not None and p["state"] == exp
            if exp == "failed":
                r = self._inbox(job["ref_message_id"])
                return r is not None and r["state"] == "failed"
            return False
        if kind == "lifecycle_notice":
            b = self.conn.execute("SELECT * FROM bindings WHERE binding_id=?",
                                  (job["binding_id"],)).fetchone()
            if b is None:
                return False
            exp = job["expected_state"] or ""
            if exp == "active":
                return b["status"] == "active"
            if ":" in exp:
                st, reason = exp.split(":", 1)
                return b["status"] == st and (b["close_reason"] or "") == reason
            return False
        if kind == "unsupported_notice":
            r = self._inbox(job["ref_message_id"])
            return r is not None and r["state"] == "unsupported"
        if kind == "inbound_notice":
            r = self._inbox(job["ref_message_id"])
            return r is not None and r["state"] == (job["expected_state"] or "")
        if kind == "receipt_reaction":
            d = self.conn.execute("SELECT state FROM deliveries WHERE delivery_seq=?",
                                  (job["ref_delivery_seq"],)).fetchone()
            return d is not None and d["state"] != "dropped"
        return False

    def _binding_active(self, binding_id):
        b = self.conn.execute("SELECT status FROM bindings WHERE binding_id=?",
                              (binding_id,)).fetchone()
        return b is not None and b["status"] == "active"

    def _pending(self, pending_id):
        return self.conn.execute("SELECT * FROM pendings WHERE pending_id=?",
                                 (pending_id,)).fetchone()

    def _inbox(self, message_id):
        return self.conn.execute("SELECT * FROM inbox WHERE message_id=?",
                                 (message_id,)).fetchone()

    # ------------------------------------------------------------------
    def _send_and_finalize(self, job_id):
        job = self.conn.execute("SELECT * FROM outbound_jobs WHERE job_id=?",
                                (job_id,)).fetchone()
        if job is None or job["state"] != "sending":
            return
        outcome, detail = self._transmit(job)  # 网络在事务外
        now = self.clock.wall_ms()
        with db.tx(self.conn):
            if outcome == "sent":
                db.cas(self.conn,
                       "UPDATE outbound_jobs SET state='sent', sent_message_id=?, sent_at=?, "
                       "error=NULL WHERE job_id=? AND state='sending'",
                       (detail, now, job_id))
                if job["kind"] == "approval_card" and detail:
                    # 发出后回填 card_message_id(4.2.7)
                    self.conn.execute(
                        "UPDATE pendings SET card_message_id=? "
                        "WHERE pending_id=? AND card_message_id IS NULL",
                        (detail, job["ref_pending_id"]))
            elif outcome == "failed":
                db.cas(self.conn,
                       "UPDATE outbound_jobs SET state='failed', error=? "
                       "WHERE job_id=? AND state='sending'", (detail, job_id))
            else:  # unknown
                fresh = self.conn.execute(
                    "SELECT attempt_count FROM outbound_jobs WHERE job_id=?",
                    (job_id,)).fetchone()
                retryable = fresh is not None and fresh["attempt_count"] < constants.MAX_SEND_ATTEMPTS
                db.cas(self.conn,
                       "UPDATE outbound_jobs SET state='unknown', error=?, next_attempt_at=? "
                       "WHERE job_id=? AND state='sending'",
                       (detail, (now + constants.UNKNOWN_RETRY_DELAY_MS) if retryable else None,
                        job_id))

    def _transmit(self, job):
        """→ ('sent', message_id|None) | ('failed', err) | ('unknown', err)。"""
        kind = job["kind"]
        if kind == "receipt_reaction":
            params = util.jdumps({"message_id": job["ref_message_id"]})
            data = util.jdumps({"reaction_type": {"emoji_type": job["body"] or "GLANCE"}})
            res = self.runner.run(
                ["im", "reactions", "create", "--as", "bot", "--params", params,
                 "--data", data], timeout_s=constants.SEND_TIMEOUT_S)
            env = runner_mod.parse_result(res)  # E4a:stderr 信封回退
            # 修复项9:成功必须解析出 .data.reaction_id(F10:缺字段=非成功)
            if res.rc == 0 and runner_mod.envelope_ok(env) \
                    and runner_mod.data_of(env).get("reaction_id"):
                return ("sent", None)
            self._log_failure(job, "failed", res)
            return ("failed", "reaction failed")  # 失败即 failed 不重试(4.5;S11 幂等)
        wire_key = util.short_key(job["idempotency_key"])  # E4b:wire 一律短键(≤40)
        if kind == "approval_card":
            argv = ["im", "+messages-reply", "--as", "bot",
                    "--message-id", job["reply_to"], "--msg-type", "interactive",
                    "--content", job["body"] or "{}",
                    "--idempotency-key", wire_key]
        else:
            argv = ["im", "+messages-send", "--as", "bot",
                    "--chat-id", job["chat_id"], "--text", job["body"] or "",
                    "--idempotency-key", wire_key]
        res = self.runner.run(argv, timeout_s=constants.SEND_TIMEOUT_S)
        env = runner_mod.parse_result(res)  # E4a:stdout → 空/不可解析回退 stderr
        if runner_mod.envelope_ok(env):
            mid = runner_mod.data_of(env).get("message_id")
            if mid:
                return ("sent", mid)
            self._log_failure(job, "unknown", res)
            return ("unknown", "ok-without-message_id")
        code = runner_mod.envelope_error_code(env)  # 顶层 code / 嵌套 .error.code 双形状
        if env is not None and env.get("ok") is False and code is not None:
            outcome = classify_error_code(code)
            self._log_failure(job, outcome, res)
            return (outcome, f"code={code} {runner_mod.envelope_error_msg(env)}".strip())
        self._log_failure(job, "unknown", res)
        if res.timed_out:
            return ("unknown", "timeout")
        return ("unknown", f"rc={res.rc} unparseable-envelope")

    def _log_failure(self, job, outcome, res):
        """E4a 可观测性:非 sent 结果把原始现场(rc/stdout/stderr 截断)记入 daemon.log。"""
        if self.log is None:
            return
        try:
            self.log(
                f"send {job['kind']} key={job['idempotency_key']} -> {outcome}: "
                f"rc={res.rc} timed_out={res.timed_out} "
                f"stdout={(res.stdout or '')[:300]!r} stderr={(res.stderr or '')[:300]!r}")
        except Exception:
            pass


def classify_error_code(code):
    """修复项4:表驱动错误分类。永久集→failed;瞬态集→unknown(同 key 自动重试仍≤1);
    未知 code→unknown(留人工,status 高亮)。"""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "unknown"
    if code in constants.PERMANENT_SEND_CODES:
        return "failed"
    if code in constants.TRANSIENT_SEND_CODES:
        return "unknown"
    return "unknown"
