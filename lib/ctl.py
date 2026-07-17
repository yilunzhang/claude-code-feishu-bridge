"""bridgectl 逻辑层(bin/bridgectl.py 的可测核心)。
I4 例外声明:bootstrap/chats 的读操作是 skill 交互期例外。"""
import fcntl
import json
import os
import subprocess
import sys
import time

from . import config as configmod
from . import constants, db, jobs, lifecycle, paths, procs, texts, util
from . import runner as runner_mod


# ---------------------------------------------------------------- bootstrap(S5 身份配方)
def bootstrap(runner, profile, clock, chat_allowlist=None):
    auth = runner.run(["auth", "status"], timeout_s=30)
    auth_obj = runner_mod.parse_envelope(auth.stdout)
    if auth.rc != 0 or not isinstance(auth_obj, dict):
        raise configmod.ConfigError("auth status 失败:先 lark-cli auth login")
    app_id = auth_obj.get("appId")
    owner = ((auth_obj.get("identities") or {}).get("user") or {}).get("openId")
    if not app_id or not owner:
        raise configmod.ConfigError("auth status 缺 appId / identities.user.openId")
    bot = runner.run(["api", "GET", "/open-apis/bot/v3/info", "--as", "bot",
                      "--format", "ndjson"], timeout_s=30)
    bot_open_id, bot_name = None, None
    for line in (bot.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        b = obj.get("bot") if isinstance(obj, dict) else None
        if isinstance(b, dict) and b.get("open_id"):
            bot_open_id = b["open_id"]
            bot_name = b.get("app_name")
            break
    if not bot_open_id:
        raise configmod.ConfigError("bot/v3/info 未取到 .bot.open_id(--format ndjson)")
    from . import fingerprint as fp
    cli_version = fp.probe_cli_version(runner)  # E1:裸 --version(不拖 --profile)
    if not cli_version:
        raise configmod.ConfigError("lark-cli --version 探测失败:cli_version 必填(版本门基准)")
    cfg = {
        "profile": profile,
        "app_id": app_id,
        "bot_open_id": bot_open_id,
        "bot_name": bot_name,
        "owner_open_id": owner,
        "cli_version": cli_version,
        "created_at": clock.wall_ms(),
    }
    if chat_allowlist:
        cfg["chat_allowlist"] = list(chat_allowlist)  # E2:灰度/测试隔离;缺省/空=全部
    with configmod.bootstrap_lock():
        if configmod.load_config() is not None:
            raise configmod.ConfigError(
                "config.json 已存在(指纹钉死,不隐式变);如确要换 profile,先手动删除 "
                f"{paths.config_path()} 并 unbind 所有绑定")
        configmod.save_config(cfg)
    return cfg


# ---------------------------------------------------------------- hooks(只读 settings,绝不改写)
def hook_command(name):
    return f"python3 {paths.skill_root() / 'hooks' / name}"


def hooks_status():
    p = paths.settings_json_path()
    st = {"stop": False, "session_end": False, "settings_path": str(p),
          "other_stop_hooks": []}
    try:
        obj = json.loads(p.read_text())
    except (OSError, ValueError):
        return st
    hooks = obj.get("hooks") or {}

    def commands(event):
        for entry in hooks.get(event) or []:
            for h in (entry or {}).get("hooks") or []:
                cmd = (h or {}).get("command", "")
                if cmd:
                    yield cmd

    st["stop"] = any("feishu-bridge/hooks/stop_hook.py" in c for c in commands("Stop"))
    st["session_end"] = any("feishu-bridge/hooks/session_end.py" in c
                            for c in commands("SessionEnd"))
    # 4.7 已声明限制:与阻断型 Stop hook 共存 → 安装时检测+警告(重复组风险)
    st["other_stop_hooks"] = [c for c in commands("Stop")
                              if "feishu-bridge/hooks/stop_hook.py" not in c]
    return st


def hooks_snippet():
    return json.dumps({
        "hooks": {
            "Stop": [{"hooks": [{"type": "command",
                                 "command": hook_command("stop_hook.py"),
                                 "timeout": 15}]}],
            "SessionEnd": [{"hooks": [{"type": "command",
                                       "command": hook_command("session_end.py"),
                                       "timeout": 15}]}],
        }
    }, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- chats
def list_chats(runner):
    res = runner.run(["im", "+chat-list", "--as", "bot"], timeout_s=30)
    env = runner_mod.parse_envelope(res.stdout)
    if res.rc != 0 or not runner_mod.envelope_ok(env):
        return None
    data = runner_mod.data_of(env)
    items = data.get("items") or data.get("chats") or []
    return [{"chat_id": i.get("chat_id"), "name": i.get("name")}
            for i in items if isinstance(i, dict) and i.get("chat_id")]


# ---------------------------------------------------------------- bind / unbind
def bind_prepare(conn, cfg, clock, prober, chat_id, chat_name, cwd, start_pid):
    # r3-1②(E2 盖全):目标 chat 不在 allowlist → 直接报错,零残留
    allow = cfg.get("chat_allowlist")
    if allow and chat_id not in allow:
        raise lifecycle.BindConflict(
            "chat_not_allowed",
            f"该群不在 config.json 的 chat_allowlist 内({chat_id});改 allowlist 或换群")
    inst = procs.find_cc_instance(prober, start_pid)
    if inst is None:
        raise lifecycle.BindConflict("no_instance", "无法定位 CC 实例(ppid 链解析失败)")
    pid, lstart = inst
    res = lifecycle.create_binding(conn, chat_id=chat_id, chat_name=chat_name,
                                   cwd=cwd, cc_pid=pid, cc_start=lstart, clock=clock)
    listener_cmd = f"python3 {paths.skill_root() / 'bin' / 'listener.py'} {res['binding_id']}"
    return {
        "binding_id": res["binding_id"],
        "marker": res["marker"],
        "banner": texts.BIND_BANNER,
        "listener_cmd": listener_cmd,
        "chat_id": chat_id,
        "chat_name": chat_name,
        "ttl_minutes": constants.PENDING_BIND_TTL_MS // 60000,
    }


def resolve_instance_binding(conn, prober, start_pid):
    inst = procs.find_cc_instance(prober, start_pid)
    if inst is None:
        return None
    pid, lstart = inst
    return conn.execute(
        "SELECT * FROM bindings WHERE cc_pid=? AND cc_start=? "
        "AND status IN ('starting','active')", (pid, lstart)).fetchone()


def unbind(conn, clock, prober, start_pid=None, binding_id=None):
    if binding_id is None:
        row = resolve_instance_binding(conn, prober, start_pid)
        if row is None:
            return {"ok": False, "error": "本 CC 实例没有 starting/active 绑定"}
        binding_id = row["binding_id"]
    won = lifecycle.terminate_binding(conn, binding_id, "user_unbind", clock)
    return {"ok": won, "binding_id": binding_id,
            "note": "已解绑(立即生效)" if won else "绑定已处于终态"}


# ---------------------------------------------------------------- status
def status_report(conn, cfg, clock):
    now = clock.wall_ms()

    def age(ms):
        return None if ms is None else round((now - int(ms)) / 1000, 1)

    daemon = {
        "pid": db.get_state(conn, "daemon_pid"),
        "started_at": db.get_state(conn, "daemon_started_at"),
        "last_loop_age_s": age(db.get_state(conn, "last_loop_at")),
        "suspect_until": db.get_state(conn, "suspect_until"),
        "last_error": db.get_state(conn, "last_error"),
    }
    consumers = {}
    for k in ("im.message.receive_v1", "card.action.trigger"):
        consumers[k] = {
            "ready": db.get_state(conn, f"consumer_{k}_ready"),
            "last_status": db.get_state(conn, f"consumer_{k}_last_status"),
            "restarts": db.get_state(conn, f"consumer_{k}_restarts", "0"),
        }
    bindings = []
    for b in conn.execute(
            "SELECT * FROM bindings ORDER BY binding_seq DESC LIMIT 20").fetchall():
        bindings.append({
            "binding_id": b["binding_id"][:8],
            "chat": b["chat_name"] or b["chat_id"],
            "status": b["status"],
            "phase": b["bind_phase"],
            "close_reason": b["close_reason"],
            "session": (b["session_id"] or "")[:8] or None,
            "listener_epoch": b["listener_epoch"],
            "beat_age_s": age(b["listener_beat_at"]),
            "suspect_since": b["suspect_since"],
        })
    jobs_by_state = {r[0]: r[1] for r in conn.execute(
        "SELECT state, COUNT(*) FROM outbound_jobs GROUP BY state")}
    deliveries_by_state = {r[0]: r[1] for r in conn.execute(
        "SELECT state, COUNT(*) FROM deliveries GROUP BY state")}
    inbox_by_state = {r[0]: r[1] for r in conn.execute(
        "SELECT state, COUNT(*) FROM inbox GROUP BY state")}
    counters = {}
    for key in ("hook_drop_count", "inbox_conflict_alerts", "inbox_cap_drops",
                "malformed_event_lines", "event_processing_errors", "media_failed",
                "resolve_deadline_failed", "approval_card_given_up"):
        v = db.get_state(conn, key)
        if v is not None:
            counters[key] = v
    gate = db.get_state(conn, "outbound_gate", "ok") or "ok"
    given_up_cards = conn.execute(
        "SELECT COUNT(*) FROM outbound_jobs WHERE kind='approval_card' "
        "AND state='failed' AND error='given-up'").fetchone()[0]
    rep = {
        "fingerprint": {k: cfg.get(k) for k in
                        ("profile", "app_id", "bot_open_id", "bot_name", "owner_open_id",
                         "cli_version")},
        "schema_version": db.get_state(conn, "schema_version"),
        "chat_allowlist": cfg.get("chat_allowlist") or "全部(未限制)",
        "outbound_gate": gate,
        "daemon": daemon,
        "consumers": consumers,
        "bindings": bindings,
        "outbound_jobs": jobs_by_state,
        "deliveries": deliveries_by_state,
        "inbox": inbox_by_state,
        "counters": counters,
        "given_up_approval_cards": given_up_cards,
    }
    if gate == "degraded:version_mismatch":
        rep["gate_hint"] = ("lark-cli 版本与 config.cli_version 不符,出站已停摆。"
                            "跑 `bridgectl doctor --chat-id <测试群oc>` 全链自检通过后自动重钉版本;"
                            "daemon 会在退避重探后自动放行(≤10min)。")
    elif gate.startswith("degraded"):
        rep["gate_hint"] = "身份指纹未验证(出站停摆),daemon 带退避重探;检查 VPN/lark-cli 登录。"
    return rep


# ---------------------------------------------------------------- daemon 拉起
def daemon_lock_held():
    lock = paths.lock_path()
    if not lock.exists():
        return False
    fd = os.open(str(lock), os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# 修复项5:daemon 挂死恢复。锁被持有但心跳陈旧(>HUNG_THRESHOLD)= 挂死 →
# 按记录的 (daemon_pid, daemon_proc_start) 精确匹配后 SIGTERM → 等退出 → 接管重启;
# 身份不匹配(pid 复用/无记录)绝不杀随机进程 → failed。
# r2-M1①:阈值 ≥300s(单次合法下载 120s+mget 30s 量级;配合多点心跳不误杀);
# r2-M1④:接管全程持 singleflight flock,防两个 ensure 重叠 kill/spawn。
HUNG_THRESHOLD_MS = 300_000
_POLL_STEP_S = 0.3


class _FlockSingleflight:
    def __init__(self, path):
        self.path = str(path)
        self.fd = None

    def try_acquire(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            os.close(self.fd)
            self.fd = None
            return False

    def release(self):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def state_ready(st, now_ms):
    """r4-1:就绪 = 心跳新鲜 ∧ startup ∈ {running,degraded} ∧ 同代 generation。
    probing/refused 不算就绪(heartbeat 只表活着,startup 才表就绪);
    generation 对齐防"新代心跳 + 旧代 running"误判。"""
    from .daemon_core import parse_startup, _READY_PHASES
    if not st or st.get("last_loop_at") is None:
        return False
    if (now_ms - int(st["last_loop_at"])) > HUNG_THRESHOLD_MS:
        return False  # 心跳陈旧(回拨=负值,视为新鲜)
    phase, sgen = parse_startup(st.get("startup"))
    if phase not in _READY_PHASES:
        return False
    return sgen == (st.get("daemon_generation") or "")


def daemon_healthy(conn, now_ms=None):
    """listener/调用方探活:锁被持有 ∧ startup 就绪(心跳新鲜+running/degraded+同代)。"""
    if not daemon_lock_held():
        return False
    try:
        st = {
            "last_loop_at": db.get_state(conn, "last_loop_at"),
            "startup": db.get_state(conn, "startup"),
            "daemon_generation": db.get_state(conn, "daemon_generation"),
        }
    except Exception:
        return False
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    return state_ready(st, now)


# r5-M2:等 probing 结论(record_identity + gate.startup,最坏探测 auth~20s + version~10s)的上限,
# 覆盖探测最坏 + 余量;心跳新鲜的 probing 期间绝不 takeover。
STARTUP_PROBE_WAIT_S = 40


class DaemonSupervisor:
    """依赖全注入的 ensure 逻辑(可测):lock_held()/read_state()/spawn()/kill(pid,sig);
    singleflight(可选,对象须有 try_acquire()/release())防重叠接管(r2-M1④)。
    r5:liveness(心跳)与 readiness(startup+generation)分离——
    ① takeover(SIGTERM+重启)**只依据心跳陈旧**,绝不因'尚未就绪'而杀;
    ② 心跳新鲜的 probing → 等结论(不 kill);refused → 终态失败(不重启);
    ③ 拉起/等待路径以 baseline generation 拒'旧代完整对齐'假成功(M1)。"""

    def __init__(self, *, lock_held, read_state, spawn, kill, prober,
                 now_ms, sleep, wait_s=12, singleflight=None, probe_wait_s=None):
        self.lock_held = lock_held
        self.read_state = read_state
        self.spawn = spawn
        self.kill = kill
        self.prober = prober
        self.now_ms = now_ms
        self.sleep = sleep
        self.wait_s = wait_s
        self.singleflight = singleflight
        self.probe_wait_s = probe_wait_s if probe_wait_s is not None else STARTUP_PROBE_WAIT_S

    # ---- 判定原语:严格区分 liveness 与 readiness ----
    def _ready(self, st):
        return state_ready(st, self.now_ms())

    def _heartbeat_fresh(self, st):
        """liveness:仅看心跳,不看 startup(r5-M2:takeover 只依此)。"""
        if not st or st.get("last_loop_at") is None:
            return False
        return (self.now_ms() - int(st["last_loop_at"])) <= HUNG_THRESHOLD_MS

    def _gen_of(self, st):
        from .daemon_core import parse_startup
        return (st or {}).get("daemon_generation") or parse_startup((st or {}).get("startup"))[1]

    def _phase_of(self, st):
        from .daemon_core import parse_startup
        return parse_startup((st or {}).get("startup"))[0]

    def _pid_alive(self, st):
        pid = (st or {}).get("daemon_pid")
        pstart = (st or {}).get("daemon_proc_start")
        if not pid or not pstart:
            return False
        return procs.probe_alive(self.prober, int(pid), pstart) == procs.ALIVE

    # ---- 入口 ----
    def ensure(self):
        # r5-M1④:健康态 fast-path 也必须经 singleflight,不越过正在进行的 ensure。
        if self.singleflight is not None:
            if not self.singleflight.try_acquire():
                # 另一 ensure 在处理:等它的结果,以当前代为 baseline 拒旧代残留(M1)
                return self._await_health(self._gen_of(self.read_state()))
            try:
                return self._ensure_locked()
            finally:
                self.singleflight.release()
        return self._ensure_locked()

    def _await_health(self, baseline_gen):
        """等并发 ensure 的结果:承认'新代就绪(gen != baseline)'或'当前代就绪且 pid 存活'
        (后者=稳态健康,并非旧代残留;旧代残留的 pid 已死,被排除)。零 kill 零 spawn。"""
        for _ in range(int(self.wait_s / _POLL_STEP_S) + 1):
            st = self.read_state()
            if self.lock_held() and self._ready(st) \
                    and (self._gen_of(st) != baseline_gen or self._pid_alive(st)):
                return "running"
            self.sleep(_POLL_STEP_S)
        return "failed"

    def _ensure_locked(self):
        st = self.read_state()
        if self.lock_held() and self._ready(st):
            return "running"  # 稳态健康(singleflight 下无并发重启,可信)
        if self.lock_held():
            if self._heartbeat_fresh(st):
                # r5-M2:心跳新鲜 → 绝不 takeover。按 startup 相位处置:
                phase = self._phase_of(st)
                if phase == "refused":
                    return self._await_lock_release_then_failed()  # 终态,不重启
                return self._await_probing_conclusion()  # probing/未知:等结论,不 kill
            # 心跳陈旧 = 真挂死 → 精确身份匹配 takeover(唯一 kill 路径)
            return self._takeover_and_restart(st)
        return self._spawn_and_wait()

    def _await_lock_release_then_failed(self):
        for _ in range(int(self.wait_s / _POLL_STEP_S) + 1):
            if not self.lock_held():
                break
            self.sleep(_POLL_STEP_S)
        return "failed"

    def _await_probing_conclusion(self):
        """r5-M2:等心跳新鲜的 probing 得出结论;就绪→running;refused/退出→failed;
        超时或中途心跳陈旧→failed(**不 kill**;下次 ensure 才依陈旧心跳接管)。"""
        for _ in range(int(self.probe_wait_s / _POLL_STEP_S) + 1):
            st = self.read_state()
            if not self.lock_held():
                return "failed"  # daemon 退出(如 refused)
            if self._ready(st):
                return "running"
            if self._phase_of(st) == "refused":
                return self._await_lock_release_then_failed()
            if not self._heartbeat_fresh(st):
                return "failed"  # 中途挂死:本次不误杀,交下次 ensure 接管
            self.sleep(_POLL_STEP_S)
        return "failed"

    def _takeover_and_restart(self, st):
        import signal as _signal
        pid = st.get("daemon_pid") if st else None
        pstart = st.get("daemon_proc_start") if st else None
        if not pid or not pstart:
            return "failed"
        if procs.probe_alive(self.prober, int(pid), pstart) != procs.ALIVE:
            return "failed"  # pid 复用/探测不确定:绝不杀
        try:
            self.kill(int(pid), _signal.SIGTERM)
        except OSError:
            return "failed"
        for _ in range(int(self.wait_s / _POLL_STEP_S) + 1):
            if not self.lock_held():
                break
            self.sleep(_POLL_STEP_S)
        else:
            return "failed"
        return "recovered" if self._spawn_and_wait() == "started" else "failed"

    def _spawn_and_wait(self):
        baseline = self._gen_of(self.read_state())  # r5-M1:spawn 前记 baseline generation
        self.spawn()
        # 等待上限覆盖新 daemon 的 record_identity + gate.startup 探测最坏(probe)+ 拉起余量。
        steps = int((self.wait_s + self.probe_wait_s) / _POLL_STEP_S) + 1
        for _ in range(steps):
            self.sleep(_POLL_STEP_S)
            st = self.read_state()
            gen = self._gen_of(st)
            # r5-M1:只承认**新代**(gen != baseline)就绪;旧代完整对齐(gen==baseline)一律不算。
            if self.lock_held() and self._ready(st) and gen != baseline:
                return "started"
            # 新代明确 refused → 失败(不再空等到超时;bind 不会继续)
            if self.lock_held() and gen != baseline and self._phase_of(st) == "refused":
                return "failed"
        return "failed"


def _read_daemon_state():
    try:
        conn = db.connect(paths.db_path(), busy_timeout_ms=2000)
        try:
            return {
                "last_loop_at": db.get_state(conn, "last_loop_at"),
                "daemon_pid": db.get_state(conn, "daemon_pid"),
                "daemon_proc_start": db.get_state(conn, "daemon_proc_start"),
                "startup": db.get_state(conn, "startup"),
                "daemon_generation": db.get_state(conn, "daemon_generation"),
            }
        finally:
            conn.close()
    except Exception:
        return None


def _spawn_daemon():
    daemon_py = paths.skill_root() / "bin" / "daemon.py"
    logf = open(paths.daemon_log_path(), "a")
    try:
        subprocess.Popen([sys.executable, str(daemon_py)],
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                         start_new_session=True)
    finally:
        logf.close()


def ensure_daemon(wait_s=12, spawn=True):
    if not spawn:
        try:
            conn = db.connect(paths.db_path(), busy_timeout_ms=2000)
            try:
                return "running" if daemon_healthy(conn) else "down"
            finally:
                conn.close()
        except Exception:
            return "down"
    sup = DaemonSupervisor(
        lock_held=daemon_lock_held, read_state=_read_daemon_state,
        spawn=_spawn_daemon, kill=os.kill, prober=procs.SystemProber(),
        now_ms=lambda: int(time.time() * 1000), sleep=time.sleep, wait_s=wait_s,
        singleflight=_FlockSingleflight(paths.ensure_lock_path()))
    return sup.ensure()


# ---------------------------------------------------------------- doctor(显式诊断例外,I2)
def doctor(runner, chat_id, clock, cfg=None):
    """真发送+撤回自检;独立 opt-in 工具,不在运行时路径。外发 message_id 落返回值。
    修复项1:自检通过 = 当前 CLI 版本契约已验证 → 若与 config.cli_version 不符,重钉版本
    (写盘;daemon 的 FingerprintGate 重探读盘后自动放行)。"""
    key = f"doctor:{util.new_id()}"
    res = runner.run(["im", "+messages-send", "--as", "bot", "--chat-id", chat_id,
                      "--text", f"feishu-bridge doctor 自检 {clock.wall_ms()}(即将撤回)",
                      "--idempotency-key", key], timeout_s=30)
    env = runner_mod.parse_result(res)  # E4a:stderr 信封回退
    if not runner_mod.envelope_ok(env):
        return {"ok": False, "step": "send", "detail": (res.stdout or res.stderr or "")[:400]}
    mid = runner_mod.data_of(env).get("message_id")
    if not mid:
        return {"ok": False, "step": "send", "detail": "no message_id"}
    rc = runner.run(["api", "DELETE", f"/open-apis/im/v1/messages/{mid}", "--as", "bot"],
                    timeout_s=30)
    rc_env = runner_mod.parse_result(rc)
    recalled = runner_mod.envelope_ok(rc_env)
    out = {"ok": recalled, "step": "recall", "message_id": mid, "recalled": recalled}
    if recalled and cfg:
        from . import fingerprint as fp
        actual = fp.probe_cli_version(runner)
        if actual and actual != cfg.get("cli_version"):
            new_cfg = dict(cfg)
            new_cfg["cli_version"] = actual
            configmod.save_config(new_cfg)
            out["repinned_cli_version"] = actual
    return out
