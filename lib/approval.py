"""审批回调(plan 4.3):执行门=纯机械(operator 比对 + 单事务 CAS),零模型判断。
有效回调 = 单事务:INSERT callback_events ∧ 机械校验 ∧ CAS ∧ 入队/物化 ∧ 通知;
任何一步失败整体回滚(重放可再处理)。无效/重复:仅裸 INSERT OR IGNORE。"""
import hmac
import json
import sqlite3

from . import constants, db, jobs, texts


class _Dup(Exception):
    pass


class _AbortLate(Exception):
    pass


def normalize_callback(obj):
    if not isinstance(obj, dict):
        return None
    cands = [obj] + [obj.get(k) for k in ("event", "payload", "data")
                     if isinstance(obj.get(k), dict)]
    for c in cands:
        if c.get("action_value") is not None and c.get("event_id"):
            return c
    return None


class Approval:
    def __init__(self, conn, cfg, clock, inbound):
        self.conn = conn
        self.cfg = cfg
        self.clock = clock
        self.inbound = inbound

    def process_event(self, ev, seam=None):
        ev = normalize_callback(ev)
        if not ev:
            return "skipped"
        event_id = ev["event_id"]
        av = ev.get("action_value")
        if isinstance(av, str):
            try:
                av = json.loads(av)
            except ValueError:
                av = None
        valid, ctx = self._validate(ev, av)
        if not valid:
            self._record_bare(event_id)
            return "invalid"
        pending, inbox_row, act = ctx
        now = self.clock.wall_ms()
        outcome = "approved" if act == "approve" else "rejected"
        media_followup = False
        try:
            with db.tx(self.conn):
                try:
                    self.conn.execute(
                        "INSERT INTO callback_events(event_id,seen_at) VALUES(?,?)",
                        (event_id, now))
                except sqlite3.IntegrityError:
                    raise _Dup()
                ok = db.cas(
                    self.conn,
                    "UPDATE pendings SET state=?, decided_by=?, decided_event_id=?, decided_at=? "
                    "WHERE pending_id=? AND state='pending'",
                    (outcome, ev.get("operator_id"), event_id, now, pending["pending_id"]))
                if not ok:
                    raise _AbortLate()  # 晚点击已终态 → CAS 拒绝
                if seam:
                    seam("after_cas")
                if act == "approve":
                    snap = json.loads(inbox_row["snapshot_json"])
                    mtype = snap.get("msg_type")
                    binding = self.conn.execute(
                        "SELECT * FROM bindings WHERE binding_id=?",
                        (pending["binding_id"],)).fetchone()
                    if mtype in constants.MEDIA_MSG_TYPES:
                        # 有媒体:approved_materializing,批准后才下载(4.2.7)
                        if not db.cas(self.conn,
                                      "UPDATE inbox SET state='approved_materializing', ts=? "
                                      "WHERE message_id=? AND state='awaiting_approval'",
                                      (now, inbox_row["message_id"])):
                            raise _AbortLate()
                        media_followup = True
                    else:
                        done = self.inbound._enqueue_in_tx(
                            inbox_row, binding, snap, "awaiting_approval", now,
                            approved_by=ev.get("operator_id"), create_receipt=False)
                        if not done:
                            raise _AbortLate()  # 绑定复验失败:整体回滚,重放可再判
                        jobs.create_job(
                            self.conn, kind="decision_notice", chat_id=inbox_row["chat_id"],
                            binding_id=pending["binding_id"],
                            idempotency_key=jobs.key_dec(pending["pending_id"], "approved"),
                            ref_pending_id=pending["pending_id"], expected_state="approved",
                            body=texts.decision_notice_body("approved"), now=now)
                else:
                    db.cas(self.conn,
                           "UPDATE inbox SET state='rejected', ts=? "
                           "WHERE message_id=? AND state='awaiting_approval'",
                           (now, inbox_row["message_id"]))
                    jobs.create_job(
                        self.conn, kind="decision_notice", chat_id=inbox_row["chat_id"],
                        binding_id=pending["binding_id"],
                        idempotency_key=jobs.key_dec(pending["pending_id"], "rejected"),
                        ref_pending_id=pending["pending_id"], expected_state="rejected",
                        body=texts.decision_notice_body("rejected"), now=now)
                if seam:
                    seam("before_commit")
        except _Dup:
            return "dup"
        except _AbortLate:
            self._record_bare(event_id)
            return "late"
        if media_followup:
            row = self.conn.execute(
                "SELECT * FROM inbox WHERE message_id=?",
                (inbox_row["message_id"],)).fetchone()
            self.inbound.drive_row(row)  # 立即物化尝试(网络在事务外;失败由恢复工人接手)
        return "applied"

    def _record_bare(self, event_id):
        self.conn.execute(
            "INSERT OR IGNORE INTO callback_events(event_id,seen_at) VALUES(?,?)",
            (event_id, self.clock.wall_ms()))

    def _validate(self, ev, av):
        """机械校验(reads 在事务外;真正防线=事务内 CAS)。"""
        if not isinstance(av, dict):
            return False, None
        act = av.get("act")
        if act not in ("approve", "reject"):
            return False, None
        pid, nonce = av.get("pending_id"), av.get("nonce")
        if not pid or not isinstance(nonce, str):
            return False, None
        pending = self.conn.execute(
            "SELECT * FROM pendings WHERE pending_id=?", (pid,)).fetchone()
        if pending is None:
            return False, None
        try:
            same = hmac.compare_digest(str(pending["nonce"]).encode("utf-8"),
                                       nonce.encode("utf-8"))
        except (UnicodeError, TypeError):
            same = False
        if not same:
            return False, None
        if ev.get("operator_id") != self.cfg["owner_open_id"]:
            return False, None
        inbox_row = self.conn.execute(
            "SELECT * FROM inbox WHERE message_id=?", (pending["message_id"],)).fetchone()
        if inbox_row is None:
            return False, None
        if ev.get("chat_id") and ev["chat_id"] != inbox_row["chat_id"]:
            return False, None
        # card_message_id 仅在已回填时校验(回填前点击自愈,4.3)
        if pending["card_message_id"] and ev.get("message_id") \
                and ev["message_id"] != pending["card_message_id"]:
            return False, None
        return True, (pending, inbox_row, act)
