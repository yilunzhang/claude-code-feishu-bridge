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
    cfg = {
        "profile": profile,
        "app_id": app_id,
        "bot_open_id": bot_open_id,
        "bot_name": bot_name,
        "owner_open_id": owner,
        "cli_version": (ver.stdout or "").strip().splitlines()[0] if ver.stdout else None,
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
    st = {"stop": False, "session_end": False, "settings_path": str(p)}
    try:
        obj = json.loads(p.read_text())
    except (OSError, ValueError):
        return st
    hooks = obj.get("hooks") or {}

    def has(event, needle):
        for entry in hooks.get(event) or []:
            for h in (entry or {}).get("hooks") or []:
                if needle in (h or {}).get("command", ""):
                    return True
        return False

    st["stop"] = has("Stop", "feishu-bridge/hooks/stop_hook.py")
    st["session_end"] = has("SessionEnd", "feishu-bridge/hooks/session_end.py")
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
                "resolve_deadline_failed"):
        v = db.get_state(conn, key)
        if v is not None:
            counters[key] = v
    return {
        "fingerprint": {k: cfg.get(k) for k in
                        ("profile", "app_id", "bot_open_id", "bot_name", "owner_open_id",
                         "cli_version")},
        "schema_version": db.get_state(conn, "schema_version"),
        "daemon": daemon,
        "consumers": consumers,
        "bindings": bindings,
        "outbound_jobs": jobs_by_state,
        "deliveries": deliveries_by_state,
        "inbox": inbox_by_state,
        "counters": counters,
    }


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


def ensure_daemon(wait_s=12, spawn=True):
    if daemon_lock_held():
        return "running"
    if not spawn:
        return "down"
    daemon_py = paths.skill_root() / "bin" / "daemon.py"
    logf = open(paths.daemon_log_path(), "a")
    subprocess.Popen([sys.executable, str(daemon_py)],
                     stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                     start_new_session=True)
    logf.close()
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if daemon_lock_held():
            # 等 daemon_state 心跳可读
            try:
                conn = db.connect(paths.db_path(), busy_timeout_ms=2000)
                last = db.get_state(conn, "last_loop_at")
                conn.close()
                if last is not None:
                    return "started"
            except Exception:
                pass
        time.sleep(0.3)
    return "failed"


# ---------------------------------------------------------------- doctor(显式诊断例外,I2)
def doctor(runner, chat_id, clock):
    """真发送+撤回自检;独立 opt-in 工具,不在运行时路径。外发 message_id 落返回值。"""
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
    return {"ok": recalled, "step": "recall", "message_id": mid, "recalled": recalled}
