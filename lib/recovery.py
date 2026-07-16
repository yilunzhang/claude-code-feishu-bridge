"""恢复工人(plan 4.8):驱动一切非终态;重驱只用行上钉死的 binding_id;
幂等=DB 约束(唯一键 + 确定性 job 键 + 带旧状态 CAS)。
r7-②:waiting_binding 专用分支先行,通用 undeliverable/dropped 分支绝不截获 waiting 行
(通用清扫的 SELECT 按状态显式排除 waiting_binding,结构上不可能碰到)。"""
import json
import os
import shutil

from . import constants, db, jobs, lifecycle, texts, util


class Recovery:
    def __init__(self, conn, cfg, runner, clock, inbound, prober):
        self.conn = conn
        self.cfg = cfg
        self.runner = runner
        self.clock = clock
        self.inbound = inbound
        self.prober = prober

    # ---------------- 快节奏(每 loop ~1s / 判死 ~5s) ----------------
    def fast_tick(self, in_suspect_window=False):
        # confirmed starting → 激活 / 30s 超时
        for b in self.conn.execute(
                "SELECT binding_id FROM bindings WHERE status='starting' "
                "AND bind_phase='confirmed'").fetchall():
            lifecycle.activate_if_ready(self.conn, b["binding_id"], self.clock)
        # waiting_binding 专用分支(激活重过分流门 / 终止映射)
        self.inbound.drive_waiting_rows()
        # 判死
        lifecycle.death_scan(self.conn, self.prober, self.clock,
                             in_suspect_window=in_suspect_window)

    # ---------------- 慢节奏(启动 + 每 60s) ----------------
    def slow_tick(self):
        now = self.clock.wall_ms()
        # r7-②:waiting 专用分支先行
        self.inbound.drive_waiting_rows()
        self._redrive_resolving(now)
        self._replenish_cards(now)
        self._redrive_materializing(now)
        self._expire_pendings(now)
        lifecycle.expire_stale_pending_binds(self.conn, self.clock)
        self._close_orphan_starting(now)
        self._reclaim_leases(now)
        self._legacy_sending(now)
        self._retention(now)

    # ------------------------------------------------------------------
    def _redrive_resolving(self, now):
        rows = self.conn.execute(
            "SELECT * FROM inbox WHERE state IN ('received','resolving')").fetchall()
        for r in rows:
            if r["ts"] is not None and now - r["ts"] > constants.RESOLVE_DEADLINE_MS:
                # 有限重试到期 → failed + 静默(未确认@bot 绝不回群,4.2.2)
                with db.tx(self.conn):
                    db.cas(self.conn,
                           "UPDATE inbox SET state='failed', ts=? "
                           "WHERE message_id=? AND state IN ('received','resolving')",
                           (now, r["message_id"]))
                    db.bump_counter(self.conn, "resolve_deadline_failed")
                continue
            self.inbound.drive_row(r)

    def _replenish_cards(self, now):
        """awaiting_approval 无卡片 job → 补发;已 sent 未回填 card_message_id → 补回填。"""
        rows = self.conn.execute(
            "SELECT p.*, i.chat_id AS chat_id, i.snapshot_json AS snapshot_json "
            "FROM pendings p JOIN inbox i ON p.message_id=i.message_id "
            "WHERE p.state='pending'").fetchall()
        for p in rows:
            job = self.conn.execute(
                "SELECT * FROM outbound_jobs WHERE idempotency_key=?",
                (jobs.key_card(p["pending_id"]),)).fetchone()
            if job is None:
                try:
                    snap = json.loads(p["snapshot_json"] or "{}")
                except ValueError:
                    snap = {}
                from . import inbound as inbound_mod
                sender = (snap.get("sender") or {}).get("id") or "?"
                jobs.create_job(
                    self.conn, kind="approval_card", chat_id=p["chat_id"],
                    binding_id=p["binding_id"], reply_to=p["message_id"],
                    idempotency_key=jobs.key_card(p["pending_id"]),
                    ref_pending_id=p["pending_id"], expected_state="pending",
                    body=texts.build_approval_card(
                        p["pending_id"], p["nonce"], sender,
                        inbound_mod.extract_text(snap)),
                    now=now)
            elif (p["card_message_id"] is None and job["state"] == "sent"
                    and job["sent_message_id"]):
                self.conn.execute(
                    "UPDATE pendings SET card_message_id=? "
                    "WHERE pending_id=? AND card_message_id IS NULL",
                    (job["sent_message_id"], p["pending_id"]))

    def _redrive_materializing(self, now):
        rows = self.conn.execute(
            "SELECT * FROM inbox WHERE state='approved_materializing'").fetchall()
        for r in rows:
            b = None
            if r["binding_id"]:
                b = self.conn.execute(
                    "SELECT status FROM bindings WHERE binding_id=?",
                    (r["binding_id"],)).fetchone()
            if b is None or b["status"] != "active":
                # 通用分支:绑定复验失败 → undeliverable(只针对非 waiting 状态,r7-②)
                with db.tx(self.conn):
                    db.cas(self.conn,
                           "UPDATE inbox SET state='undeliverable', ts=? "
                           "WHERE message_id=? AND state='approved_materializing'",
                           (now, r["message_id"]))
                continue
            if r["ts"] is not None and now - r["ts"] > constants.MATERIALIZE_DEADLINE_MS:
                p = self.conn.execute(
                    "SELECT * FROM pendings WHERE message_id=?",
                    (r["message_id"],)).fetchone()
                with db.tx(self.conn):
                    if db.cas(self.conn,
                              "UPDATE inbox SET state='failed', ts=? "
                              "WHERE message_id=? AND state='approved_materializing'",
                              (now, r["message_id"])) and p is not None:
                        jobs.create_job(
                            self.conn, kind="decision_notice", chat_id=r["chat_id"],
                            binding_id=r["binding_id"],
                            idempotency_key=jobs.key_dec(p["pending_id"], "failed"),
                            ref_pending_id=p["pending_id"], ref_message_id=r["message_id"],
                            expected_state="failed",
                            body=texts.decision_notice_body("failed"), now=now)
                continue
            self.inbound.drive_row(r)

    def _expire_pendings(self, now):
        rows = self.conn.execute(
            "SELECT p.*, i.chat_id AS chat_id FROM pendings p "
            "JOIN inbox i ON p.message_id=i.message_id "
            "WHERE p.state='pending' AND p.created_at IS NOT NULL AND p.created_at+?<?",
            (constants.PENDING_TTL_MS, now)).fetchall()
        for p in rows:
            with db.tx(self.conn):
                if not db.cas(self.conn,
                              "UPDATE pendings SET state='expired', decided_at=? "
                              "WHERE pending_id=? AND state='pending'",
                              (now, p["pending_id"])):
                    continue
                db.cas(self.conn,
                       "UPDATE inbox SET state='expired', ts=? "
                       "WHERE message_id=? AND state='awaiting_approval'",
                       (now, p["message_id"]))
                jobs.create_job(
                    self.conn, kind="decision_notice", chat_id=p["chat_id"],
                    binding_id=p["binding_id"],
                    idempotency_key=jobs.key_dec(p["pending_id"], "expired"),
                    ref_pending_id=p["pending_id"], expected_state="expired",
                    body=texts.decision_notice_body("expired"), now=now)

    def _close_orphan_starting(self, now):
        """安全网:unconfirmed starting 且其 pending_bind 已终态 → bind_timeout 终止。"""
        rows = self.conn.execute(
            "SELECT b.binding_id FROM bindings b "
            "LEFT JOIN pending_bind pb ON pb.request_id=b.binding_id AND pb.state='pending' "
            "WHERE b.status='starting' AND b.bind_phase='unconfirmed' "
            "AND pb.request_id IS NULL").fetchall()
        for r in rows:
            lifecycle.terminate_binding(self.conn, r["binding_id"], "bind_timeout", self.clock)

    def _reclaim_leases(self, now):
        rows = self.conn.execute(
            "SELECT d.*, b.status AS b_status FROM deliveries d "
            "JOIN bindings b ON b.binding_id=d.binding_id "
            "WHERE d.state='leased' AND d.lease_until IS NOT NULL AND d.lease_until<?",
            (now,)).fetchall()
        for d in rows:
            target = "enqueued" if d["b_status"] == "active" else "dropped"
            db.cas(self.conn,
                   "UPDATE deliveries SET state=?, lease_token=NULL, lease_epoch=NULL, "
                   "lease_pid=NULL, lease_start=NULL, lease_until=NULL "
                   "WHERE delivery_seq=? AND state='leased' AND lease_until<?",
                   (target, d["delivery_seq"], now))

    def _legacy_sending(self, now):
        self.conn.execute(
            "UPDATE outbound_jobs SET state='unknown', error='stale-sending', "
            "next_attempt_at=? WHERE state='sending' AND sending_at IS NOT NULL "
            "AND sending_at<?", (now, now - 2 * constants.SEND_TIMEOUT_S * 1000))

    # ------------------------------------------------------------------
    def _retention(self, now):
        """终态行 retention:正文裁剪、骨架保留;终态 media TTL 删(非终态禁删)。"""
        cutoff = now - constants.RETENTION_MS
        qs = ",".join("?" for _ in constants.INBOX_TERMINAL_STATES)
        self.conn.execute(
            f"UPDATE inbox SET snapshot_json=NULL WHERE state IN ({qs}) "
            "AND ts IS NOT NULL AND ts<? AND snapshot_json IS NOT NULL",
            (*constants.INBOX_TERMINAL_STATES, cutoff))
        self.conn.execute(
            "UPDATE deliveries SET payload_json='{}' WHERE state IN ('emitted','dropped') "
            "AND enq_at IS NOT NULL AND enq_at<? AND payload_json!='{}'", (cutoff,))
        self.conn.execute(
            "UPDATE outbound_jobs SET body=NULL WHERE state IN ('sent','failed','cancelled') "
            "AND created_at IS NOT NULL AND created_at<? AND body IS NOT NULL", (cutoff,))
        # media:仅终态消息的目录可删
        media_root = self.inbound.media_root
        try:
            binding_dirs = os.listdir(media_root)
        except OSError:
            return
        for bdir in binding_dirs:
            bpath = os.path.join(media_root, bdir)
            if not os.path.isdir(bpath):
                continue
            for mdir in os.listdir(bpath):
                mpath = os.path.join(bpath, mdir)
                if not os.path.isdir(mpath) or mdir.startswith(".tmp"):
                    continue
                row = self.conn.execute(
                    "SELECT state, ts FROM inbox WHERE message_id=?", (mdir,)).fetchone()
                if row is None:
                    continue
                if row["state"] in constants.INBOX_TERMINAL_STATES \
                        and row["ts"] is not None and row["ts"] < cutoff:
                    shutil.rmtree(mpath, ignore_errors=True)
