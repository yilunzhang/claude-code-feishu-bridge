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
    hooks = ctl.hooks_live_status()
    # plugin 化:hooks 由 plugin 提供,不再要求手改 settings.json。ready 只看 config 是否已配置;
    # hooks 生效与否作为**信息/提示**呈现(哨兵心跳),无法在同 turn 内硬性验证。
    ready = bool(cfg)
    res = {
        "ok": ready,
        "config_present": bool(cfg),
        "hooks": hooks,
        "next_steps": [],
    }
    if not cfg:
        res["next_steps"].append(
            "首次配置(每人一次):python3 bin/bridgectl.py bootstrap --profile <lark-cli profile 名>")
    if not hooks["seen"]:
        res["next_steps"].append(
            "尚未检测到 plugin hooks 心跳(全新安装 / 尚未完成一轮对话时是正常的)。"
            "hooks 由 plugin 自带 —— 若刚 /plugin install 或更新了 feishu-bridge,请**重启 Claude Code** "
            "让 hooks 生效;之后随便完成一轮对话即会记录心跳(可再跑 preflight 确认)。"
            "**绝不需要手改 settings.json。**")
    elif not hooks["fresh"]:
        res["next_steps"].append(
            f"检测到 hooks 心跳但较旧(age {hooks['age_s']}s);若近期换过 plugin 版本,重启 CC 后完成一轮对话刷新。")
    foreign = ctl.foreign_stop_hooks()
    if foreign:
        res["foreign_stop_hooks"] = foreign
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
    # plugin 化:hooks 由 plugin 提供,不再硬阻断/要求手改 settings.json。绑定确认(握手)依赖
    # Stop hook;若 hooks 未生效,握手不会完成,pending_bind 会在 TTL 后 bind_timeout 优雅收尾。
    # 这里只做**软提示**(哨兵心跳),bind 照常进行。
    hooks = ctl.hooks_live_status()
    state = ctl.ensure_daemon()
    # r7-1:bind 只在 daemon **结构化就绪**(is_ready_result)时继续,别再靠"排除字符串 failed"。
    if not ctl.is_ready_result(state):
        if state == "in_progress":
            out({"ok": False, "retryable": True, "daemon": state,
                 "error": "daemon 正在启动(尚未就绪),请稍候重跑 /feishu-bridge bind"}, 5)
        out({"ok": False, "daemon": state, "error": "daemon 拉起失败,看 daemon.log"}, 2)
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
    res["hooks"] = hooks
    if not hooks["seen"]:
        res["hooks_note"] = ("未检测到 plugin hooks 心跳。绑定确认(握手)依赖 Stop hook —— "
                             "若刚安装/更新 plugin,请确保**已重启 Claude Code** 让 hooks 生效。"
                             "若约 10 分钟内群里未出现「✅ 已绑定」,说明 hooks 未生效:重启 CC 后重跑 bind。")
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
