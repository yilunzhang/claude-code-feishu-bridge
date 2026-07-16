"""出站(plan 4.5):per-kind 守卫线性化 / chunk 组内外序 / 契约解析 / unknown 同 key 重试一次。"""
import json

import pytest

from tests.conftest import CHAT
from tests.helpers import FakeRunResult, ok_envelope, err_envelope
from lib import constants, jobs


def mk_job(env, kind="session_turn", key=None, binding_id=None, chat_id=CHAT,
           body="hello", turn_group=None, chunk_index=None, **kw):
    key = key or f"k:{kind}:{env.conn.execute('SELECT COUNT(*) FROM outbound_jobs').fetchone()[0]}"
    jobs.create_job(env.conn, kind=kind, chat_id=chat_id, idempotency_key=key,
                    binding_id=binding_id, body=body, turn_group=turn_group,
                    chunk_index=chunk_index, now=env.clock.wall_ms(), **kw)
    return env.conn.execute("SELECT * FROM outbound_jobs WHERE idempotency_key=?", (key,)).fetchone()


def job_state(env, key):
    return env.conn.execute(
        "SELECT * FROM outbound_jobs WHERE idempotency_key=?", (key,)).fetchone()


def arm_send_ok(env, prefix=("im", "+messages-send")):
    counter = {"n": 0}

    def fn(args, cwd):
        counter["n"] += 1
        return ok_envelope({"message_id": f"om_sent_{counter['n']}"}, notice="rate hint")

    env.runner.on_prefix(list(prefix), fn)
    return counter


class TestSendContract:
    def test_session_turn_sends_text_with_key(self, env):
        bid = env.make_binding(status="active")
        arm_send_ok(env)
        j = mk_job(env, binding_id=bid, key="turn:g1:0", turn_group="g1", chunk_index=0)
        assert env.outbound.tick() == 1
        row = job_state(env, "turn:g1:0")
        assert row["state"] == "sent" and row["sent_message_id"] == "om_sent_1"
        args, _ = env.runner.calls_matching("im", "+messages-send")[0]
        assert args[args.index("--chat-id") + 1] == CHAT
        assert args[args.index("--text") + 1] == "hello"
        assert args[args.index("--idempotency-key") + 1] == "turn:g1:0"
        assert "--markdown" not in args  # F6:出站禁用 markdown 主动面

    def test_missing_message_id_is_unknown_then_retry_same_key(self, env):
        bid = env.make_binding(status="active")
        calls = []

        def fn(args, cwd):
            calls.append(args)
            if len(calls) == 1:
                return ok_envelope({})  # 缺 message_id → UNKNOWN 非成功(F10)
            return ok_envelope({"message_id": "om_ok"})

        env.runner.on_prefix(["im", "+messages-send"], fn)
        mk_job(env, binding_id=bid, key="turn:g:0", turn_group="g", chunk_index=0)
        env.outbound.tick()
        row = job_state(env, "turn:g:0")
        assert row["state"] == "unknown" and row["attempt_count"] == 1
        assert row["next_attempt_at"] is not None
        env.clock.tick(constants.UNKNOWN_RETRY_DELAY_MS + 1)
        env.outbound.tick()
        row = job_state(env, "turn:g:0")
        assert row["state"] == "sent" and row["sent_message_id"] == "om_ok"
        # 同 key 重试(S4 服务端幂等)
        assert calls[0][calls[0].index("--idempotency-key") + 1] == \
               calls[1][calls[1].index("--idempotency-key") + 1]

    def test_second_unknown_is_final_no_third_attempt(self, env):
        bid = env.make_binding(status="active")
        env.runner.on_prefix(["im", "+messages-send"],
                             lambda a, c: FakeRunResult(rc=1, stdout="", timed_out=True))
        mk_job(env, binding_id=bid, key="turn:g:0", turn_group="g", chunk_index=0)
        env.outbound.tick()
        env.clock.tick(constants.UNKNOWN_RETRY_DELAY_MS + 1)
        env.outbound.tick()
        row = job_state(env, "turn:g:0")
        assert row["state"] == "unknown" and row["attempt_count"] == 2
        n_calls = len(env.runner.calls_matching("im", "+messages-send"))
        env.clock.tick(10 * constants.UNKNOWN_RETRY_DELAY_MS)
        env.outbound.tick()
        assert len(env.runner.calls_matching("im", "+messages-send")) == n_calls  # 不再试

    def test_explicit_error_code_is_failed(self, env):
        bid = env.make_binding(status="active")
        env.runner.on_prefix(["im", "+messages-send"], lambda a, c: err_envelope(230002, "perm"))
        mk_job(env, binding_id=bid, key="turn:g:0", turn_group="g", chunk_index=0)
        env.outbound.tick()
        row = job_state(env, "turn:g:0")
        assert row["state"] == "failed" and "230002" in row["error"]

    def test_startup_scan_sending_to_unknown(self, env):
        bid = env.make_binding(status="active")
        mk_job(env, binding_id=bid, key="turn:g:0", turn_group="g", chunk_index=0)
        env.conn.execute("UPDATE outbound_jobs SET state='sending', attempt_count=1 "
                         "WHERE idempotency_key='turn:g:0'")
        env.outbound.startup_scan()
        row = job_state(env, "turn:g:0")
        assert row["state"] == "unknown" and row["next_attempt_at"] is not None


class TestGuards:
    def test_session_turn_binding_not_active_cancelled(self, env):
        bid = env.make_binding(status="closed", close_reason="user_unbind")
        mk_job(env, binding_id=bid, key="turn:g:0", turn_group="g", chunk_index=0)
        env.outbound.tick()
        assert job_state(env, "turn:g:0")["state"] == "cancelled"
        assert env.runner.calls == []

    def test_approval_card_guard_and_backfill(self, env):
        from tests.test_approval import member_pending
        p = member_pending(env)
        env.runner.on_prefix(["im", "+messages-reply"],
                             lambda a, c: ok_envelope({"message_id": "om_card_1"}))
        env.outbound.tick()
        card = job_state(env, f"card:{p['pending_id']}")
        assert card["state"] == "sent"
        args, _ = env.runner.calls_matching("im", "+messages-reply")[0]
        assert args[args.index("--message-id") + 1] == "om_1"
        assert args[args.index("--msg-type") + 1] == "interactive"
        # 发出后回填 card_message_id
        pr = env.conn.execute("SELECT card_message_id FROM pendings WHERE pending_id=?",
                              (p["pending_id"],)).fetchone()
        assert pr[0] == "om_card_1"

    def test_approval_card_cancelled_if_decided(self, env):
        from tests.test_approval import member_pending
        p = member_pending(env)
        env.conn.execute("UPDATE pendings SET state='rejected' WHERE pending_id=?",
                         (p["pending_id"],))
        env.outbound.tick()
        assert job_state(env, f"card:{p['pending_id']}")["state"] == "cancelled"

    def test_approval_card_cancelled_if_already_backfilled(self, env):
        from tests.test_approval import member_pending
        p = member_pending(env)
        env.conn.execute("UPDATE pendings SET card_message_id='om_prev' WHERE pending_id=?",
                         (p["pending_id"],))
        env.outbound.tick()
        assert job_state(env, f"card:{p['pending_id']}")["state"] == "cancelled"

    def test_decision_notice_guard_by_pending_state(self, env):
        bid = env.make_binding(status="active")
        env.conn.execute(
            "INSERT INTO inbox(event_id,message_id,chat_id,binding_id,state,ts) "
            "VALUES('ev_1','om_1',?,?,'rejected',0)", (CHAT, bid))
        env.conn.execute(
            "INSERT INTO pendings(pending_id,message_id,binding_id,nonce,state) "
            "VALUES('p1','om_1',?,'n','rejected')", (bid,))
        arm_send_ok(env)
        mk_job(env, kind="decision_notice", key="dec:p1:rejected", binding_id=bid,
               ref_pending_id="p1", expected_state="rejected")
        mk_job(env, kind="decision_notice", key="dec:p1:approved", binding_id=bid,
               ref_pending_id="p1", expected_state="approved")
        env.outbound.tick()
        assert job_state(env, "dec:p1:rejected")["state"] == "sent"
        assert job_state(env, "dec:p1:approved")["state"] == "cancelled"

    def test_lifecycle_notice_guard(self, env):
        bid = env.make_binding(status="active")
        arm_send_ok(env)
        mk_job(env, kind="lifecycle_notice", key=f"lc:{bid}:bound", binding_id=bid,
               expected_state="active")
        mk_job(env, kind="lifecycle_notice", key=f"lc:{bid}:user_unbind", binding_id=bid,
               expected_state="closed:user_unbind")
        env.outbound.tick()
        assert job_state(env, f"lc:{bid}:bound")["state"] == "sent"
        assert job_state(env, f"lc:{bid}:user_unbind")["state"] == "cancelled"

    def test_inbound_and_unsupported_notice_guards(self, env):
        env.conn.execute(
            "INSERT INTO inbox(event_id,message_id,chat_id,state,ts) "
            "VALUES('ev_1','om_1',?,'unbound',0)", (CHAT,))
        env.conn.execute(
            "INSERT INTO inbox(event_id,message_id,chat_id,state,ts) "
            "VALUES('ev_2','om_2',?,'enqueued',0)", (CHAT,))
        arm_send_ok(env)
        mk_job(env, kind="inbound_notice", key="notice:om_1:unbound",
               ref_message_id="om_1", expected_state="unbound")
        mk_job(env, kind="unsupported_notice", key="un:om_2",
               ref_message_id="om_2", expected_state="unsupported")
        env.outbound.tick()
        assert job_state(env, "notice:om_1:unbound")["state"] == "sent"
        assert job_state(env, "un:om_2")["state"] == "cancelled"  # inbox 状态不符

    def test_receipt_reaction_guard_and_no_retry(self, env):
        bid = env.make_binding(status="active")
        env.conn.execute(
            "INSERT INTO inbox(event_id,message_id,chat_id,binding_id,state,ts) "
            "VALUES('ev_1','om_1',?,?,'enqueued',0)", (CHAT, bid))
        env.conn.execute(
            "INSERT INTO deliveries(binding_id,message_id,payload_json,state) "
            "VALUES(?,'om_1','{}','enqueued')", (bid,))
        seq = env.conn.execute("SELECT delivery_seq FROM deliveries").fetchone()[0]
        env.runner.on_prefix(["im", "reactions", "create"],
                             lambda a, c: FakeRunResult(rc=1, stdout="", timed_out=True))
        mk_job(env, kind="receipt_reaction", key=f"rc:{seq}", binding_id=bid,
               ref_delivery_seq=seq, ref_message_id="om_1", body="GLANCE")
        env.outbound.tick()
        assert job_state(env, f"rc:{seq}")["state"] == "failed"  # 失败即 failed 不重试

    def test_receipt_reaction_cancelled_on_dropped_delivery(self, env):
        bid = env.make_binding(status="active")
        env.conn.execute(
            "INSERT INTO inbox(event_id,message_id,chat_id,binding_id,state,ts) "
            "VALUES('ev_1','om_1',?,?,'enqueued',0)", (CHAT, bid))
        env.conn.execute(
            "INSERT INTO deliveries(binding_id,message_id,payload_json,state) "
            "VALUES(?,'om_1','{}','dropped')", (bid,))
        seq = env.conn.execute("SELECT delivery_seq FROM deliveries").fetchone()[0]
        mk_job(env, kind="receipt_reaction", key=f"rc:{seq}", binding_id=bid,
               ref_delivery_seq=seq, ref_message_id="om_1", body="GLANCE")
        env.outbound.tick()
        assert job_state(env, f"rc:{seq}")["state"] == "cancelled"

    def test_reaction_success_argv_shape(self, env):
        bid = env.make_binding(status="active")
        env.conn.execute(
            "INSERT INTO inbox(event_id,message_id,chat_id,binding_id,state,ts) "
            "VALUES('ev_1','om_1',?,?,'enqueued',0)", (CHAT, bid))
        env.conn.execute(
            "INSERT INTO deliveries(binding_id,message_id,payload_json,state) "
            "VALUES(?,'om_1','{}','enqueued')", (bid,))
        seq = env.conn.execute("SELECT delivery_seq FROM deliveries").fetchone()[0]
        env.runner.on_prefix(["im", "reactions", "create"],
                             lambda a, c: ok_envelope({"reaction_id": "r1"}))
        mk_job(env, kind="receipt_reaction", key=f"rc:{seq}", binding_id=bid,
               ref_delivery_seq=seq, ref_message_id="om_1", body="GLANCE")
        env.outbound.tick()
        assert job_state(env, f"rc:{seq}")["state"] == "sent"
        args, _ = env.runner.calls_matching("im", "reactions", "create")[0]
        params = json.loads(args[args.index("--params") + 1])
        data = json.loads(args[args.index("--data") + 1])
        assert params == {"message_id": "om_1"}
        assert data == {"reaction_type": {"emoji_type": "GLANCE"}}


class TestOrdering:
    def test_chunks_in_group_strict_order(self, env):
        bid = env.make_binding(status="active")
        sent = []

        def fn(args, cwd):
            body = args[args.index("--text") + 1]
            sent.append(body)
            return ok_envelope({"message_id": f"om_{len(sent)}"})

        env.runner.on_prefix(["im", "+messages-send"], fn)
        for i in range(3):
            mk_job(env, binding_id=bid, key=f"turn:g:{i}", body=f"chunk{i}",
                   turn_group="g", chunk_index=i)
        env.outbound.tick()
        assert sent == ["chunk0", "chunk1", "chunk2"]

    def test_chunk_blocked_while_prev_unknown(self, env):
        bid = env.make_binding(status="active")
        calls = {"n": 0}

        def fn(args, cwd):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeRunResult(rc=1, stdout="", timed_out=True)  # chunk0 unknown
            return ok_envelope({"message_id": f"om_{calls['n']}"})

        env.runner.on_prefix(["im", "+messages-send"], fn)
        mk_job(env, binding_id=bid, key="turn:g:0", body="c0", turn_group="g", chunk_index=0)
        mk_job(env, binding_id=bid, key="turn:g:1", body="c1", turn_group="g", chunk_index=1)
        env.outbound.tick()
        assert job_state(env, "turn:g:0")["state"] == "unknown"
        assert job_state(env, "turn:g:1")["state"] == "pending"  # 前块非 sent 后块不发
        env.clock.tick(constants.UNKNOWN_RETRY_DELAY_MS + 1)
        env.outbound.tick()
        assert job_state(env, "turn:g:0")["state"] == "sent"
        assert job_state(env, "turn:g:1")["state"] == "sent"

    def test_chunk_after_failed_prev_cancelled(self, env):
        bid = env.make_binding(status="active")
        env.runner.on_prefix(["im", "+messages-send"], lambda a, c: err_envelope(230002))
        mk_job(env, binding_id=bid, key="turn:g:0", turn_group="g", chunk_index=0)
        mk_job(env, binding_id=bid, key="turn:g:1", turn_group="g", chunk_index=1)
        env.outbound.tick()
        env.outbound.tick()
        assert job_state(env, "turn:g:0")["state"] == "failed"
        assert job_state(env, "turn:g:1")["state"] == "cancelled"

    def test_turn_groups_ordered_unknown_blocks_next_group(self, env):
        bid = env.make_binding(status="active")
        env.runner.on_prefix(["im", "+messages-send"],
                             lambda a, c: FakeRunResult(rc=1, stdout="", timed_out=True))
        mk_job(env, binding_id=bid, key="turn:gA:0", turn_group="gA", chunk_index=0)
        mk_job(env, binding_id=bid, key="turn:gB:0", turn_group="gB", chunk_index=0)
        env.outbound.tick()
        env.clock.tick(constants.UNKNOWN_RETRY_DELAY_MS + 1)
        env.outbound.tick()  # gA 第二次 unknown → 终局 unknown
        env.clock.tick(constants.UNKNOWN_RETRY_DELAY_MS + 1)
        env.outbound.tick()
        assert job_state(env, "turn:gA:0")["state"] == "unknown"
        assert job_state(env, "turn:gB:0")["state"] == "pending"  # 前组 unknown 后组不发

    def test_other_binding_not_blocked(self, env):
        bid_a = env.make_binding(status="active", chat_id="oc_A", session_id="sA",
                                 cc_pid=1111, cc_start="t1")
        bid_b = env.make_binding(status="active", chat_id="oc_B", session_id="sB",
                                 cc_pid=2222, cc_start="t2")
        env.conn.execute("UPDATE outbound_jobs SET state='cancelled'")  # 清 lifecycle 噪声
        calls = {"n": 0}

        def fn(args, cwd):
            calls["n"] += 1
            chat = args[args.index("--chat-id") + 1]
            if chat == "oc_A":
                return FakeRunResult(rc=1, stdout="", timed_out=True)
            return ok_envelope({"message_id": "om_b"})

        env.runner.on_prefix(["im", "+messages-send"], fn)
        mk_job(env, binding_id=bid_a, chat_id="oc_A", key="turn:ga:0", turn_group="ga", chunk_index=0)
        mk_job(env, binding_id=bid_b, chat_id="oc_B", key="turn:gb:0", turn_group="gb", chunk_index=0)
        env.outbound.tick()
        assert job_state(env, "turn:ga:0")["state"] == "unknown"
        assert job_state(env, "turn:gb:0")["state"] == "sent"

    def test_notices_same_chat_ordered_by_job_seq(self, env):
        env.conn.execute(
            "INSERT INTO inbox(event_id,message_id,chat_id,state,ts) VALUES('e1','om_1',?, 'unbound',0)",
            (CHAT,))
        env.conn.execute(
            "INSERT INTO inbox(event_id,message_id,chat_id,state,ts) VALUES('e2','om_2',?, 'unbound',0)",
            (CHAT,))
        calls = {"n": 0}

        def fn(args, cwd):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeRunResult(rc=1, stdout="", timed_out=True)
            return ok_envelope({"message_id": "om_x"})

        env.runner.on_prefix(["im", "+messages-send"], fn)
        mk_job(env, kind="inbound_notice", key="notice:om_1:unbound",
               ref_message_id="om_1", expected_state="unbound")
        mk_job(env, kind="inbound_notice", key="notice:om_2:unbound",
               ref_message_id="om_2", expected_state="unbound")
        env.outbound.tick()
        assert job_state(env, "notice:om_1:unbound")["state"] == "unknown"
        assert job_state(env, "notice:om_2:unbound")["state"] == "pending"  # 同 chat 按序
