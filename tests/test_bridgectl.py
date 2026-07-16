"""bridgectl 逻辑层(lib/ctl.py):bootstrap 身份配方 / hooks 检查(只读)/ bind/unbind / doctor。"""
import json

import pytest

from tests.conftest import CC_PID, CC_START, CHAT, PROFILE
from tests.helpers import FakeRunResult, FakeRunner, ok_envelope
from lib import config as configmod
from lib import ctl, lifecycle, paths


ZSH_PID = 8100


@pytest.fixture
def ctl_prober(prober):
    prober.set(ZSH_PID, CC_PID, "Tue Jul 15 12:00:00 2026", "zsh")
    return prober


def auth_status_runner():
    r = FakeRunner(profile=PROFILE)
    r.on_prefix(["auth", "status"], lambda a, c: FakeRunResult(0, json.dumps({
        "appId": "cli_testapp",
        "identities": {"user": {"available": True, "openId": "ou_owner"},
                       "bot": {"available": True}}})))
    r.on_prefix(["api", "GET", "/open-apis/bot/v3/info"], lambda a, c: FakeRunResult(
        0, '{"note":"noise"}\n{"bot":{"open_id":"ou_bot","app_name":"Yilun CLI"}}\n'))
    r.on_prefix(["--version"], lambda a, c: FakeRunResult(0, "1.0.66\n"))
    return r


class TestBootstrap:
    def test_bootstrap_writes_fingerprint(self, data_dir):
        from lib.clock import SystemClock
        cfg = ctl.bootstrap(auth_status_runner(), PROFILE, SystemClock())
        assert cfg["app_id"] == "cli_testapp" and cfg["owner_open_id"] == "ou_owner"
        assert cfg["bot_open_id"] == "ou_bot" and cfg["bot_name"] == "Yilun CLI"
        assert cfg["cli_version"] == "1.0.66"
        assert configmod.load_config()["profile"] == PROFILE

    def test_bootstrap_refuses_overwrite(self, cfg):
        from lib.clock import SystemClock
        with pytest.raises(configmod.ConfigError):
            ctl.bootstrap(auth_status_runner(), "other", SystemClock())


class TestHooks:
    def test_status_false_when_missing(self, data_dir):
        st = ctl.hooks_status()
        assert st["stop"] is False and st["session_end"] is False

    def test_status_true_when_installed(self, data_dir):
        p = paths.settings_json_path()
        p.write_text(ctl.hooks_snippet())
        st = ctl.hooks_status()
        assert st["stop"] and st["session_end"]

    def test_other_stop_hooks_detected(self, data_dir):
        p = paths.settings_json_path()
        snip = json.loads(ctl.hooks_snippet())
        snip["hooks"]["Stop"].append(
            {"hooks": [{"type": "command", "command": "python3 /x/other_blocking_hook.py"}]})
        p.write_text(json.dumps(snip))
        st = ctl.hooks_status()
        assert st["stop"] and st["other_stop_hooks"] == ["python3 /x/other_blocking_hook.py"]

    def test_snippet_shape(self, data_dir):
        snip = json.loads(ctl.hooks_snippet())
        stop_cmd = snip["hooks"]["Stop"][0]["hooks"][0]["command"]
        end_cmd = snip["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
        assert "feishu-bridge/hooks/stop_hook.py" in stop_cmd
        assert "feishu-bridge/hooks/session_end.py" in end_cmd
        assert stop_cmd.startswith("python3 /")  # 绝对路径


class TestBindUnbind:
    def test_bind_prepare_creates_rows(self, env, ctl_prober):
        res = ctl.bind_prepare(env.conn, env.cfg, env.clock, ctl_prober,
                               chat_id=CHAT, chat_name="测试群", cwd="/tmp/p",
                               start_pid=ZSH_PID)
        assert res["marker"].startswith("[feishu-bridge-bind:")
        assert res["binding_id"] in res["listener_cmd"]
        b = env.conn.execute("SELECT * FROM bindings WHERE binding_id=?",
                             (res["binding_id"],)).fetchone()
        assert b["status"] == "starting" and b["cc_pid"] == CC_PID
        assert b["cc_start"] == CC_START

    def test_bind_conflict_surfaces(self, env, ctl_prober):
        env.make_binding(status="active", chat_id=CHAT)
        with pytest.raises(lifecycle.BindConflict):
            ctl.bind_prepare(env.conn, env.cfg, env.clock, ctl_prober,
                             chat_id=CHAT, chat_name=None, cwd=None, start_pid=ZSH_PID)

    def test_unbind_resolves_instance(self, env, ctl_prober):
        bid = env.make_binding(status="active")
        res = ctl.unbind(env.conn, env.clock, ctl_prober, start_pid=ZSH_PID)
        assert res["ok"] and res["binding_id"] == bid
        b = env.conn.execute("SELECT status, close_reason FROM bindings").fetchone()
        assert b[0] == "closed" and b[1] == "user_unbind"

    def test_unbind_without_binding(self, env, ctl_prober):
        res = ctl.unbind(env.conn, env.clock, ctl_prober, start_pid=ZSH_PID)
        assert res["ok"] is False


class TestStatusAndChats:
    def test_status_report_shape(self, env):
        env.make_binding(status="active")
        rep = ctl.status_report(env.conn, env.cfg, env.clock)
        assert rep["fingerprint"]["profile"] == PROFILE
        assert rep["schema_version"] == "1"
        assert len(rep["bindings"]) == 1
        assert rep["bindings"][0]["status"] == "active"

    def test_list_chats(self, env):
        env.runner.on_prefix(["im", "+chat-list"], lambda a, c: ok_envelope(
            {"items": [{"chat_id": "oc_1", "name": "群A"},
                       {"chat_id": "oc_2", "name": "群B"}]}))
        chats = ctl.list_chats(env.runner)
        assert [c["chat_id"] for c in chats] == ["oc_1", "oc_2"]


class TestDoctor:
    def test_doctor_send_and_recall(self, env):
        env.runner.on_prefix(["im", "+messages-send"],
                             lambda a, c: ok_envelope({"message_id": "om_doc"}))
        env.runner.on_prefix(["api", "DELETE"], lambda a, c: ok_envelope({}))
        res = ctl.doctor(env.runner, CHAT, env.clock)
        assert res["ok"] and res["message_id"] == "om_doc" and res["recalled"]
        del_call = env.runner.calls_matching("api", "DELETE")[0]
        assert del_call[0][2] == "/open-apis/im/v1/messages/om_doc"
