"""feishu-bridge 常量(plan v7)。时间单位一律 ms,除非后缀 _S。"""

# 出站 chunk 阈值(S9:20k CJK 单条 OK,阈值取 12000 字符)
CHUNK_LIMIT = 12000

# bind 握手 TTL(plan 4.1.4)
PENDING_BIND_TTL_MS = 10 * 60 * 1000

# member 审批 pending TTL(plan 未定数值;v1 取 6h)
PENDING_TTL_MS = 6 * 3600 * 1000

# listener 心跳节奏与新鲜度
LISTENER_TICK_S = 2.0
HEARTBEAT_FRESH_MS = 6_000       # "新鲜心跳"判定(激活门 / 多余副本判定)
HEARTBEAT_GRACE_MS = 15_000      # 判死:心跳陈旧超此值进入 suspect
SUSPECT_CONFIRM_MS = 15_000      # suspect 持续超此值才判死(两阶段)
DAEMON_GAP_MS = 10_000           # daemon 循环间隔异常 gap 阈值(睡眠恢复检测)
SUSPECT_WINDOW_MS = 30_000       # gap 后的宽限窗:暂停 listener 判死

# confirmed starting 激活超时(plan 4.1.6:超 30s 无新鲜心跳 → listener_never_ready)
ACTIVATION_TIMEOUT_MS = 30_000

# deliveries 租约
LEASE_MS = 30_000

# 入站快照/物化的有限重试(时间上限,由恢复工人驱动)
RESOLVE_DEADLINE_MS = 10 * 60 * 1000
MATERIALIZE_DEADLINE_MS = 10 * 60 * 1000

# member 全链限速配额(plan 4.2.7 / §5)
MAX_UNDECIDED_PER_CHAT = 5
SENDER_COOLDOWN_MS = 30_000
NOTICE_COOLDOWN_MS = 60_000      # 未绑定/已关闭 chat 的提示回复冷却(per chat)
INBOX_NONTERMINAL_CAP = 500      # 非终态 inbox 总量配额(非 owner 消息受限)

# media
MEDIA_MSG_QUOTA_BYTES = 100 * 1024 * 1024   # 单 message 物化配额

# retention(终态行正文裁剪 / 终态 media TTL)
RETENTION_MS = 7 * 24 * 3600 * 1000

# 出站
SEND_TIMEOUT_S = 30
MGET_TIMEOUT_S = 30
DOWNLOAD_TIMEOUT_S = 120
UNKNOWN_RETRY_DELAY_MS = 15_000
MAX_SEND_ATTEMPTS = 2            # 首发 + unknown 同 key 自动重试一次(S4)
OUTBOUND_BATCH = 20

# daemon 节奏
RECOVERY_INTERVAL_MS = 60_000
DEATH_SCAN_INTERVAL_MS = 5_000
CHECKPOINT_INTERVAL_MS = 5 * 60 * 1000

# bind marker
MARKER_PREFIX = "[feishu-bridge-bind:"

# 日志
LOG_MAX_BYTES = 5 * 1024 * 1024

# busy_timeout(有界;I5)
BUSY_TIMEOUT_DAEMON_MS = 5_000
BUSY_TIMEOUT_HOOK_MS = 3_000
BUSY_TIMEOUT_LISTENER_MS = 3_000

SCHEMA_VERSION = "1"

# inbox 终态集合(禁 TTL 删 media 的判定等)
INBOX_TERMINAL_STATES = (
    "ignored_not_mentioned", "unsupported", "rejected", "expired", "enqueued",
    "undeliverable", "failed", "unbound", "session_closed",
)
INBOX_NONTERMINAL_STATES = (
    "received", "resolving", "waiting_binding", "awaiting_approval",
    "approved_materializing",
)

# close_reason → inbound_notice 终态映射(§3/4.2.4/4.8;r7-①:bind_timeout→unbound)
UNBOUND_CLOSE_REASONS = ("user_unbind", "bind_failed", "bind_timeout")
SESSION_CLOSED_REASONS = ("cc_gone", "session_end", "listener_gone", "listener_never_ready")

SUPPORTED_MSG_TYPES = ("text", "image", "file", "post")
MEDIA_MSG_TYPES = ("image", "file")
