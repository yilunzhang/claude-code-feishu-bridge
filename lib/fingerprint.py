"""修复项1:指纹/版本门 fail-closed(§5 指纹钉死的运行时执行者)。
- verify_identity:缺字段绝不算 ok(fail-closed);确证不符=mismatch;探测失败/缺字段=unknown。
- verify_cli_version:实际 CLI 版本 vs config.cli_version(必填)。
- FingerprintGate:身份 unknown / 版本不符 → daemon_state.outbound_gate=degraded:*
  (出站停摆;入站仍可入库),带退避重探;重探读盘上 config(doctor 重钉后自动放行)。"""
from . import config as configmod
from . import db, runner as runner_mod

PROBE_BACKOFF_START_MS = 30_000
PROBE_BACKOFF_MAX_MS = 10 * 60 * 1000
REVERIFY_INTERVAL_MS = 10 * 60 * 1000  # r2-M2:ok 状态的周期复检间隔

GATE_KEY = "outbound_gate"


def verify_identity(runner, cfg):
    """→ 'ok' | 'mismatch' | 'unknown'。缺字段=unknown(不是 ok)。"""
    res = runner.run(["auth", "status"], timeout_s=20)
    env = runner_mod.parse_envelope(res.stdout)
    if res.rc != 0 or not isinstance(env, dict):
        return "unknown"
    app_id = env.get("appId")
    owner = ((env.get("identities") or {}).get("user") or {}).get("openId")
    if not app_id or not owner:
        return "unknown"  # fail-closed:缺字段不放行
    if app_id != cfg.get("app_id") or owner != cfg.get("owner_open_id"):
        return "mismatch"
    return "ok"


def probe_cli_version(runner):
    # E1:--version 是顶层元命令,不吃全局 --profile(拖尾 → rc=2)→ 裸 argv
    res = runner.run(["--version"], timeout_s=10, no_profile=True)
    if res.rc != 0 or not (res.stdout or "").strip():
        return None
    return res.stdout.strip().splitlines()[0].strip()


def verify_cli_version(runner, cfg):
    actual = probe_cli_version(runner)
    if actual is None:
        return "unknown"
    pinned = cfg.get("cli_version")
    if not pinned:
        return "mismatch"  # cli_version 必填;缺=视为不符(require_config 也会拒)
    return "ok" if actual == pinned else "mismatch"


class FingerprintGate:
    """daemon 内的出站门。startup() → 'ok'|'mismatch'|'degraded';mismatch 由 daemon 拒启。
    degraded 期间 tick() 按退避重探;全 ok → 开门。
    r2-M2:ok 状态也按 REVERIFY_INTERVAL 周期复检(身份/版本漂移 → 下一循环发送前关门;
    daemon 循环里 gate.tick 排在 outbound tick 之前)。"""

    def __init__(self, conn, cfg, runner, clock):
        self.conn = conn
        self.cfg = cfg
        self.runner = runner
        self.clock = clock
        self._backoff = PROBE_BACKOFF_START_MS
        self._next_probe_at = 0

    def _current_cfg(self):
        # doctor 重钉写盘;重探必须读盘上最新 config(内存 cfg 只作兜底)
        return configmod.load_config() or self.cfg

    def _evaluate(self):
        cfg = self._current_cfg()
        ident = verify_identity(self.runner, cfg)
        if ident == "mismatch":
            return "mismatch", "identity_mismatch"
        ver = verify_cli_version(self.runner, cfg)
        if ident == "ok" and ver == "ok":
            return "ok", None
        if ident != "ok":
            return "degraded", "identity_unverified"
        return "degraded", ("version_mismatch" if ver == "mismatch" else "version_unverified")

    def _apply(self, state, reason):
        now = self.clock.wall_ms()
        if state == "ok":
            db.set_state(self.conn, GATE_KEY, "ok")
            self._backoff = PROBE_BACKOFF_START_MS
            self._next_probe_at = now + REVERIFY_INTERVAL_MS  # ok 也定期复检(r2-M2)
        else:
            db.set_state(self.conn, GATE_KEY, f"degraded:{reason}")
            self._next_probe_at = now + self._backoff
            self._backoff = min(self._backoff * 2, PROBE_BACKOFF_MAX_MS)

    def startup(self):
        state, reason = self._evaluate()
        if state == "mismatch":
            db.set_state(self.conn, GATE_KEY, "degraded:identity_mismatch")
            return "mismatch"
        self._apply(state, reason)
        return state

    def tick(self):
        now = self.clock.wall_ms()
        if now < self._next_probe_at:
            return
        state, reason = self._evaluate()
        if state == "mismatch":
            # 运行期发现确证不符:关死门(比 degraded 更硬的语义留给 daemon 重启决断)
            db.set_state(self.conn, GATE_KEY, "degraded:identity_mismatch")
            self._next_probe_at = now + self._backoff
            self._backoff = min(self._backoff * 2, PROBE_BACKOFF_MAX_MS)
            return
        self._apply(state, reason)
