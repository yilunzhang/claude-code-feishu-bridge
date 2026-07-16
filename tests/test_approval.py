"""审批回调(plan 4.3):单事务(去重+机械校验+CAS+入队/物化+通知);崩溃缝注入重放。"""
import json

import pytest

from tests.conftest import APP_ID, CHAT, MEMBER, OWNER
from tests.helpers import bot_mention, mget_snapshot
from lib import lifecycle


class Crash(Exception):
    pass


def member_pending(env, mid="om_1", msg_type="text"):
    env.make_binding(status="active")
    env.arm_mget([mget_snapshot(mid, CHAT, MEMBER, msg_type=msg_type, text="run task",
                                mentions=[bot_mention(APP_ID)])])
    env.recv_event(message_id=mid, sender_id=MEMBER, message_type=msg_type)
    p = env.pendings()[0]
    assert p["state"] == "pending"
    return p


def cb(p, act="approve", operator=OWNER, event_id="cb_1", nonce=None,
       message_id="om_card_x", chat_id=CHAT):
    return {"type": "card.action.trigger", "event_id": event_id,
            "operator_id": operator,
            "action_value": json.dumps({"pending_id": p["pending_id"],
                                        "nonce": nonce or p["nonce"], "act": act}),
            "message_id": message_id, "chat_id": chat_id, "host": "im_message"}


def pending_row(env, pid):
    return env.conn.execute("SELECT * FROM pendings WHERE pending_id=?", (pid,)).fetchone()


class TestApproveReject:
    def test_approve_text_single_tx_effects(self, env):
        p = member_pending(env)
        r = env.approval.process_event(cb(p))
        assert r == "applied"
        pr = pending_row(env, p["pending_id"])
        assert pr["state"] == "approved" and pr["decided_by"] == OWNER
        assert pr["decided_event_id"] == "cb_1"
        assert env.inbox_row("om_1")["state"] == "enqueued"
        d = env.deliveries()
        assert len(d) == 1
        payload = json.loads(d[0]["payload_json"])
        assert payload["sender_is_owner"] is False and payload["approved_by"] == OWNER
        dec = env.jobs("decision_notice")
        assert len(dec) == 1
        assert dec[0]["idempotency_key"] == f"dec:{p['pending_id']}:approved"
        assert env.jobs("receipt_reaction") == []  # 审批路径无 👀
        assert env.conn.execute(
            "SELECT COUNT(*) FROM callback_events WHERE event_id='cb_1'").fetchone()[0] == 1

    def test_reject(self, env):
        p = member_pending(env)
        r = env.approval.process_event(cb(p, act="reject"))
        assert r == "applied"
        assert pending_row(env, p["pending_id"])["state"] == "rejected"
        assert env.inbox_row("om_1")["state"] == "rejected"
        assert env.deliveries() == []
        dec = env.jobs("decision_notice")
        assert dec[0]["idempotency_key"] == f"dec:{p['pending_id']}:rejected"


class TestMechanicalValidation:
    @pytest.mark.parametrize("mutate,desc", [
        (dict(operator=MEMBER), "member 自批"),
        (dict(nonce="deadbeef" * 4), "nonce 错"),
        (dict(act="detonate"), "act 非枚举"),
        (dict(chat_id="oc_other"), "chat 不符"),
    ])
    def test_invalid_no_state_change(self, env, mutate, desc):
        p = member_pending(env)
        r = env.approval.process_event(cb(p, event_id="cb_bad", **mutate))
        assert r == "invalid", desc
        assert pending_row(env, p["pending_id"])["state"] == "pending"
        assert env.deliveries() == [] and env.jobs("decision_notice") == []
        # 无效回调裸去重记录
        assert env.conn.execute(
            "SELECT COUNT(*) FROM callback_events WHERE event_id='cb_bad'").fetchone()[0] == 1

    def test_non_ascii_nonce_invalid_not_crash(self, env):
        p = member_pending(env)
        r = env.approval.process_event(cb(p, event_id="cb_u", nonce="坏心思 nonce"))
        assert r == "invalid"
        assert pending_row(env, p["pending_id"])["state"] == "pending"

    def test_unknown_pending_invalid(self, env):
        p = member_pending(env)
        fake = dict(p)
        fake["pending_id"] = "no-such"
        assert env.approval.process_event(cb(fake, event_id="cb_z")) == "invalid"

    def test_card_message_id_checked_only_when_backfilled(self, env):
        p = member_pending(env)
        env.conn.execute("UPDATE pendings SET card_message_id='om_card_real' WHERE pending_id=?",
                         (p["pending_id"],))
        assert env.approval.process_event(cb(p, message_id="om_forged")) == "invalid"
        assert env.approval.process_event(
            cb(p, event_id="cb_2", message_id="om_card_real")) == "applied"

    def test_duplicate_event_noop(self, env):
        p = member_pending(env)
        assert env.approval.process_event(cb(p)) == "applied"
        assert env.approval.process_event(cb(p)) == "dup"
        assert len(env.deliveries()) == 1
        assert len(env.jobs("decision_notice")) == 1

    def test_late_click_on_decided_pending(self, env):
        p = member_pending(env)
        assert env.approval.process_event(cb(p, act="reject", event_id="cb_1")) == "applied"
        r = env.approval.process_event(cb(p, act="approve", event_id="cb_2"))
        assert r == "late"
        assert pending_row(env, p["pending_id"])["state"] == "rejected"
        assert env.deliveries() == []

    def test_binding_terminated_then_click_is_late(self, env):
        p = member_pending(env)
        bid = env.conn.execute("SELECT binding_id FROM pendings WHERE pending_id=?",
                               (p["pending_id"],)).fetchone()[0]
        lifecycle.terminate_binding(env.conn, bid, "user_unbind", env.clock)
        r = env.approval.process_event(cb(p))
        assert r == "late"  # 终止级联已 expire pending → CAS 拒绝
        assert env.deliveries() == []


class TestCrashSeams:
    def test_crash_after_cas_rolls_back_then_replay_succeeds(self, env):
        p = member_pending(env)

        def seam(name):
            if name == "after_cas":
                raise Crash()

        with pytest.raises(Crash):
            env.approval.process_event(cb(p), seam=seam)
        # 整体回滚:pending 仍 pending,callback 未记,零副作用
        assert pending_row(env, p["pending_id"])["state"] == "pending"
        assert env.conn.execute("SELECT COUNT(*) FROM callback_events").fetchone()[0] == 0
        assert env.deliveries() == []
        # 重放同一 event → 完整生效一次
        assert env.approval.process_event(cb(p)) == "applied"
        assert len(env.deliveries()) == 1

    def test_crash_before_commit_rolls_back(self, env):
        p = member_pending(env)

        def seam(name):
            if name == "before_commit":
                raise Crash()

        with pytest.raises(Crash):
            env.approval.process_event(cb(p), seam=seam)
        assert pending_row(env, p["pending_id"])["state"] == "pending"
        assert env.approval.process_event(cb(p)) == "applied"

    def test_replay_after_commit_is_dup(self, env):
        p = member_pending(env)
        assert env.approval.process_event(cb(p)) == "applied"
        # "崩溃在 commit 后、下游动作前" 的重放等价于 dup
        assert env.approval.process_event(cb(p)) == "dup"
        assert len(env.deliveries()) == 1


class TestMediaApprove:
    def test_media_approve_materializes_then_delivers(self, env):
        p = member_pending(env, mid="om_img", msg_type="image")
        snap = mget_snapshot("om_img", CHAT, MEMBER, msg_type="image",
                             mentions=[bot_mention(APP_ID)])

        def dl(args, cwd):
            import pathlib
            d = pathlib.Path(cwd) / "lark-im-resources"
            d.mkdir(parents=True, exist_ok=True)
            (d / "img.png").write_bytes(b"IMG")
            from tests.helpers import ok_envelope
            return ok_envelope({"messages": [snap]})

        env.runner.on(lambda a: "--download-resources" in a, dl)
        r = env.approval.process_event(cb(p))
        assert r == "applied"
        row = env.inbox_row("om_img")
        assert row["state"] == "enqueued"
        d = env.deliveries()
        assert len(d) == 1
        payload = json.loads(d[0]["payload_json"])
        assert payload["media_paths"] and payload["approved_by"] == OWNER
        dec = env.jobs("decision_notice")
        assert [j["idempotency_key"] for j in dec] == [f"dec:{p['pending_id']}:approved"]

    def test_media_approve_download_definitive_failure(self, env):
        p = member_pending(env, mid="om_img", msg_type="image")
        snap = mget_snapshot("om_img", CHAT, MEMBER, msg_type="image",
                             mentions=[bot_mention(APP_ID)])

        def dl(args, cwd):
            import pathlib
            d = pathlib.Path(cwd) / "lark-im-resources"
            d.mkdir(parents=True, exist_ok=True)
            import os
            os.symlink("/etc/hosts", d / "evil")  # 确定性失败
            from tests.helpers import ok_envelope
            return ok_envelope({"messages": [snap]})

        env.runner.on(lambda a: "--download-resources" in a, dl)
        assert env.approval.process_event(cb(p)) == "applied"
        assert env.inbox_row("om_img")["state"] == "failed"
        dec = env.jobs("decision_notice")
        assert [j["idempotency_key"] for j in dec] == [f"dec:{p['pending_id']}:failed"]
        assert env.deliveries() == []

    def test_media_approve_transient_failure_stays_materializing(self, env):
        p = member_pending(env, mid="om_img", msg_type="image")
        from tests.helpers import err_envelope
        env.runner.on(lambda a: "--download-resources" in a, lambda a, c: err_envelope(500))
        assert env.approval.process_event(cb(p)) == "applied"
        assert env.inbox_row("om_img")["state"] == "approved_materializing"
        assert env.deliveries() == []
