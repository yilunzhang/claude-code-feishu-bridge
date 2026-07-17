# feishu-bridge — 飞书群 ↔ 本地 Claude Code session 绑定桥

设计:`~/csr/knowledge/feishu-bridge-plan.md` v7(七轮 codex 对抗评审收敛)。
定位:Yilun 本机 build-in-public 桥。本地 CC session 与飞书群一一绑定;群内 @bot 消息投递进 session 作为指令;session 每 turn 最终输出自动转发回群。只管 `chat_type=group`(DM 桥 = `feishu-chat` skill)。

## 架构一图流

```
飞书 ──WS── lark-cli event bus(自带)
              │ event consume ×2(daemon 持 stdin;stderr 监控 ready;SIGTERM 管理;退避重启)
              ▼
  bridge daemon(flock 单例;单线程事件循环;bridge.db=SQLite WAL 唯一持久真相)
    入站:去重→mget 快照(钉死 binding)→结构化@判定→门禁(owner 直投/member 审批卡)→deliveries
    审批:card.action.trigger 回调=单事务(去重+机械校验+CAS+入队/物化+通知)
    出站:outbound_jobs per-kind 守卫线性化→argv 发送→契约解析(唯一发送进程)
    存活:cc_gone / listener 心跳租约 两条独立 CAS;恢复工人驱动一切非终态
              ▲ hooks 只写库(零网络):Stop / SessionEnd
              ▼ listener 领取(epoch 排他+lease),print 到 persistent Monitor stdout
  绑定 session(CC 实例)
```

不变量(plan §2):未绑定 session 的输出绝不外发(含 bind turn 自身,fail-closed);投递判定零模型参与;一切外发经 outbound_jobs 由 daemon 发出;所有状态推进=带旧状态 CAS。

## 安装(一次性)

1. 依赖:macOS 系统 `python3`(≥3.9,纯标准库)、已登录的 `lark-cli`(bot+user 双身份)。
2. **hooks 手动合入**(本工具绝不改写 settings.json):

   ```bash
   cp ~/.claude/settings.json ~/.claude/settings.json.bak-feishu-bridge
   ```

   然后把下面片段合入 `~/.claude/settings.json`(若已有 `hooks.Stop`/`hooks.SessionEnd` 数组,把对象追加进去):

   ```json
   {
     "hooks": {
       "Stop": [
         {
           "hooks": [
             {
               "type": "command",
               "command": "python3 /Users/skysniper/.claude/skills/feishu-bridge/hooks/stop_hook.py",
               "timeout": 15
             }
           ]
         }
       ],
       "SessionEnd": [
         {
           "hooks": [
             {
               "type": "command",
               "command": "python3 /Users/skysniper/.claude/skills/feishu-bridge/hooks/session_end.py",
               "timeout": 15
             }
           ]
         }
       ]
     }
   }
   ```

   合入后**重启 Claude Code**(hooks 在会话启动时快照)。`bridgectl preflight` 会校验安装并给出同样的片段;若检测到其它 Stop hook 会给出"阻断型共存"警告(见已知限制)。
3. 指纹初始化(首次;profile 一经选定钉死):

   ```bash
   python3 ~/.claude/skills/feishu-bridge/bin/bridgectl.py bootstrap --profile <lark-cli profile 名> \
     [--chat-allowlist oc_xxx,oc_yyy]
   ```

   `--chat-allowlist`(可选):逗号分隔的 chat_id 白名单,用于**灰度/测试隔离**——覆盖全链:
   入站事件在任何入库/回复之前直接丢弃(零副作用零痕迹);列外群的审批回调按无效处理;
   bind 列外群直接报错;出站兜底(列外 job 一律 cancelled)。缺省/空 = 不限制(plan 语义)。
   改动 allowlist 需编辑 `config.json` 后重启 daemon 生效;`status` 会显示当前值。

## 用法

CC 里 `/feishu-bridge`(见 SKILL.md);或手动:

```bash
B=~/.claude/skills/feishu-bridge/bin/bridgectl.py
python3 $B preflight            # 就绪检查(config + hooks)
python3 $B chats                # 列群(bot 须已入群)
python3 $B bind --chat-id oc_x --chat-name 某群   # 建绑定(自动拉起 daemon)
python3 $B status               # 全景状态
python3 $B unbind               # 解绑(立即生效)
python3 $B ensure-daemon        # 手动确活 daemon
python3 $B doctor --chat-id oc_x  # 真发送+撤回自检(opt-in,勿对生产群随手用)
```

bind 完整握手需要 CC 侧:起 persistent Monitor 跑 `bin/listener.py <binding_id>`,并在回复文本里原样包含 `[feishu-bridge-bind:<nonce>]` marker(Stop hook 借此确认"输出通道=这个 session")。流程细节见 SKILL.md。

群内行为:
- 只有 **@bot** 的消息才会被处理(结构化 mentions 判定,不看文本启发式)。
- **owner(你)** 的消息直投 session(👀 表情=已排队);**其他成员**的消息先弹审批卡片,owner 点「投递」才进 session,点「忽略」丢弃。
- image/file 会下载到本地,payload 给绝对路径;member 附件批准前不下载。
- 未绑定群 @bot → "未绑定"提示;绑定 session 已关 → "已关闭"提示(有冷却限速)。
- 敏感操作前 `/feishu-bridge unbind`(立即生效),事后 rebind。

## 数据与文件

| 路径 | 内容 |
|---|---|
| `~/.claude/data/feishu-bridge/` | 数据目录(0700) |
| ├ `bridge.db` | SQLite WAL,唯一持久真相(schema 见 `schema.sql`) |
| ├ `config.json` | 指纹:profile/app_id/bot_open_id/owner_open_id/cli_version(钉死) |
| ├ `bridge.lock` | daemon flock 单例锁 |
| ├ `daemon.log` / `hook_drops.log` | daemon 日志 / hook fail-closed 丢弃记录(轮转,无正文) |
| └ `media/<binding>/<message>/` | 附件物化(原子 rename;终态消息 7 天后清理) |

代码:`bin/daemon.py`(守护进程)· `bin/listener.py`(Monitor 内)· `bin/bridgectl.py`(CLI)· `hooks/stop_hook.py` + `hooks/session_end.py`(只写库)· `lib/*`(核心逻辑)· `schema.sql` · `tests/`。

## 测试

```bash
cd ~/.claude/skills/feishu-bridge && python3 -m pytest tests/ -q
```

全程离线:lark-cli 经可注入 runner(fake 按本机实测契约造形),事件流=可注入行迭代器,进程探测/时钟均可注入。覆盖 plan §6 全矩阵(DDL 真跑、bind-turn 双 Stop 链闩、双 listener epoch 抢占、审批崩溃缝重放、inbox 钉死、waiting_binding 激活重过审批门、pending_bind 超时、per-kind 守卫、chunk 组内/组间序、判死矩阵含睡眠宽限与时钟回拨、unbind 级联+线性化、限速配额、sending→unknown、callback 单事务、ENOSPC)+ 指纹/版本门、bind 自愈(bind_superseded)、卡片重臂、daemon 挂死接管、consumer respawn 卫生。

**已实测契约(leader 2026-07-16 真机验证)**:lark-cli = **1.0.66**;`+messages-reply --msg-type interactive --content <card> --idempotency-key` → `ok:true` + `.data.message_id`,同 key 幂等同 id(审批卡片走此路径,fake 契约与真机一致);S3(hook/skill 进程 ppid 链上溯解析 CC 实例)已在真实进程树验证通过;mget 正文在**顶层 `content`**(渲染文本,E3);**错误信封可能打在 stderr**(stdout 空,code 嵌套 `.error.code`,E4a)→ 解析 stdout→stderr 回退;`--idempotency-key` 上限 ~50 字符(超长报 99992402,E4b)→ wire 一律传 `fb:`+sha1 短键(≤40),DB 保留可读逻辑键;`--version` 不吃全局 `--profile`(E1)。

## 已知限制(plan §8 + 诚实语义)

- **v1 不做**:thread 出站回复(审批卡的 reply 除外)、reaction 快捷审批、消息编辑/撤回跟踪、topic 群、离线自动补投(daemon 停摆期间的消息**不重放**,S6 已证;LaunchAgent 待决 D1)、长输出转文件、卡片原地更新(晚点击无卡片刷新,结果以文本通知)、原子换绑、消费级 ACK、post 内嵌图片(只取文本)、多 profile 多桥。
- **投递语义**:delivery `emitted` = 已写入 Monitor stdout 管道;到模型 = at-least-once(payload 带 message_id,session 按 id 跳重)。Monitor 可能合并连发的行。
- **unbind 线性化**:以各 CAS 提交为线性化点;unbind 提交前已进入 `sending` 的 job / 已 `leased` 的 delivery 允许其后完成(各至多 1 件在途),此后零新增。
- **阻断型 Stop hook 共存**:同一 turn 会触发多次 Stop。bind turn 有链闩全抑制;普通 turn 因 turn_group 无法跨 Stop 归并,存在重复转发组风险(preflight 检测+警告;S7 语义 A2 联合验收确认,声明限制不重设计)。
- **发送结果 `unknown`**:超时/信封缺字段/瞬态错误码会同 key 自动重试一次(服务端幂等已证);二次仍 unknown 则停发并阻塞同绑定后续 turn(status 高亮),需人工看一眼群里到底发没发。
- **错误分类 = 表驱动**:`ok:false` 按 `lib/constants.py` 的 `PERMANENT_SEND_CODES`(权限/成员关系/能力/目标不存在 → failed 不重试)/ `TRANSIENT_SEND_CODES`(频控/令牌自刷 → unknown,≤1 次重试)分类;**未知 code → unknown 留人工**。表可维护,新 code 实测后补入。
- **审批卡片发送失败自愈**:failed 的 approval_card 由恢复工人退避重臂(总尝试 ≤5 次),之后放弃并在 status 高亮(`given_up_approval_cards`)——member 消息不再无声悬挂整个审批 TTL。
- **指纹/版本门(fail-closed)**:daemon 启动与运行期校验 lark-cli 身份(appId/owner)与版本;身份确证不符=拒启;身份未验证/版本不符=**出站停摆**(入站照常入库),带退避重探;版本升级后跑 `doctor` 全链自检通过即自动重钉 `cli_version`。门在 ok 态每 10 分钟(单调钟)复检,漂移在下一循环发送前关门。
- **启动态与存活分义**:daemon 心跳(`last_loop_at`)只表"进程活着";是否"就绪"由 `startup`(probing/running/degraded/refused,带 generation)判定。`bridge bind` 只在 daemon 就绪(锁+心跳新鲜+startup∈{running,degraded}+同代)后继续——身份 mismatch 的 daemon 会 refused 退出,bind 不会留下悬空绑定。
- **owner 附件物化失败**:静默落 `failed`(status 计数),不回群提示(member 路径有失败通知)。
- 事件到达依赖 lark-cli event bus 的 WS 在线;网络长断期消息丢失(同上不重放)。

## 故障排查

1. `python3 $B status`:daemon `last_loop_age_s` 应 <5s;consumer 应 ready;看 `outbound_jobs.unknown/failed` 与 `counters`。
2. daemon 不动 → `tail -50 ~/.claude/data/feishu-bridge/daemon.log`;手动 `python3 $B ensure-daemon`(锁被持有但心跳陈旧 **>300s** = 挂死——阈值远大于单次合法下载 120s,且含网络的条目处理完都会多点刷新心跳;判定挂死后按记录的 pid+启动时间精确匹配 SIGTERM 并接管重启,全程持 singleflight 锁防两个 ensure 重叠;listener 的探活同样以"锁+心跳新鲜"为准,自动触发该自愈)。`status.outbound_gate` 非 `ok` = 出站停摆(身份未验证/版本不符),看 `gate_hint`;门在 ok 状态也每 10 分钟复检一次,身份/版本漂移会在下一循环发送前自动关门。
3. 转发缺失 → `hook_drops.log`(hook fail-closed 记录);确认 hooks 已装且 CC 重启过;确认绑定 active(`status`)。
4. 群消息进不来 → bot 是否在群里、是否真的 @ 了 bot(要结构化 @,转发/引用里的假 @ 无效)、VPN/WS 是否在线(daemon.log 的 consumer 状态)。
5. 彻底重置:unbind 全部绑定 → 杀 daemon(`pkill -f feishu-bridge/bin/daemon.py`,SIGTERM)→ 删 `~/.claude/data/feishu-bridge/`(会丢历史)→ 重新 bootstrap。
