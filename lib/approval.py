"""审批回调(plan 4.3):执行门=纯机械(operator 比对 + 单事务 CAS),零模型判断。
有效回调 = 单事务:INSERT callback_events ∧ 机械校验(**在 BEGIN IMMEDIATE 事务内重读
pending/inbox/card 状态**,修复项6)∧ CAS ∧ 入队/物化 ∧ 通知;任何一步失败整体回滚
(重放可再处理)。无效/重复:仅裸 INSERT OR IGNORE。
fail-closed:envelope 缺 chat_id、或卡片已回填后缺/不匹配 message_id → 一律 REJECT。"""
import hmac
import json
import sqlite3

from . import constants, db, jobs, texts, util


class _Dup(Exception):
    pass


class _Invalid(Exception):
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
        if isinstance(av, dict) and av.get("act") == constants.SAY_ACT:
            return self._process_say(ev, av, event_id)   # 与审批卡无关的独立分支
        now = self.clock.wall_ms()
        media_followup = False
        inbox_message_id = None
        try:
            with db.tx(self.conn):
                try:
                    self.conn.execute(
                        "INSERT INTO callback_events(event_id,seen_at) VALUES(?,?)",
                        (event_id, now))
                except sqlite3.IntegrityError:
                    raise _Dup()
                if seam:
                    seam("in_tx_before_validate")
                # 机械校验:事务内重读(修复项6;此前在事务外读=可被并发回填绕过)
                valid, ctx = self._validate_in_tx(ev, av)
                if not valid:
                    raise _Invalid()
                pending, inbox_row, act = ctx
                inbox_message_id = inbox_row["message_id"]
                outcome = "approved" if act == "approve" else "rejected"
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
        except _Invalid:
            self._record_bare(event_id)
            return "invalid"
        except _AbortLate:
            self._record_bare(event_id)
            return "late"
        if media_followup:
            row = self.conn.execute(
                "SELECT * FROM inbox WHERE message_id=?", (inbox_message_id,)).fetchone()
            self.inbound.drive_row(row)  # 立即物化尝试(网络在事务外;失败由恢复工人接手)
        return "applied"

    # ---------------- card say:owner 点自定义卡片按钮 ----------------
    def _process_say(self, ev, av, event_id):
        """owner 点自定义卡片按钮 = owner 在绑定群说了按钮上的那句话。
        单事务:INSERT callback_events(去重) ∧ 机械校验 ∧ INSERT inbox(直接终态 enqueued)
        ∧ INSERT deliveries ∧ 卡片上的 👀 回执;无效/重复:仅裸去重,零投递。
        payload 与普通 owner 文本同形(sender_is_owner=true),另带 card_message_id / action_value;
        message_id 是合成的 `card:<event_id>`(只用于去重,不是飞书消息)。"""
        now = self.clock.wall_ms()
        try:
            with db.tx(self.conn):
                try:
                    self.conn.execute(
                        "INSERT INTO callback_events(event_id,seen_at) VALUES(?,?)",
                        (event_id, now))
                except sqlite3.IntegrityError:
                    raise _Dup()
                ctx = self._validate_say_in_tx(ev, av)
                if ctx is None:
                    raise _Invalid()
                binding, text, card_mid = ctx
                mid = "card:" + str(event_id)
                operator = ev.get("operator_id")
                self.conn.execute(
                    "INSERT INTO inbox(event_id,message_id,chat_id,binding_id,sender_open_id,"
                    "sender_type,message_type,snapshot_json,state,ts) "
                    "VALUES(?,?,?,?,?,?,?,?,'enqueued',?)",
                    (mid, mid, ev["chat_id"], binding["binding_id"], operator, "user",
                     constants.CARD_ACTION_MSG_TYPE,
                     util.jdumps({"card_message_id": card_mid, "action_value": av}), now))
                payload = {
                    "message_id": mid,
                    "chat_id": ev["chat_id"],
                    "sender_open_id": operator,
                    "sender_type": "user",
                    "sender_is_owner": True,
                    "approved_by": None,
                    "message_type": constants.CARD_ACTION_MSG_TYPE,
                    "text": text,
                    "media_paths": [],
                    "card_message_id": card_mid,
                    "action_value": av,
                }
                cur = self.conn.execute(
                    "INSERT INTO deliveries(binding_id,message_id,payload_json,state,enq_at) "
                    "VALUES(?,?,?,'enqueued',?)",
                    (binding["binding_id"], mid, util.jdumps(payload), now))
                seq = cur.lastrowid
                # 👀 打在被点的卡片上(点击已收到的即时反馈);失败即 failed 不重试,不挡队列
                jobs.create_job(
                    self.conn, kind="receipt_reaction", chat_id=ev["chat_id"],
                    binding_id=binding["binding_id"], idempotency_key=jobs.key_rc(seq),
                    ref_delivery_seq=seq, ref_message_id=card_mid, body="GLANCE", now=now)
        except _Dup:
            return "dup"
        except _Invalid:
            self._record_bare(event_id)
            return "invalid"
        return "said"

    def _validate_say_in_tx(self, ev, av):
        """机械校验(fail-closed);调用方保证已在写事务内。→ (binding, text, card_message_id) | None"""
        if ev.get("operator_id") != self.cfg["owner_open_id"]:
            return None  # 只认 owner 本人点
        chat_id, card_mid = ev.get("chat_id"), ev.get("message_id")
        if not chat_id or not card_mid:
            return None
        allow = self.cfg.get("chat_allowlist")
        if allow and chat_id not in allow:
            return None
        text = av.get("text")
        if not isinstance(text, str):
            return None
        text = text.strip()
        if not text or len(text) > constants.SAY_TEXT_MAX:
            return None
        binding = self.conn.execute(
            "SELECT * FROM bindings WHERE chat_id=? AND status='active' "
            "ORDER BY binding_seq DESC LIMIT 1", (chat_id,)).fetchone()
        if binding is None:
            return None  # 只认已绑定且 active 的群(starting/closed 都不投)
        return binding, text, card_mid

    def _record_bare(self, event_id):
        self.conn.execute(
            "INSERT OR IGNORE INTO callback_events(event_id,seen_at) VALUES(?,?)",
            (event_id, self.clock.wall_ms()))

    def _validate_in_tx(self, ev, av):
        """机械校验;调用方保证已在写事务内(读到的就是被 CAS 保护的同一视图)。
        fail-closed:缺字段一律拒(不再"缺字段跳过校验")。"""
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
        # r3-1①(E2 盖全):遗留 pending 来自列外群 → invalid(裸去重,零 CAS 零 delivery)
        allow = self.cfg.get("chat_allowlist")
        if allow:
            b = self.conn.execute("SELECT chat_id FROM bindings WHERE binding_id=?",
                                  (pending["binding_id"],)).fetchone()
            if b is None or b["chat_id"] not in allow:
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
        if not ev.get("chat_id") or ev["chat_id"] != inbox_row["chat_id"]:
            return False, None  # 缺 chat_id 也拒(fail-closed)
        # 卡片已回填 → 回调必须携带且匹配 card_message_id;回填前点击自愈(4.3)
        if pending["card_message_id"]:
            if not ev.get("message_id") or ev["message_id"] != pending["card_message_id"]:
                return False, None
        return True, (pending, inbox_row, act)
