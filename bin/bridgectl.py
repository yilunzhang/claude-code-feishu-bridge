#!/usr/bin/env python3
"""bridgectl:feishu-bridge 控制 CLI(skill 从 CC 内调用;人也可直接跑)。
子命令:bootstrap / preflight / chats / bind / unbind / status / ensure-daemon / doctor。
输出:machine-friendly JSON 到 stdout(SKILL.md 解析);人读信息带在字段里。"""
import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from lib import config as configmod  # noqa: E402
from lib import constants, ctl, db, lifecycle, paths, procs  # noqa: E402
from lib.clock import SystemClock  # noqa: E402
from lib.runner import LarkRunner  # noqa: E402


def out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    sys.exit(code)


def open_db():
    paths.ensure_data_dir()
    conn = db.connect(paths.db_path(), busy_timeout_ms=constants.BUSY_TIMEOUT_DAEMON_MS)
    db.init_schema(conn, paths.schema_path())
    return conn


def cmd_bootstrap(args):
    clock = SystemClock()
    runner = LarkRunner(args.profile)
    allow = [x.strip() for x in (args.chat_allowlist or "").split(",") if x.strip()] or None
    try:
        cfg = ctl.bootstrap(runner, args.profile, clock, chat_allowlist=allow)
    except configmod.ConfigError as e:
        out({"ok": False, "error": str(e)}, 2)
    out({"ok": True, "config": {k: cfg.get(k) for k in
                                ("profile", "app_id", "bot_open_id", "bot_name",
                                 "owner_open_id", "cli_version")}})


def cmd_preflight(args):
    cfg = configmod.load_config()
    hooks = ctl.hooks_status()
    ready = bool(cfg) and hooks["stop"] and hooks["session_end"]
    res = {
        "ok": ready,
        "config_present": bool(cfg),
        "hooks": hooks,
        "next_steps": [],
    }
    if not cfg:
        res["next_steps"].append(
            "运行: python3 bin/bridgectl.py bootstrap --profile <lark-cli profile 名>")
    if not (hooks["stop"] and hooks["session_end"]):
        res["next_steps"].append(
            "手动把 hooks 片段合入 ~/.claude/settings.json(见 hooks_snippet),然后重启 CC 再重跑 bind;"
            "本工具绝不代写 settings.json")
        res["hooks_snippet"] = ctl.hooks_snippet()
    if hooks.get("other_stop_hooks"):
        res["warning"] = ("检测到其它 Stop hook(可能是阻断型):同一 turn 可能触发多次 Stop,"
                          "普通 turn 存在重复转发组风险(plan 4.7 已声明限制;bind turn 有链闩保护)")
    out(res, 0 if ready else 3)


def cmd_chats(args):
    cfg = configmod.require_config()
    runner = LarkRunner(cfg["profile"])
    chats = ctl.list_chats(runner)
    if chats is None:
        out({"ok": False, "error": "im +chat-list 失败(检查 VPN/lark-cli 登录)"}, 2)
    out({"ok": True, "chats": chats})


def cmd_bind(args):
    cfg = configmod.require_config()
    hooks = ctl.hooks_status()
    if not (hooks["stop"] and hooks["session_end"]):
        # plan 4.1.1:hooks 未装 → 指引合入 + 重启 CC 后重跑,终止(不建 pending)
        out({"ok": False, "error": "hooks 未安装:先合入片段并重启 CC(不建 pending)",
             "hooks_snippet": ctl.hooks_snippet()}, 3)
    state = ctl.ensure_daemon()
    if state == "failed":
        out({"ok": False, "error": "daemon 拉起失败,看 daemon.log"}, 2)
    conn = open_db()
    clock = SystemClock()
    try:
        res = ctl.bind_prepare(conn, cfg, clock, procs.SystemProber(),
                               chat_id=args.chat_id, chat_name=args.chat_name,
                               cwd=os.getcwd(), start_pid=os.getppid())
    except lifecycle.BindConflict as e:
        out({"ok": False, "error": str(e), "code": e.code}, 4)
    res["ok"] = True
    res["daemon"] = state
    res["next"] = ("1) 启动 persistent Monitor 跑 listener_cmd;"
                   "2) 在给用户的回复文本里原样包含 marker 一行(触发 Stop 握手);"
                   "3) 回复里带上 banner 提醒。")
    out(res)


def cmd_unbind(args):
    cfg = configmod.require_config()
    conn = open_db()
    res = ctl.unbind(conn, SystemClock(), procs.SystemProber(),
                     start_pid=os.getppid(), binding_id=args.binding_id)
    out(res, 0 if res.get("ok") else 4)


def cmd_status(args):
    cfg = configmod.load_config()
    if not cfg:
        out({"ok": False, "error": "未 bootstrap"}, 2)
    conn = open_db()
    rep = ctl.status_report(conn, cfg, SystemClock())
    rep["daemon_lock_held"] = ctl.daemon_lock_held()
    out(rep)


def cmd_ensure_daemon(args):
    configmod.require_config()
    out({"ok": True, "daemon": ctl.ensure_daemon()})


def cmd_doctor(args):
    cfg = configmod.require_config()
    runner = LarkRunner(cfg["profile"])
    res = ctl.doctor(runner, args.chat_id, SystemClock(), cfg=cfg)
    out(res, 0 if res.get("ok") else 2)


def main():
    p = argparse.ArgumentParser(prog="bridgectl")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("bootstrap")
    sp.add_argument("--profile", required=True)
    sp.add_argument("--chat-allowlist", default=None,
                    help="逗号分隔 chat_id 列表;缺省=不限制(E2 灰度/测试隔离)")
    sp.set_defaults(fn=cmd_bootstrap)
    sp = sub.add_parser("preflight")
    sp.set_defaults(fn=cmd_preflight)
    sp = sub.add_parser("chats")
    sp.set_defaults(fn=cmd_chats)
    sp = sub.add_parser("bind")
    sp.add_argument("--chat-id", required=True)
    sp.add_argument("--chat-name", default=None)
    sp.set_defaults(fn=cmd_bind)
    sp = sub.add_parser("unbind")
    sp.add_argument("--binding-id", default=None)
    sp.set_defaults(fn=cmd_unbind)
    sp = sub.add_parser("status")
    sp.set_defaults(fn=cmd_status)
    sp = sub.add_parser("ensure-daemon")
    sp.set_defaults(fn=cmd_ensure_daemon)
    sp = sub.add_parser("doctor")
    sp.add_argument("--chat-id", required=True)
    sp.set_defaults(fn=cmd_doctor)
    args = p.parse_args()
    try:
        args.fn(args)
    except configmod.ConfigError as e:
        out({"ok": False, "error": str(e)}, 2)


if __name__ == "__main__":
    main()
