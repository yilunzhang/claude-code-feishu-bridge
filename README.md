# feishu-bridge — 飞书群 ↔ 本地 Claude Code session 绑定桥(Claude Code plugin)

设计:`~/csr/knowledge/feishu-bridge-plan.md` v7(七轮 codex 对抗评审收敛)。
定位:build-in-public 桥。本地 CC session 与飞书群一一绑定;群内 @bot 消息投递进 session 作为指令;session 每 turn 最终输出自动转发回群。只管 `chat_type=group`(DM 桥 = `feishu-chat` skill)。

**打包形态 = 标准 Claude Code plugin**:plugin 根含 `.claude-plugin/plugin.json`、`skills/bridge/SKILL.md`(自动发现)、`hooks/hooks.json`(Stop/SessionEnd,自动加载),以及 `bin/ lib/ schema.sql tests/`。安装 plugin 即自带 hooks——**不再需要手改 `settings.json`**。

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

## 安装

依赖:系统 `python3`(≥3.9,纯标准库)、已登录的 `lark-cli`(bot+user 双身份)。

**A. 安装 plugin**(每机一次):

```
# 从 marketplace 安装(分发就绪后)
/plugin marketplace add <marketplace-url>
/plugin install feishu-bridge

# 或本地开发/试用:指向 plugin 根目录(含 .claude-plugin/plugin.json)
claude --plugin-dir ~/csr/feishu-bridge
```

安装后**重启 Claude Code**(hooks 在会话启动时加载)。skill 的 slash 入口 = **`/feishu-bridge:bridge`**(plugin 名:skill 名)。`hooks/hooks.json` 由 CC 自动发现(exec form,`command:"python3"` + `args:["${CLAUDE_PLUGIN_ROOT}/hooks/…"]`),Stop/SessionEnd hook 自定位——**无需任何手改 settings.json**。`preflight` 通过分事件「hooks 心跳」(Stop/SessionEnd 各一个哨兵,记 event/时间/plugin 版本/pkg_root)确认 hooks 是否已生效——**advisory only**,权威证明是「握手成功 + 群内 ✅ 已绑定」。

**B. 配置身份指纹**(每人一次;profile 一经选定钉死):在 CC 里跑 `/feishu-bridge:bridge` 走引导,或直接:

```bash
python3 <plugin根>/bin/bridgectl.py bootstrap --profile <lark-cli profile 名> \
  [--chat-allowlist oc_xxx,oc_yyy]
```

(`<plugin根>` = plugin 安装目录;SKILL.md 内用 `${CLAUDE_SKILL_DIR}/../../bin` 自动定位。)

   `--chat-allowlist`(可选):逗号分隔的 chat_id 白名单,用于**灰度/测试隔离**——覆盖全链:
   入站事件在任何入库/回复之前直接丢弃(零副作用零痕迹);列外群的审批回调按无效处理;
   bind 列外群直接报错;出站兜底(列外 job 一律 cancelled)。缺省/空 = 不限制(plan 语义)。
   改动 allowlist 需编辑 `config.json` 后重启 daemon 生效;`status` 会显示当前值。

**C. 更新 / 迁移**(plugin 版本升级、marketplace 换安装根、或从旧 standalone 迁来):daemon 是 detached 常驻,可能仍跑**旧代码**并与新 hook/新 CLI 共用同一 `bridge.db`。处理:**更新后重启 CC**,下次 `bind` 时 CLI 在建绑定前做一步**串行 code-identity 检查**——比对自己的代码身份(`pkg_root|plugin_version`,pkg_root 换根或 version 变都会检测到)与 daemon 启动时记录的 `daemon_code_identity`,**不一致则自动安全重启旧 daemon**(精确 pid+start 匹配 SIGTERM→等 flock 释放→拉起本版本新的)。
- 想手动先停:先 `... status` 看 `daemon.pid`,再 `kill <pid>`;或 `pkill -f 'feishu-bridge.*/bin/daemon\.py'`(正则兼容 marketplace 的版本目录 `.../feishu-bridge/<version>/bin/daemon.py`)。
- **dev 注意**:同一目录改代码但不改 `plugin.json` 的 version → identity 不变、检测不到,dev 自己手动重启 daemon(或 bump version)。

## 用法

CC 里 `/feishu-bridge:bridge`(见 SKILL.md);或手动:

```bash
B=<plugin根>/bin/bridgectl.py   # plugin 安装目录;SKILL.md 内用 ${CLAUDE_SKILL_DIR}/../../bin 自动定位
python3 $B preflight            # 就绪检查(config + hooks 心跳)
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
- 敏感操作前 `/feishu-bridge:bridge unbind`(立即生效),事后 rebind。

## 数据与文件

| 路径 | 内容 |
|---|---|
| `~/.claude/data/feishu-bridge/` | 数据目录(0700);**固定路径**——四类进程(hook/daemon/listener/CLI)共享同一 `bridge.db`,detached daemon 拿不到 `${CLAUDE_PLUGIN_DATA}`,故不随 plugin 走 |
| ├ `bridge.db` | SQLite WAL,唯一持久真相(schema 见 `schema.sql`) |
| ├ `config.json` | 指纹:profile/app_id/bot_open_id/owner_open_id/cli_version(钉死) |
| ├ `bridge.lock` | daemon flock 单例锁 |
| ├ `hook_heartbeat.stop` / `.session_end` | plugin hooks 生效哨兵(各 event 分开;记 event/墙钟 ms/plugin 版本/pkg_root;preflight/bind 据此 advisory 判 hooks 是否已加载,不做安全判定) |
| ├ `daemon.log` / `hook_drops.log` | daemon 日志 / hook fail-closed 丢弃记录(轮转,无正文) |
| └ `media/<binding>/<message>/` | 附件物化(原子 rename;终态消息 7 天后清理) |

代码在 **plugin 根**下:`.claude-plugin/plugin.json`(清单)· `skills/bridge/SKILL.md`(自动发现)· `hooks/hooks.json`(自动加载)+ `hooks/stop_hook.py`/`session_end.py`(只写库)· `bin/daemon.py`(守护进程)· `bin/listener.py`(Monitor 内)· `bin/bridgectl.py`(CLI)· `lib/*`(核心逻辑)· `schema.sql` · `tests/`。所有 Python 自定位靠 `Path(__file__).resolve().parents[1]` = plugin 根(bin/lib/hooks 均直接位于根下)。

## 测试

```bash
cd <plugin根> && python3 -m pytest tests/ -q
```

全程离线:lark-cli 经可注入 runner(fake 按本机实测契约造形),事件流=可注入行迭代器,进程探测/时钟均可注入。覆盖 plan §6 全矩阵(DDL 真跑、bind-turn 双 Stop 链闩、双 listener epoch 抢占、审批崩溃缝重放、inbox 钉死、waiting_binding 激活重过审批门、pending_bind 超时、per-kind 守卫、chunk 组内/组间序、判死矩阵含睡眠宽限与时钟回拨、unbind 级联+线性化、限速配额、sending→unknown、callback 单事务、ENOSPC)+ 指纹/版本门、bind 自愈(bind_superseded)、卡片重臂、daemon 挂死接管、consumer respawn 卫生。

**已实测契约(leader 2026-07-16 真机验证)**:lark-cli = **1.0.66**;`+messages-reply --msg-type interactive --content <card> --idempotency-key` → `ok:true` + `.data.message_id`,同 key 幂等同 id(审批卡片走此路径,fake 契约与真机一致);S3(hook/skill 进程 ppid 链上溯解析 CC 实例)已在真实进程树验证通过;mget 正文在**顶层 `content`**(渲染文本,E3);**错误信封可能打在 stderr**(stdout 空,code 嵌套 `.error.code`,E4a)→ 解析 stdout→stderr 回退;`--idempotency-key` 上限 ~50 字符(超长报 99992402,E4b)→ wire 一律传 `fb:`+sha1 短键(≤40),DB 保留可读逻辑键;`--version` 不吃全局 `--profile`(E1)。

## 已知限制(plan §8 + 诚实语义)

- **🅐 多 session 冷启动窗口并发 bind(by-design 已知限制,Yilun 2026-07-16 拍板接受)**:桥定位=个人轻量应用,**不上 launchd/systemd**(否则绑 macOS,分发受限),daemon 靠首次 bind 自愈拉起。
  - **触发条件**:多个 CC session 在 daemon **冷启动的几十秒窗口内几乎同时**首次 bind(此前没有已运行的 daemon)。
  - **后果**:其中一个 bind 可能**假成功**(显示成功但 daemon 因故未就绪)或**假失败**(显示失败但实际可重试)。
  - **影响面**:**单会话顺序 bind、或复用已起来的 daemon,均不触发**;个人使用场景罕见。
  - **恢复**:重跑 `/feishu-bridge:bridge bind`,或 `status` 查真实状态(daemon/consumer/绑定)。
  - **不影响安全**:机械审批门、未绑定不外发、allowlist 全部不受此竞态影响。
  - 已做的**缩窗+可观测**(非根治):busy waiter 等待上限覆盖 owner 最坏临界区(2*wait_s+probe_wait_s)且到期返回结构化 `in_progress`(bind 靠 `is_ready_result` 判定,不误当成功);daemon 退出安全点写 `startup=stopping` 且退出后不再刷心跳,supervisor 由此更快判定其停摆。**未做**(by-design):fencing/desired_generation 持久化、单行状态快照重构、launchd。
- **🅑 S6:daemon/总线离线期间群消息不重放**:daemon 停摆或 lark-cli event bus WS 断线期间,群里 @bot 的消息**会丢失且无回执**(S6 已实证不重放);靠下次 bind / listener 的 daemon 自愈行拉起后恢复,历史消息不补投。轻量应用可接受。
- **🅒 两个不同 identity 的 CLI 并发 bind(by-design 已知限制)**:bind 前置 code-identity 检查是**串行**做的(不在 supervisor 并发层),没为「同时开两个装了不同 plugin 版本/位置的 CC session、几乎同时 bind」这一极罕见场景加严格串行化(与 🅐 冷启动竞态同类)。后果:两者可能互相重启对方的 daemon 打转几次,最终收敛;不影响安全(机械审批门/未绑定不外发/allowlist 无洞)。个人使用几乎不触发。
- **v1 不做**:thread 出站回复(审批卡的 reply 除外)、reaction 快捷审批、消息编辑/撤回跟踪、topic 群、离线自动补投(见 🅑;LaunchAgent 已决不做,见 🅐)、长输出转文件、卡片原地更新(晚点击无卡片刷新,结果以文本通知)、原子换绑、消费级 ACK、post 内嵌图片(只取文本)、多 profile 多桥。
- **投递语义**:delivery `emitted` = 已写入 Monitor stdout 管道;到模型 = at-least-once(payload 带 message_id,session 按 id 跳重)。Monitor 可能合并连发的行。
- **unbind 线性化**:以各 CAS 提交为线性化点;unbind 提交前已进入 `sending` 的 job / 已 `leased` 的 delivery 允许其后完成(各至多 1 件在途),此后零新增。
- **阻断型 Stop hook 共存**:同一 turn 会触发多次 Stop。bind turn 有链闩全抑制;普通 turn 因 turn_group 无法跨 Stop 归并,存在重复转发组风险(preflight 检测+警告;S7 语义 A2 联合验收确认,声明限制不重设计)。
- **发送结果 `unknown`**:超时/信封缺字段/瞬态错误码会同 key 自动重试一次(服务端幂等已证);二次仍 unknown 则停发并阻塞同绑定后续 turn(status 高亮),需人工看一眼群里到底发没发。
- **错误分类 = 表驱动**:`ok:false` 按 `lib/constants.py` 的 `PERMANENT_SEND_CODES`(权限/成员关系/能力/目标不存在 → failed 不重试)/ `TRANSIENT_SEND_CODES`(频控/令牌自刷 → unknown,≤1 次重试)分类;**未知 code → unknown 留人工**。表可维护,新 code 实测后补入。
- **审批卡片发送失败自愈**:failed 的 approval_card 由恢复工人退避重臂(总尝试 ≤5 次),之后放弃并在 status 高亮(`given_up_approval_cards`)——member 消息不再无声悬挂整个审批 TTL。
- **指纹/版本门(fail-closed)**:daemon 启动与运行期校验 lark-cli 身份(appId/owner)与版本;身份确证不符=拒启;身份未验证/版本不符=**出站停摆**(入站照常入库),带退避重探;版本升级后跑 `doctor` 全链自检通过即自动重钉 `cli_version`。门在 ok 态每 10 分钟(单调钟)复检,漂移在下一循环发送前关门。
- **启动态与存活分义**:daemon 心跳(`last_loop_at`)只表"进程活着";是否"就绪"由 `startup`(probing/running/degraded/refused/**stopping**,带 generation)判定。`bridge bind` 只在 daemon 就绪(锁+心跳新鲜+startup∈{running,degraded}+同代)后继续——身份 mismatch 的 daemon 会 refused 退出,正常退出会先写 stopping;两者都不算就绪。**这套分义是缩窗+可观测,不根治 🅐 的冷启动窗口竞态**(该竞态已 by-design 接受)。
- **owner 附件物化失败**:静默落 `failed`(status 计数),不回群提示(member 路径有失败通知)。
- 事件到达依赖 lark-cli event bus 的 WS 在线;网络长断期消息丢失(同上不重放)。

## 故障排查

1. `python3 $B status`:daemon `last_loop_age_s` 应 <5s;consumer 应 ready;看 `outbound_jobs.unknown/failed` 与 `counters`。
2. daemon 不动 → `tail -50 ~/.claude/data/feishu-bridge/daemon.log`;手动 `python3 $B ensure-daemon`(锁被持有但心跳陈旧 **>180s**(=DOWNLOAD_TIMEOUT_S 120s + 60s 余量,覆盖单次最长同步下载不误判;从早期 300s 收窄)= 挂死;含网络的条目处理完都会多点刷新心跳;判定挂死后按记录的 pid+启动时间精确匹配 SIGTERM 并接管重启,全程持 singleflight 锁防两个 ensure 重叠;listener 的探活同样以"锁+心跳新鲜+就绪"为准,自动触发该自愈)。`status.outbound_gate` 非 `ok` = 出站停摆(身份未验证/版本不符),看 `gate_hint`;门在 ok 状态也每 10 分钟复检一次,身份/版本漂移会在下一循环发送前自动关门。
3. 转发缺失 → `hook_drops.log`(hook fail-closed 记录);确认 hooks 已装且 CC 重启过;确认绑定 active(`status`)。
4. 群消息进不来 → bot 是否在群里、是否真的 @ 了 bot(要结构化 @,转发/引用里的假 @ 无效)、VPN/WS 是否在线(daemon.log 的 consumer 状态)。
5. 彻底重置:unbind 全部绑定 → 杀 daemon(`pkill -f feishu-bridge/bin/daemon.py`,SIGTERM)→ 删 `~/.claude/data/feishu-bridge/`(会丢历史)→ 重新 bootstrap。
