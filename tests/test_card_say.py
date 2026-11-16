"""card say:owner 点自定义卡片按钮 = owner 在绑定群说了按钮上的那句话。
纯机械门;无效/重复零投递;审批卡路径不受影响(由 test_approval.py 全套覆盖)。"""
import json

import pytest

from tests.conftest import CHAT, MEMBER, OWNER
from tests.helpers import ok_envelope
from lib import constants


def say(text="选 1", operator=OWNER, event_id="cb_say_1", chat_id=CHAT,
        message_id="om_card_1", as_string=True, extra=None, **over):
    av = {"act": "say", "text": text}
    av.update(extra or {})
    ev = {"type": "card.action.trigger", "event_id": event_id, "operator_id": operator,
          "action_value": json.dumps(av, ensure_ascii=False) if as_string else av,
          "message_id": message_id, "chat_id": chat_id, "host": "im_message"}
    ev.update(over)
    return ev


def seen(env, event_id):
    return env.conn.execute(
        "SELECT COUNT(*) FROM callback_events WHERE event_id=?", (event_id,)).fetchone()[0]


class TestSayDelivered:
    def test_owner_click_enqueues_owner_message(self, env):
        bid = env.make_binding(status="active")
        r = env.approval.process_event(say(text="  选 2：发给逸伦  ", extra={"id": "d-1", "choice": 2}))
        assert r == "said"
        d = env.deliveries(bid)
        assert len(d) == 1 and d[0]["state"] == "enqueued"
        p = json.loads(d[0]["payload_json"])
        assert p["message_id"] == "card:cb_say_1"
        assert p["chat_id"] == CHAT
        assert p["sender_open_id"] == OWNER and p["sender_is_owner"] is True
        assert p["approved_by"] is None
        assert p["message_type"] == "card_action"
        assert p["text"] == "选 2：发给逸伦"          # strip 后原样
        assert p["card_message_id"] == "om_card_1"
        assert p["action_value"] == {"act": "say", "text": "  选 2：发给逸伦  ", "id": "d-1", "choice": 2}
        assert p["media_paths"] == []
        row = env.inbox_row("card:cb_say_1")
        assert row["state"] == "enqueued" and row["message_type"] == "card_action"
        assert row["binding_id"] == bid and row["sender_open_id"] == OWNER
        assert json.loads(row["snapshot_json"])["card_message_id"] == "om_card_1"
        assert seen(env, "cb_say_1") == 1
        assert env.pendings() == []                    # 不产生审批 pending
        assert env.jobs("decision_notice") == []

    def test_receipt_reaction_targets_the_card(self, env):
        bid = env.make_binding(status="active")
        env.approval.process_event(say())
        rc = env.jobs("receipt_reaction")
        assert len(rc) == 1
        seq = env.deliveries(bid)[0]["delivery_seq"]
        assert rc[0]["ref_message_id"] == "om_card_1" and rc[0]["ref_delivery_seq"] == seq
        assert rc[0]["idempotency_key"] == f"rc:{seq}" and rc[0]["body"] == "GLANCE"
        env.runner.on_prefix(["im", "reactions", "create"],
                             lambda a, c: ok_envelope({"reaction_id": "r1"}))
        env.outbound.tick()
        calls = env.runner.calls_matching("im", "reactions", "create")
        assert len(calls) == 1
        argv = calls[0][0]
        assert json.loads(argv[argv.index("--params") + 1]) == {"message_id": "om_card_1"}
        assert env.conn.execute(
            "SELECT state FROM outbound_jobs WHERE idempotency_key=?", (f"rc:{seq}",)).fetchone()[0] == "sent"

    def test_action_value_as_dict(self, env):
        env.make_binding(status="active")
        assert env.approval.process_event(say(as_string=False)) == "said"
        assert len(env.deliveries()) == 1

    def test_duplicate_event_is_dup_single_delivery(self, env):
        env.make_binding(status="active")
        assert env.approval.process_event(say()) == "said"
        assert env.approval.process_event(say()) == "dup"
        assert len(env.deliveries()) == 1 and len(env.jobs("receipt_reaction")) == 1

    def test_two_clicks_on_same_card_two_deliveries(self, env):
        env.make_binding(status="active")
        assert env.approval.process_event(say(event_id="cb_a", text="发 663")) == "said"
        assert env.approval.process_event(say(event_id="cb_b", text="撤 553")) == "said"
        texts = [json.loads(d["payload_json"])["text"] for d in env.deliveries()]
        assert texts == ["发 663", "撤 553"]

    def test_listener_emits_the_click_as_a_message_line(self, env):
        """真 listener 领取:行形状与普通群消息一致(type=feishu_message),多出 card 字段。"""
        from lib.listener_core import ListenerCore
        bid = env.make_binding(status="active", listener_pid=None, listener_start=None,
                               listener_epoch=0, listener_beat_at=None)
        assert env.approval.process_event(say(text="选 3")) == "said"
        lines = []
        core = ListenerCore(env.conn, bid, env.clock, env.prober,
                            me_pid=7001, me_start="Tue Jul 15 11:00:00 2026",
                            printer=lambda x: lines.append(x),
                            daemon_alive_probe=lambda: True, ensure_daemon=lambda: None)
        core.step()
        assert len(lines) == 1
        o = json.loads(lines[0])
        assert o["type"] == "feishu_message" and "delivery_seq" in o
        assert o["message_type"] == "card_action" and o["text"] == "选 3"
        assert o["sender_is_owner"] is True and o["card_message_id"] == "om_card_1"
        assert env.deliveries(bid)[0]["state"] == "emitted"


class TestSayRejected:
    @pytest.mark.parametrize("kw,desc", [
        (dict(operator=MEMBER), "成员点击"),
        (dict(operator=None), "无 operator"),
        (dict(chat_id="oc_other"), "该群没有绑定"),
        (dict(chat_id=None), "缺 chat_id"),
        (dict(message_id=None), "缺卡片 message_id"),
        (dict(text=""), "空文本"),
        (dict(text="   "), "纯空白"),
        (dict(text="长" * (constants.SAY_TEXT_MAX + 1)), "超长"),
    ])
    def test_invalid_no_delivery(self, env, kw, desc):
        env.make_binding(status="active")
        r = env.approval.process_event(say(event_id="cb_bad", **kw))
        assert r == "invalid", desc
        assert env.deliveries() == [] and env.jobs() == []
        assert env.inbox_row("card:cb_bad") is None
        assert seen(env, "cb_bad") == 1                # 无效回调裸去重

    def test_non_string_text_invalid(self, env):
        env.make_binding(status="active")
        ev = say(event_id="cb_n", as_string=False)
        ev["action_value"]["text"] = {"x": 1}
        assert env.approval.process_event(ev) == "invalid"
        assert env.deliveries() == []

    @pytest.mark.parametrize("status,phase", [("starting", "confirmed"), ("starting", "unconfirmed"),
                                              ("closed", "confirmed"), ("dead", "confirmed")])
    def test_binding_not_active_invalid(self, env, status, phase):
        env.make_binding(status=status, bind_phase=phase)
        assert env.approval.process_event(say(event_id="cb_s")) == "invalid"
        assert env.deliveries() == []

    def test_no_binding_at_all_invalid(self, env):
        assert env.approval.process_event(say(event_id="cb_none")) == "invalid"
        assert env.deliveries() == []

    def test_chat_allowlist_excludes(self, env):
        env.make_binding(status="active")
        env.cfg["chat_allowlist"] = ["oc_someone_else"]
        assert env.approval.process_event(say(event_id="cb_al")) == "invalid"
        assert env.deliveries() == []

    def test_invalid_then_replay_is_dup(self, env):
        env.make_binding(status="active")
        assert env.approval.process_event(say(event_id="cb_r", operator=MEMBER)) == "invalid"
        assert env.approval.process_event(say(event_id="cb_r")) == "dup"   # 同 event 不翻案
        assert env.deliveries() == []


class TestApprovalUntouched:
    def test_approve_reject_values_do_not_enter_say_branch(self, env):
        env.make_binding(status="active")
        ev = say(event_id="cb_x", as_string=False)
        ev["action_value"] = {"act": "approve", "pending_id": "nope", "nonce": "n", "text": "选 1"}
        assert env.approval.process_event(ev) == "invalid"    # 走审批校验:未知 pending
        assert env.deliveries() == []
