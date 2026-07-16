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
def bootstrap(runner, profile, clock):
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
    ver = runner.run(["--version"], timeout_s=10)
    cli_version = (ver.stdout or "").strip().splitlines()[0].strip() if (ver.stdout or "").strip() else None
    if ver.rc != 0 or not cli_version:
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
HUNG_THRESHOLD_MS = 60_000
_POLL_STEP_S = 0.3


def daemon_healthy(conn, now_ms=None):
    """listener/调用方探活:锁被持有 ∧ last_loop_at 新鲜(时钟回拨按新鲜处理)。"""
    if not daemon_lock_held():
        return False
    try:
        last = db.get_state(conn, "last_loop_at")
    except Exception:
        return False
    if last is None:
        return False
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    return (now - int(last)) <= HUNG_THRESHOLD_MS  # 负值(回拨)=新鲜


class DaemonSupervisor:
    """依赖全注入的 ensure 逻辑(可测):lock_held()/read_state()/spawn()/kill(pid,sig)。"""

    def __init__(self, *, lock_held, read_state, spawn, kill, prober,
                 now_ms, sleep, wait_s=12):
        self.lock_held = lock_held
        self.read_state = read_state
        self.spawn = spawn
        self.kill = kill
        self.prober = prober
        self.now_ms = now_ms
        self.sleep = sleep
        self.wait_s = wait_s

    def _fresh(self, st):
        if not st or st.get("last_loop_at") is None:
            return False
        return (self.now_ms() - int(st["last_loop_at"])) <= HUNG_THRESHOLD_MS

    def ensure(self):
        import signal as _signal
        if self.lock_held():
            st = self.read_state()
            if self._fresh(st):
                return "running"
            # 挂死:精确身份匹配才杀
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
        return self._spawn_and_wait()

    def _spawn_and_wait(self):
        spawn_at = self.now_ms()
        self.spawn()
        for _ in range(int(self.wait_s / _POLL_STEP_S) + 1):
            self.sleep(_POLL_STEP_S)
            if self.lock_held():
                st = self.read_state()
                # 新拉起必须以"晚于本次 spawn 的心跳"为 ready(旧残留心跳不算)
                if st and st.get("last_loop_at") is not None \
                        and int(st["last_loop_at"]) >= spawn_at:
                    return "started"
        return "failed"


def _read_daemon_state():
    try:
        conn = db.connect(paths.db_path(), busy_timeout_ms=2000)
        try:
            return {
                "last_loop_at": db.get_state(conn, "last_loop_at"),
                "daemon_pid": db.get_state(conn, "daemon_pid"),
                "daemon_proc_start": db.get_state(conn, "daemon_proc_start"),
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
        now_ms=lambda: int(time.time() * 1000), sleep=time.sleep, wait_s=wait_s)
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
    env = runner_mod.parse_envelope(res.stdout)
    if not runner_mod.envelope_ok(env):
        return {"ok": False, "step": "send", "detail": (res.stdout or res.stderr or "")[:400]}
    mid = runner_mod.data_of(env).get("message_id")
    if not mid:
        return {"ok": False, "step": "send", "detail": "no message_id"}
    rc = runner.run(["api", "DELETE", f"/open-apis/im/v1/messages/{mid}", "--as", "bot"],
                    timeout_s=30)
    rc_env = runner_mod.parse_envelope(rc.stdout)
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
