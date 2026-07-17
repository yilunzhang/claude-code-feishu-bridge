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
        # session_turn 走 --markdown(可信群前提,2026-07-17),不走 --text
        assert args[args.index("--markdown") + 1] == "hello"
        assert "--text" not in args
        from lib import util
        wire_key = args[args.index("--idempotency-key") + 1]
        assert wire_key == util.short_key("turn:g1:0") and len(wire_key) <= 40  # E4b

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
            body = args[args.index("--markdown") + 1]  # session_turn 走 --markdown
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


class TestErrorClassification:
    """修复项4:表驱动错误分类:永久集→failed;瞬态集→unknown(仍≤1 次同 key 重试);未知→unknown。"""

    def _job(self, env):
        bid = env.make_binding(status="active")
        return mk_job(env, binding_id=bid, key="turn:g:0", turn_group="g", chunk_index=0)

    def test_permanent_code_failed(self, env):
        self._job(env)
        env.runner.on_prefix(["im", "+messages-send"], lambda a, c: err_envelope(230002))
        env.outbound.tick()
        assert job_state(env, "turn:g:0")["state"] == "failed"

    def test_transient_code_unknown_with_single_retry(self, env):
        self._job(env)
        calls = {"n": 0}

        def fn(args, cwd):
            calls["n"] += 1
            return err_envelope(230020, "req too frequent")  # 频控=瞬态

        env.runner.on_prefix(["im", "+messages-send"], fn)
        env.outbound.tick()
        row = job_state(env, "turn:g:0")
        assert row["state"] == "unknown" and row["next_attempt_at"] is not None
        env.clock.tick(constants.UNKNOWN_RETRY_DELAY_MS + 1)
        env.outbound.tick()
        env.clock.tick(constants.UNKNOWN_RETRY_DELAY_MS + 1)
        env.outbound.tick()
        assert calls["n"] == 2  # 同 key 自动重试仍 ≤1 次
        assert job_state(env, "turn:g:0")["state"] == "unknown"

    def test_unknown_code_stays_unknown_for_manual(self, env):
        self._job(env)
        env.runner.on_prefix(["im", "+messages-send"], lambda a, c: err_envelope(123456789))
        env.outbound.tick()
        assert job_state(env, "turn:g:0")["state"] == "unknown"


class TestPendingBackoffGate:
    def test_pending_with_future_next_attempt_not_sent(self, env):
        """修复项3配套:重臂后的 pending 带 next_attempt_at,未到期不发。"""
        bid = env.make_binding(status="active")
        mk_job(env, binding_id=bid, key="turn:g:0", turn_group="g", chunk_index=0)
        env.conn.execute(
            "UPDATE outbound_jobs SET next_attempt_at=? WHERE idempotency_key='turn:g:0'",
            (env.clock.wall_ms() + 60_000,))
        arm_send_ok(env)
        assert env.outbound.tick() == 0
        env.clock.tick(60_001)
        assert env.outbound.tick() == 1


class TestReactionContract:
    def test_reaction_ok_without_reaction_id_is_failed(self, env):
        """修复项9:reaction 成功必须解析出 .data.reaction_id,缺=failed(仍不重试)。"""
        bid = env.make_binding(status="active")
        env.conn.execute(
            "INSERT INTO inbox(event_id,message_id,chat_id,binding_id,state,ts) "
            "VALUES('ev_1','om_1',?,?,'enqueued',0)", (CHAT, bid))
        env.conn.execute(
            "INSERT INTO deliveries(binding_id,message_id,payload_json,state) "
            "VALUES(?,'om_1','{}','enqueued')", (bid,))
        seq = env.conn.execute("SELECT delivery_seq FROM deliveries").fetchone()[0]
        env.runner.on_prefix(["im", "reactions", "create"], lambda a, c: ok_envelope({}))
        mk_job(env, kind="receipt_reaction", key=f"rc:{seq}", binding_id=bid,
               ref_delivery_seq=seq, ref_message_id="om_1", body="GLANCE")
        env.outbound.tick()
        assert job_state(env, f"rc:{seq}")["state"] == "failed"


class TestE4StderrEnvelope:
    """E4a:错误信封在 stderr(stdout 空),code 嵌套 .error.code;分类照走永久/瞬态表。"""

    def _job(self, env, key="turn:g:0"):
        bid = env.make_binding(status="active")
        return mk_job(env, binding_id=bid, key=key, turn_group="g", chunk_index=0)

    def test_stderr_permanent_code_failed(self, env):
        from tests.helpers import stderr_err_envelope
        self._job(env)
        env.runner.on_prefix(["im", "+messages-send"],
                             lambda a, c: stderr_err_envelope(99992402))
        env.outbound.tick()
        row = job_state(env, "turn:g:0")
        assert row["state"] == "failed" and "99992402" in row["error"]

    def test_stderr_transient_code_unknown_with_retry(self, env):
        from tests.helpers import stderr_err_envelope
        self._job(env)
        env.runner.on_prefix(["im", "+messages-send"],
                             lambda a, c: stderr_err_envelope(230020, subtype="rate_limited",
                                                              msg="req too frequent"))
        env.outbound.tick()
        row = job_state(env, "turn:g:0")
        assert row["state"] == "unknown" and row["next_attempt_at"] is not None

    def test_failure_logs_raw_streams(self, env):
        """E4a 可观测性:unknown/failed 把 rc/stdout/stderr 截断记入 daemon.log。"""
        from tests.helpers import stderr_err_envelope
        logs = []
        env.outbound.log = logs.append
        self._job(env)
        env.runner.on_prefix(["im", "+messages-send"],
                             lambda a, c: stderr_err_envelope(99992402))
        env.outbound.tick()
        joined = "\n".join(logs)
        assert "rc=" in joined and "99992402" in joined and "turn:g:0" in joined


class TestE4ShortKey:
    """E4b:飞书 uuid 参数上限 ~50 字符 → wire 短键 ≤40;DB 逻辑键与 UNIQUE 语义不变。"""

    def test_short_key_properties(self):
        from lib import util
        k1 = util.short_key("notice:om_" + "a" * 40 + ":session_closed")
        k2 = util.short_key("notice:om_" + "a" * 40 + ":session_closed")
        k3 = util.short_key("notice:om_" + "b" * 40 + ":session_closed")
        assert k1 == k2 and k1 != k3
        assert len(k1) <= 40 and k1.startswith("fb:")

    def test_long_logical_key_transmitted_as_short_key(self, env):
        """真形状:fake 对 >50 字符 key 返回 99992402 stderr 信封,逼实现走短键。"""
        from tests.helpers import stderr_err_envelope
        from lib import util
        long_mid = "om_" + "a" * 40
        env.conn.execute(
            "INSERT INTO inbox(event_id,message_id,chat_id,state,ts) VALUES('e1',?,?,?,0)",
            (long_mid, CHAT, "session_closed"))
        logical = f"notice:{long_mid}:session_closed"
        assert len(logical) > 50  # 逻辑键必超限
        mk_job(env, kind="inbound_notice", key=logical,
               ref_message_id=long_mid, expected_state="session_closed", body="提示")
        wire_keys = []

        def fn(args, cwd):
            key = args[args.index("--idempotency-key") + 1]
            wire_keys.append(key)
            if len(key) > 50:
                return stderr_err_envelope(99992402)  # 真机行为:超长键被拒
            return ok_envelope({"message_id": "om_ok"})

        env.runner.on_prefix(["im", "+messages-send"], fn)
        env.outbound.tick()
        row = job_state(env, logical)
        assert row["state"] == "sent"  # 走短键才可能成功
        assert wire_keys == [util.short_key(logical)]
        assert len(wire_keys[0]) <= 40
        # DB 存逻辑键,UNIQUE 语义不变
        assert row["idempotency_key"] == logical

    def test_retry_uses_same_short_key(self, env):
        from lib import util
        bid = env.make_binding(status="active")
        mk_job(env, binding_id=bid, key="turn:g:0", turn_group="g", chunk_index=0)
        seen = []

        def fn(args, cwd):
            seen.append(args[args.index("--idempotency-key") + 1])
            if len(seen) == 1:
                return FakeRunResult(rc=1, stdout="", timed_out=True)
            return ok_envelope({"message_id": "om_ok"})

        env.runner.on_prefix(["im", "+messages-send"], fn)
        env.outbound.tick()
        env.clock.tick(constants.UNKNOWN_RETRY_DELAY_MS + 1)
        env.outbound.tick()
        assert seen[0] == seen[1] == util.short_key("turn:g:0")  # S4 同键重试语义保持


class TestAllowlistOutboundGate:
    def test_out_of_list_job_cancelled(self, env):
        """r3-1③:allowlist 生效且 job.chat_id 不在列 → cancelled,零外发。"""
        bid = env.make_binding(status="active")
        mk_job(env, binding_id=bid, key="turn:g:0", turn_group="g", chunk_index=0)
        env.cfg["chat_allowlist"] = ["oc_other"]
        env.outbound.tick()
        assert job_state(env, "turn:g:0")["state"] == "cancelled"
        assert env.runner.calls == []

    def test_in_list_job_sends(self, env):
        bid = env.make_binding(status="active")
        mk_job(env, binding_id=bid, key="turn:g:0", turn_group="g", chunk_index=0)
        env.cfg["chat_allowlist"] = [CHAT]
        arm_send_ok(env)
        assert env.outbound.tick() == 1


class TestAllowlistBeforeGate:
    def test_out_of_list_job_cancelled_even_when_gate_degraded(self, env):
        """r4-3:allowlist 列外 job 无论 fingerprint 门状态都确定性 cancelled。"""
        from lib import db as dbmod
        bid = env.make_binding(status="active")
        mk_job(env, binding_id=bid, key="turn:g:0", turn_group="g", chunk_index=0)
        env.cfg["chat_allowlist"] = ["oc_other"]
        dbmod.set_state(env.conn, "outbound_gate", "degraded:identity_unverified")
        env.outbound.tick()
        assert job_state(env, "turn:g:0")["state"] == "cancelled"
        assert env.runner.calls == []

    def test_in_list_job_still_blocked_by_degraded_gate(self, env):
        """对照:列内 job 在 degraded 门下仍停摆(不 cancel,保持 pending)。"""
        from lib import db as dbmod
        bid = env.make_binding(status="active")
        mk_job(env, binding_id=bid, key="turn:g:0", turn_group="g", chunk_index=0)
        env.cfg["chat_allowlist"] = [CHAT]
        dbmod.set_state(env.conn, "outbound_gate", "degraded:version_mismatch")
        assert env.outbound.tick() == 0
        assert job_state(env, "turn:g:0")["state"] == "pending"


class TestWireFlagsByKind:
    """出站 wire flag 按 kind(2026-07-17):session_turn=--markdown(可信群前提),
    通知=--text,审批卡=转义 interactive。"""

    def test_session_turn_uses_markdown_not_text(self, env):
        bid = env.make_binding(status="active")
        arm_send_ok(env)
        mk_job(env, kind="session_turn", key="turn:g:0", binding_id=bid,
               turn_group="g", chunk_index=0, body="# 标题\n- a\n```py\nx=1\n```")
        env.outbound.tick()
        args, _ = env.runner.calls_matching("im", "+messages-send")[0]
        assert "--markdown" in args and "--text" not in args
        assert args[args.index("--markdown") + 1].startswith("# 标题")

    def test_lifecycle_notice_uses_text_not_markdown(self, env):
        bid = env.make_binding(status="active")
        arm_send_ok(env)
        mk_job(env, kind="lifecycle_notice", key=f"lc:{bid}:bound", binding_id=bid,
               expected_state="active", body="✅ 已绑定")
        env.outbound.tick()
        args, _ = env.runner.calls_matching("im", "+messages-send")[0]
        assert "--text" in args and "--markdown" not in args

    def test_inbound_notice_uses_text_not_markdown(self, env):
        env.conn.execute(
            "INSERT INTO inbox(event_id,message_id,chat_id,state,ts) "
            "VALUES('e1','om_1',?,'unbound',0)", (CHAT,))
        arm_send_ok(env)
        mk_job(env, kind="inbound_notice", key="notice:om_1:unbound",
               ref_message_id="om_1", expected_state="unbound", body="⚠️ 未绑定")
        env.outbound.tick()
        args, _ = env.runner.calls_matching("im", "+messages-send")[0]
        assert "--text" in args and "--markdown" not in args

    def test_approval_card_stays_escaped_interactive(self, env):
        from tests.test_approval import member_pending
        p = member_pending(env)
        env.runner.on_prefix(["im", "+messages-reply"],
                             lambda a, c: ok_envelope({"message_id": "om_card"}))
        env.outbound.tick()
        args, _ = env.runner.calls_matching("im", "+messages-reply")[0]
        assert args[args.index("--msg-type") + 1] == "interactive"
        assert "--markdown" not in args and "--text" not in args  # 成员预览绝不 markdown 渲染
