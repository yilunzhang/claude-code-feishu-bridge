"""恢复工人(plan 4.8):非终态收口全清单 + retention + 崩溃残留收口。"""
import json
import os

from tests.conftest import APP_ID, CHAT, MEMBER, OWNER
from tests.helpers import bot_mention, mget_snapshot, err_envelope, ok_envelope
from lib import constants, db as dbmod, jobs


class TestResolvingRedrive:
    def test_retry_then_success(self, env):
        bid = env.make_binding(status="active")
        flag = {"fail": True}

        def fn(args, cwd):
            if flag["fail"]:
                return err_envelope(500)
            return ok_envelope({"messages": [mget_snapshot(
                "om_1", CHAT, OWNER, mentions=[bot_mention(APP_ID)])]})

        env.runner.on(lambda a: a[:2] == ["im", "+messages-mget"], fn)
        env.recv_event()
        assert env.inbox_row("om_1")["state"] == "resolving"
        flag["fail"] = False
        env.recovery.slow_tick()
        assert env.inbox_row("om_1")["state"] == "enqueued"

    def test_deadline_fails_silently(self, env):
        env.make_binding(status="active")
        env.runner.on(lambda a: a[:2] == ["im", "+messages-mget"],
                      lambda a, c: err_envelope(500))
        env.recv_event()
        env.clock.tick(constants.RESOLVE_DEADLINE_MS + 1)
        env.recovery.slow_tick()
        assert env.inbox_row("om_1")["state"] == "failed"
        assert env.jobs() == []  # 静默:未确认@bot 绝不回群


class TestCardReplenish:
    def test_missing_card_job_recreated(self, env):
        from tests.test_approval import member_pending
        p = member_pending(env)
        env.conn.execute("DELETE FROM outbound_jobs WHERE idempotency_key=?",
                         (jobs.key_card(p["pending_id"]),))
        env.recovery.slow_tick()
        j = env.conn.execute("SELECT * FROM outbound_jobs WHERE idempotency_key=?",
                             (jobs.key_card(p["pending_id"]),)).fetchone()
        assert j is not None and j["state"] == "pending"
        assert j["reply_to"] == "om_1"

    def test_card_message_id_backfilled_from_sent_job(self, env):
        from tests.test_approval import member_pending
        p = member_pending(env)
        env.conn.execute(
            "UPDATE outbound_jobs SET state='sent', sent_message_id='om_card_9' "
            "WHERE idempotency_key=?", (jobs.key_card(p["pending_id"]),))
        env.recovery.slow_tick()
        row = env.conn.execute("SELECT card_message_id FROM pendings WHERE pending_id=?",
                               (p["pending_id"],)).fetchone()
        assert row[0] == "om_card_9"


class TestMaterializingRedrive:
    def _approved_media(self, env):
        from tests.test_approval import member_pending, cb
        p = member_pending(env, mid="om_img", msg_type="image")
        env.runner.on(lambda a: "--download-resources" in a, lambda a, c: err_envelope(500))
        assert env.approval.process_event(cb(p)) == "applied"
        assert env.inbox_row("om_img")["state"] == "approved_materializing"
        return p

    def test_redrive_succeeds_later(self, env):
        p = self._approved_media(env)
        snap = mget_snapshot("om_img", CHAT, MEMBER, msg_type="image",
                             mentions=[bot_mention(APP_ID)])

        def dl(args, cwd):
            import pathlib
            d = pathlib.Path(cwd) / "lark-im-resources"
            d.mkdir(parents=True, exist_ok=True)
            (d / "x.png").write_bytes(b"P")
            return ok_envelope({"messages": [snap]})

        env.runner.responders.insert(0, (lambda a: "--download-resources" in a, dl))
        env.recovery.slow_tick()
        assert env.inbox_row("om_img")["state"] == "enqueued"
        assert len(env.deliveries()) == 1

    def test_deadline_fails_with_notice(self, env):
        p = self._approved_media(env)
        env.clock.tick(constants.MATERIALIZE_DEADLINE_MS + 1)
        env.recovery.slow_tick()
        assert env.inbox_row("om_img")["state"] == "failed"
        dec = [j["idempotency_key"] for j in env.jobs("decision_notice")]
        assert f"dec:{p['pending_id']}:failed" in dec

    def test_terminated_binding_goes_undeliverable(self, env):
        p = self._approved_media(env)
        bid = env.conn.execute("SELECT binding_id FROM pendings WHERE pending_id=?",
                               (p["pending_id"],)).fetchone()[0]
        # 直接把绑定置终态(绕过级联,模拟崩溃缝:pending 已 approved,不受级联 expire 影响)
        env.conn.execute(
            "UPDATE bindings SET status='closed', close_reason='cc_gone' WHERE binding_id=?",
            (bid,))
        env.recovery.slow_tick()
        assert env.inbox_row("om_img")["state"] == "undeliverable"


class TestPendingTTL:
    def test_expired_pending_notice_and_inbox(self, env):
        from tests.test_approval import member_pending
        p = member_pending(env)
        env.clock.tick(constants.PENDING_TTL_MS + 1)
        env.recovery.slow_tick()
        row = env.conn.execute("SELECT state FROM pendings WHERE pending_id=?",
                               (p["pending_id"],)).fetchone()
        assert row[0] == "expired"
        assert env.inbox_row("om_1")["state"] == "expired"
        dec = [j["idempotency_key"] for j in env.jobs("decision_notice")]
        assert f"dec:{p['pending_id']}:expired" in dec


class TestLeaseReclaim:
    def _leased(self, env, status="active"):
        bid = env.make_binding(status=status)
        env.conn.execute(
            "INSERT INTO inbox(event_id,message_id,chat_id,binding_id,state,ts) "
            "VALUES('ev_1','om_1',?,?,'enqueued',0)", (CHAT, bid))
        env.conn.execute(
            "INSERT INTO deliveries(binding_id,message_id,payload_json,state,lease_token,"
            "lease_epoch,lease_until,attempts) VALUES(?,'om_1','{}','leased','tok',1,?,1)",
            (bid, env.clock.wall_ms() - 1))
        return bid

    def test_active_binding_reclaims_to_enqueued(self, env):
        self._leased(env, "active")
        env.recovery.slow_tick()
        d = env.deliveries()[0]
        assert d["state"] == "enqueued" and d["lease_token"] is None
        assert d["attempts"] == 1  # attempts 保留

    def test_terminated_binding_drops(self, env):
        self._leased(env, "closed")
        env.recovery.slow_tick()
        assert env.deliveries()[0]["state"] == "dropped"


class TestOrphanStarting:
    def test_unconfirmed_starting_without_pending_row_closed(self, env):
        bid = env.make_binding(status="starting", bind_phase="unconfirmed", session_id=None)
        env.recovery.slow_tick()
        b = env.conn.execute("SELECT * FROM bindings WHERE binding_id=?", (bid,)).fetchone()
        assert b["status"] == "closed" and b["close_reason"] == "bind_timeout"


class TestRetention:
    def test_trims_terminal_and_keeps_nonterminal(self, env):
        bid = env.make_binding(status="active")
        old = env.clock.wall_ms() - constants.RETENTION_MS - 1000
        env.conn.execute(
            "INSERT INTO inbox(event_id,message_id,chat_id,binding_id,state,ts,snapshot_json) "
            "VALUES('e1','om_old',?,?,'enqueued',?, '{\"big\":1}')", (CHAT, bid, old))
        env.conn.execute(
            "INSERT INTO inbox(event_id,message_id,chat_id,binding_id,state,ts,snapshot_json) "
            "VALUES('e2','om_live',?,?,'awaiting_approval',?, '{\"big\":1}')", (CHAT, bid, old))
        # media 目录:终态老消息删,非终态禁删
        for m in ("om_old", "om_live"):
            d = env.media_root / bid / m
            d.mkdir(parents=True)
            (d / "f.bin").write_bytes(b"x")
        env.recovery.slow_tick()
        assert env.inbox_row("om_old")["snapshot_json"] is None
        assert env.inbox_row("om_live")["snapshot_json"] is not None
        assert not (env.media_root / bid / "om_old").exists()
        assert (env.media_root / bid / "om_live").exists()


class TestLegacySending:
    def test_stale_sending_to_unknown(self, env):
        bid = env.make_binding(status="active")
        jobs.create_job(env.conn, kind="session_turn", chat_id=CHAT, binding_id=bid,
                        idempotency_key="turn:g:0", turn_group="g", chunk_index=0,
                        body="x", now=env.clock.wall_ms())
        env.conn.execute(
            "UPDATE outbound_jobs SET state='sending', attempt_count=1, sending_at=? "
            "WHERE idempotency_key='turn:g:0'",
            (env.clock.wall_ms() - 3 * constants.SEND_TIMEOUT_S * 1000,))
        env.recovery.slow_tick()
        row = env.conn.execute(
            "SELECT state FROM outbound_jobs WHERE idempotency_key='turn:g:0'").fetchone()
        assert row[0] == "unknown"
