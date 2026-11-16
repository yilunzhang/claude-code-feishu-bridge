"""回复语言提醒:群里最近一条明确的消息不是英文、而本轮回复几乎全是英文 → Stop hook 拦一次提醒 AI,
本轮不转发;续写那次(stop_hook_active)照常转发。检查本身出错 → 照常转发(fail-open)。"""
import json

import pytest

from tests.test_stop_hook import hook_env, stop, turn_jobs  # noqa: F401
from lib import hooklib, langcheck

EN_REPLY = ("I've pushed the fix to both remotes and restarted the daemon. All fifteen "
            "bindings are healthy and the selfcheck passed on the new CLI version.")
ZH_REPLY = "已推到 GitHub 和 GitLab,commit a1e5333;daemon 切到 1.6.4,15 个 binding 都正常,selfcheck 通过。"
LOG = "\n".join(f"2026-11-16 10:0{i} ERROR worker failed: connection reset by peer" for i in range(8))


class TestDetect:
    @pytest.mark.parametrize("text,lang", [
        ("你检查一下这个 log\n" + LOG, "non_en"),          # 中文指令 + 大段英文日志 → 仍是中文
        ("Проверь, пожалуйста, этот лог", "non_en"),
        ("このログを確認してください", "non_en"),
        ("看下 /Users/me/csr/feishu-bridge/lib/hooklib.py 这个文件", "non_en"),   # 路径不计
        ("把 `reconcile_daemon_code_identity` 跑一下", "non_en"),                   # 行内代码不计
        ("帮我检查/Users/me/csr/feishu-bridge/lib/hooklib.py这个文件", "non_en"),   # 路径不吞相邻中文
        ("看下 log\nERROR worker failed: connection reset by peer", "non_en"),      # 首行短指令
        ("看下这个 log: ERROR worker failed: connection reset by peer", "non_en"),   # 冒号前的指令
        ("Please check the deploy log for errors", "en"),
        ("ask 张林 about the deploy window", "en"),          # 英文里夹人名 ≠ 中文提问
        ("A", None),
        ("可以", "non_en"),
    ])
    def test_question_language(self, text, lang):
        assert langcheck.question_language(text) == lang

    @pytest.mark.parametrize("text,english", [
        (EN_REPLY, True),
        (ZH_REPLY, False),                                    # 中文夹英文术语不算英文
        ("OK, done.", False),                                 # 太短不判
        ("已修复 reconcile_daemon_code_identity,并重启 feishu_bridge_supervisor 和 "
         "feishu_bridge_daemon,selfcheck_passed。", False),  # 裸标识符不计
        ("已完成,详情见https://example.com/report,测试全部通过。\n"
         "The fix has been deployed and all regression checks passed successfully.", False),
        ("看下面的输出:\n```\n" + LOG + "\n```\n都是连接被重置,重启 worker 就好。", False),
    ])
    def test_reply_is_english(self, text, english):
        assert langcheck.reply_is_english(text) is english


def deliver(env, bid, text, seq_ts):
    env.conn.execute(
        "INSERT INTO inbox(event_id,message_id,chat_id,state,ts) VALUES(?,?,?,'enqueued',?)",
        (f"e_{seq_ts}", f"om_{seq_ts}", "oc_x", seq_ts))
    env.conn.execute(
        "INSERT INTO deliveries(binding_id,message_id,payload_json,state,enq_at) "
        "VALUES(?,?,?,'emitted',?)",
        (bid, f"om_{seq_ts}", json.dumps({"text": text, "sender_is_owner": True}), seq_ts))


class TestStopHookReminder:
    def test_non_english_question_english_reply_blocks_once(self, hook_env):
        env = hook_env
        bid = env.make_binding(status="active", session_id="sess-1")
        deliver(env, bid, "你检查一下这个 log\n" + LOG, 1)
        r = stop(env, msg=EN_REPLY)
        assert r["reason"] == "language-mismatch" and r["block_reason"]
        assert turn_jobs(env) == []                           # 这一版不转发
        r2 = stop(env, msg=EN_REPLY, sha=True)                # 续写那次:不再拦,照常转发
        assert r2["reason"] == "enqueued" and "block_reason" not in r2
        assert len(turn_jobs(env)) == 1

    def test_english_question_english_reply_forwards(self, hook_env):
        env = hook_env
        bid = env.make_binding(status="active", session_id="sess-1")
        deliver(env, bid, "Please check the deploy log for errors", 1)
        assert stop(env, msg=EN_REPLY)["reason"] == "enqueued"

    def test_short_reply_looks_back_to_last_clear_message(self, hook_env):
        env = hook_env
        bid = env.make_binding(status="active", session_id="sess-1")
        deliver(env, bid, "这个方案你怎么看,两种做法各有什么问题?", 1)
        deliver(env, bid, "A", 2)
        assert stop(env, msg=EN_REPLY)["reason"] == "language-mismatch"

    def test_matching_language_forwards(self, hook_env):
        env = hook_env
        bid = env.make_binding(status="active", session_id="sess-1")
        deliver(env, bid, "你检查一下这个 log", 1)
        assert stop(env, msg=ZH_REPLY)["reason"] == "enqueued"

    def test_only_this_bindings_messages_count(self, hook_env):
        env = hook_env
        other = env.make_binding(status="active", session_id="sess-2", chat_id="oc_other",
                                 cc_pid=4242, cc_start="other")
        bid = env.make_binding(status="active", session_id="sess-1")
        deliver(env, bid, "Please check the deploy log for errors", 1)
        deliver(env, other, "你检查一下这个 log", 2)
        assert stop(env, msg=EN_REPLY)["reason"] == "enqueued"

    def test_check_error_fails_open(self, hook_env, monkeypatch):
        env = hook_env
        bid = env.make_binding(status="active", session_id="sess-1")
        deliver(env, bid, "你检查一下这个 log", 1)

        def boom(*a, **k):
            raise RuntimeError("bug in check")
        monkeypatch.setattr(langcheck, "reply_is_english", boom)
        assert stop(env, msg=EN_REPLY)["reason"] == "enqueued"


def test_hook_stdout_carries_block_decision():
    out = hooklib.stop_hook_stdout({"reason": "language-mismatch", "block_reason": "请确认"})
    assert json.loads(out) == {"decision": "block", "reason": "请确认"}
    assert hooklib.stop_hook_stdout({"reason": "enqueued"}) is None
