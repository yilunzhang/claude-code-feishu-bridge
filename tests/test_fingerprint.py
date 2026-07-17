"""修复项1:指纹/版本门 fail-closed。缺字段≠ok;unknown→出站停摆(degraded)+退避重探;
cli_version 必填且不符→出站停摆+doctor 重钉。"""
import json

import pytest

from tests.conftest import CHAT, PROFILE
from tests.helpers import FakeRunResult, FakeRunner, ok_envelope
from lib import config as configmod
from lib import constants, db as dbmod, fingerprint, jobs


def auth_resp(app_id="cli_testapp", owner="ou_owner"):
    obj = {"identities": {"user": {"available": True}}}
    if app_id is not None:
        obj["appId"] = app_id
    if owner is not None:
        obj["identities"]["user"]["openId"] = owner
    return FakeRunResult(0, json.dumps(obj))


def runner_with(auth=None, version="1.0.66\n"):
    r = FakeRunner(profile=PROFILE)
    if auth is not None:
        r.on_prefix(["auth", "status"], lambda a, c: auth)
    r.on_prefix(["--version"], lambda a, c: FakeRunResult(0, version))
    return r


class TestVerifyFingerprint:
    def test_match_ok(self, cfg):
        assert fingerprint.verify_identity(runner_with(auth_resp()), cfg) == "ok"

    def test_missing_app_id_not_ok(self, cfg):
        r = fingerprint.verify_identity(runner_with(auth_resp(app_id=None)), cfg)
        assert r == "unknown"  # 缺字段绝不算 ok(fail-closed)

    def test_missing_owner_not_ok(self, cfg):
        assert fingerprint.verify_identity(
            runner_with(auth_resp(owner=None)), cfg) == "unknown"

    def test_mismatch(self, cfg):
        assert fingerprint.verify_identity(
            runner_with(auth_resp(app_id="cli_evil")), cfg) == "mismatch"
        assert fingerprint.verify_identity(
            runner_with(auth_resp(owner="ou_evil")), cfg) == "mismatch"

    def test_probe_failure_unknown(self, cfg):
        assert fingerprint.verify_identity(
            runner_with(FakeRunResult(1, "boom")), cfg) == "unknown"

    def test_version_match_mismatch_unknown(self, cfg):
        assert fingerprint.verify_cli_version(runner_with(auth_resp()), cfg) == "ok"
        assert fingerprint.verify_cli_version(
            runner_with(auth_resp(), version="1.0.99\n"), cfg) == "mismatch"
        r = FakeRunner(profile=PROFILE)
        r.on_prefix(["--version"], lambda a, c: FakeRunResult(1, ""))
        assert fingerprint.verify_cli_version(r, cfg) == "unknown"


class TestGate:
    def _turn_job(self, env):
        bid = env.make_binding(status="active")
        jobs.create_job(env.conn, kind="session_turn", chat_id=CHAT, binding_id=bid,
                        idempotency_key="turn:g:0", turn_group="g", chunk_index=0,
                        body="x", now=env.clock.wall_ms())

    def test_unknown_identity_degrades_and_blocks_outbound(self, env):
        gate = fingerprint.FingerprintGate(
            env.conn, env.cfg, runner_with(FakeRunResult(1, "")), env.clock)
        assert gate.startup() == "degraded"
        assert dbmod.get_state(env.conn, "outbound_gate").startswith("degraded:identity")
        self._turn_job(env)
        assert env.outbound.tick() == 0  # 出站停摆
        assert env.conn.execute(
            "SELECT state FROM outbound_jobs WHERE idempotency_key='turn:g:0'"
        ).fetchone()[0] == "pending"
        assert env.runner.calls == []

    def test_reprobe_with_backoff_then_clears(self, env):
        flaky = {"fail": True}
        r = FakeRunner(profile=PROFILE)
        r.on_prefix(["auth", "status"],
                    lambda a, c: FakeRunResult(1, "") if flaky["fail"] else auth_resp())
        r.on_prefix(["--version"], lambda a, c: FakeRunResult(0, "1.0.66\n"))
        gate = fingerprint.FingerprintGate(env.conn, env.cfg, r, env.clock)
        assert gate.startup() == "degraded"
        n_probes = len(r.calls_matching("auth", "status"))
        gate.tick()  # 退避期内不重探
        assert len(r.calls_matching("auth", "status")) == n_probes
        flaky["fail"] = False
        env.clock.tick(fingerprint.PROBE_BACKOFF_START_MS + 1)
        gate.tick()
        assert dbmod.get_state(env.conn, "outbound_gate") == "ok"
        self._turn_job(env)
        env.runner.on_prefix(["im", "+messages-send"],
                             lambda a, c: ok_envelope({"message_id": "om_1"}))
        assert env.outbound.tick() == 1  # 门开,恢复发送

    def test_identity_mismatch_reported(self, env):
        gate = fingerprint.FingerprintGate(
            env.conn, env.cfg, runner_with(auth_resp(app_id="cli_evil")), env.clock)
        assert gate.startup() == "mismatch"

    def test_version_mismatch_degrades_with_hint(self, env):
        gate = fingerprint.FingerprintGate(
            env.conn, env.cfg, runner_with(auth_resp(), version="9.9.9\n"), env.clock)
        assert gate.startup() == "degraded"
        assert dbmod.get_state(env.conn, "outbound_gate") == "degraded:version_mismatch"
        from lib import ctl
        rep = ctl.status_report(env.conn, env.cfg, env.clock)
        assert rep["outbound_gate"] == "degraded:version_mismatch"
        assert "doctor" in (rep.get("gate_hint") or "")

    def test_gate_reads_repinned_config_from_disk(self, env):
        gate = fingerprint.FingerprintGate(
            env.conn, env.cfg, runner_with(auth_resp(), version="1.0.67\n"), env.clock)
        assert gate.startup() == "degraded"
        # doctor 重钉后(写盘),gate 重探应读到新版本 → 放行
        cfg2 = dict(env.cfg)
        cfg2["cli_version"] = "1.0.67"
        configmod.save_config(cfg2)
        env.clock.tick(fingerprint.PROBE_BACKOFF_START_MS + 1)
        gate.tick()
        assert dbmod.get_state(env.conn, "outbound_gate") == "ok"


class TestCliVersionRequired:
    def test_require_config_demands_cli_version(self, data_dir):
        from lib import paths
        paths.ensure_data_dir()
        import json as _json
        from lib import util
        util.atomic_write(paths.config_path(), _json.dumps({
            "profile": "main", "app_id": "cli_x", "bot_open_id": "ou_b",
            "owner_open_id": "ou_o"}))
        with pytest.raises(configmod.ConfigError):
            configmod.require_config()

    def test_bootstrap_fails_without_version(self, data_dir):
        from tests.test_bridgectl import auth_status_runner
        from lib.clock import SystemClock
        from lib import ctl
        r = auth_status_runner()
        r.responders = [x for x in r.responders
                        if not x[0](["--version"])]  # 去掉 version responder
        r.on_prefix(["--version"], lambda a, c: FakeRunResult(1, ""))
        with pytest.raises(configmod.ConfigError):
            ctl.bootstrap(r, PROFILE, SystemClock())


class TestDoctorRepin:
    def test_doctor_repins_version_after_pass(self, env):
        env.runner.on_prefix(["im", "+messages-send"],
                             lambda a, c: ok_envelope({"message_id": "om_doc"}))
        env.runner.on_prefix(["api", "DELETE"], lambda a, c: ok_envelope({}))
        env.runner.on_prefix(["--version"], lambda a, c: FakeRunResult(0, "1.0.67\n"))
        from lib import ctl
        res = ctl.doctor(env.runner, CHAT, env.clock, cfg=env.cfg)
        assert res["ok"] and res.get("repinned_cli_version") == "1.0.67"
        assert configmod.load_config()["cli_version"] == "1.0.67"

    def test_doctor_no_repin_when_same(self, env):
        env.runner.on_prefix(["im", "+messages-send"],
                             lambda a, c: ok_envelope({"message_id": "om_doc"}))
        env.runner.on_prefix(["api", "DELETE"], lambda a, c: ok_envelope({}))
        env.runner.on_prefix(["--version"], lambda a, c: FakeRunResult(0, "1.0.66\n"))
        from lib import ctl
        res = ctl.doctor(env.runner, CHAT, env.clock, cfg=env.cfg)
        assert res["ok"] and "repinned_cli_version" not in res


class TestReverify:
    """r2-M2:ok 后也周期复检;漂移 → 下一循环发送前关门。"""

    def _ok_gate(self, env, mutable):
        r = FakeRunner(profile=PROFILE)

        def auth(a, c):
            return auth_resp(app_id=mutable["app_id"])

        r.on_prefix(["auth", "status"], auth)
        r.on(lambda a: a[:1] == ["--version"], lambda a, c: FakeRunResult(0, "1.0.66\n"))
        gate = fingerprint.FingerprintGate(env.conn, env.cfg, r, env.clock)
        assert gate.startup() == "ok"
        return gate, r

    def test_ok_state_no_probe_before_interval(self, env):
        mutable = {"app_id": "cli_testapp"}
        gate, r = self._ok_gate(env, mutable)
        n = len(r.calls_matching("auth", "status"))
        gate.tick()
        assert len(r.calls_matching("auth", "status")) == n  # 10min 内不复检

    def test_ok_reverifies_and_closes_on_drift(self, env):
        mutable = {"app_id": "cli_testapp"}
        gate, r = self._ok_gate(env, mutable)
        mutable["app_id"] = "cli_evil"  # 身份漂移
        env.clock.tick(fingerprint.REVERIFY_INTERVAL_MS + 1)
        gate.tick()
        g = dbmod.get_state(env.conn, "outbound_gate")
        assert g.startswith("degraded")

    def test_loop_closes_gate_before_send_on_drift(self, env):
        """DaemonCore 循环序:gate.tick 先于 outbound.tick → 漂移后零发送。"""
        from lib.daemon_core import DaemonCore
        mutable = {"app_id": "cli_testapp"}
        gate, r = self._ok_gate(env, mutable)
        core = DaemonCore(env.conn, env.cfg, env.clock, env.inbound, env.approval,
                          env.outbound, env.recovery, gate=gate)
        bid = env.make_binding(status="active")
        jobs.create_job(env.conn, kind="session_turn", chat_id=CHAT, binding_id=bid,
                        idempotency_key="turn:g:0", turn_group="g", chunk_index=0,
                        body="秘密", now=env.clock.wall_ms())
        mutable["app_id"] = "cli_evil"
        env.clock.tick(fingerprint.REVERIFY_INTERVAL_MS + 1)
        core.loop_iteration()
        row = env.conn.execute(
            "SELECT state FROM outbound_jobs WHERE idempotency_key='turn:g:0'").fetchone()
        assert row[0] == "pending"  # 未发送(门先关)
        assert env.runner.calls_matching("im", "+messages-send") == []


class TestVersionProbeArgv:
    """E1(真机实锤):`--version` 不吃全局 --profile;拖尾会 rc=2。"""

    def test_build_argv_no_profile(self):
        from lib.runner import LarkRunner
        r = LarkRunner("main")
        assert r.build_argv(["--version"], no_profile=True) == ["lark-cli", "--version"]
        assert r.build_argv(["im", "+chat-list", "--as", "bot"]) == \
               ["lark-cli", "im", "+chat-list", "--as", "bot", "--profile", "main"]

    def test_probe_version_true_shape(self, cfg):
        """fake 对 --version+--profile 组合返回 rc=2,逼实现走裸 --version。"""
        r = FakeRunner(profile=PROFILE)
        r.on(lambda a: "--version" in a and "--profile" in a,
             lambda a, c: FakeRunResult(2, "", "unknown command"))
        r.on(lambda a: a == ["--version"], lambda a, c: FakeRunResult(0, "1.0.66\n"))
        assert fingerprint.probe_cli_version(r) == "1.0.66"

    def test_bootstrap_version_probe_true_shape(self, data_dir):
        from tests.test_bridgectl import auth_status_runner
        from lib.clock import SystemClock
        from lib import ctl
        r = auth_status_runner()
        # 移除宽松 version responder,换成真形状
        r.responders = [x for x in r.responders if not x[0](["--version"])]
        r.on(lambda a: "--version" in a and "--profile" in a,
             lambda a, c: FakeRunResult(2, "", "unknown command"))
        r.on(lambda a: a == ["--version"], lambda a, c: FakeRunResult(0, "1.0.66\n"))
        cfg = ctl.bootstrap(r, PROFILE, SystemClock())
        assert cfg["cli_version"] == "1.0.66"
