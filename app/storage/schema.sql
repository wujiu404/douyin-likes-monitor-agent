-- ============================================================================
-- 业务数据存储 · SQLite
--
-- 与 Graph 执行状态（checkpoint）是两条独立支线，**不要混用同一个库文件**：
--   业务数据  = 永久保留、靠幂等键去重、可迁移
--   执行状态  = 可清理、需 ACID、本地文件
-- 两者的写入不在同一事务内，跨两者的重放恢复靠幂等键兜底。
-- ============================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- ---------------------------------------------------------------- 配置表（控制面）
-- 这张表不是业务数据，是驱动「触发层」（扫描模式/时点/间隔）与
-- 「编排层」（阈值/采集范围）的控制面。改表即生效，不用改代码、不用重启。
CREATE TABLE IF NOT EXISTS config (
    key         TEXT PRIMARY KEY,              -- 配置项
    value       TEXT NOT NULL,                 -- 值（按 type 解析）
    type        TEXT NOT NULL DEFAULT 'str',   -- int | str | bool
    scope       TEXT NOT NULL DEFAULT '通用',   -- 调度 | 阈值 | 采集
    note        TEXT DEFAULT '',               -- 说明
    updated_at  TEXT
);

-- ---------------------------------------------------------------- 表 1 监控账号表
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,                 -- 账号名
    sec_uid     TEXT NOT NULL UNIQUE,          -- 抖音账号唯一 ID
    homepage    TEXT DEFAULT '',               -- 主页链接
    enabled     INTEGER NOT NULL DEFAULT 1,    -- 启用状态 1/0
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------- 表 2 视频快照表
CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    idem_key     TEXT NOT NULL UNIQUE,         -- run_id:video_id
    run_id       TEXT NOT NULL,
    scanned_at   TEXT NOT NULL,                -- UTC ISO8601
    account      TEXT NOT NULL,
    video_id     TEXT NOT NULL,
    title        TEXT DEFAULT '',
    publish_time TEXT DEFAULT '',
    likes        INTEGER NOT NULL DEFAULT 0,
    comments     INTEGER NOT NULL DEFAULT 0,
    shares       INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT 'mock'  -- thirdparty | browser | mock
);
CREATE INDEX IF NOT EXISTS idx_snap_video ON snapshots(video_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_snap_run   ON snapshots(run_id);

-- ---------------------------------------------------------------- 表 3 增量与告警表
CREATE TABLE IF NOT EXISTS deltas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    idem_key    TEXT NOT NULL UNIQUE,          -- run_id:video_id:delta
    run_id      TEXT NOT NULL,
    video_id    TEXT NOT NULL,
    account     TEXT NOT NULL DEFAULT '',
    title       TEXT DEFAULT '',
    prev_likes  INTEGER NOT NULL DEFAULT 0,
    curr_likes  INTEGER NOT NULL DEFAULT 0,
    delta       INTEGER NOT NULL DEFAULT 0,
    is_alert    INTEGER NOT NULL DEFAULT 0,    -- 增量 > 阈值
    alerted     INTEGER NOT NULL DEFAULT 0,    -- 是否已推送提醒
    alert_time  TEXT
);
CREATE INDEX IF NOT EXISTS idx_delta_run ON deltas(run_id);

-- ---------------------------------------------------------------- 表 4 评论命中表
CREATE TABLE IF NOT EXISTS comment_hits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    idem_key     TEXT NOT NULL UNIQUE,         -- thread_id:comment_id
    thread_id    TEXT NOT NULL,
    run_id       TEXT DEFAULT '',
    video_id     TEXT NOT NULL,
    account      TEXT DEFAULT '',
    comment_id   TEXT DEFAULT '',
    content      TEXT DEFAULT '',
    comment_time TEXT DEFAULT '',
    keywords     TEXT DEFAULT '',              -- 命中关键词，逗号分隔
    song_title   TEXT DEFAULT '',
    song_artist  TEXT DEFAULT '',
    draft        TEXT DEFAULT '',              -- 拟回复，待人工确认
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | ignored
    created_at   TEXT NOT NULL,
    decided_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_hit_thread ON comment_hits(thread_id);
CREATE INDEX IF NOT EXISTS idx_hit_status ON comment_hits(status);

-- ---------------------------------------------------------------- 表 5 扫描轮次表
CREATE TABLE IF NOT EXISTS scan_rounds (
    run_id        TEXT PRIMARY KEY,
    trigger_type  TEXT NOT NULL DEFAULT 'manual',  -- cron | manual
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    account_count INTEGER DEFAULT 0,
    video_count   INTEGER DEFAULT 0,
    source        TEXT DEFAULT '',
    alert_count   INTEGER DEFAULT 0,
    error_count   INTEGER DEFAULT 0,
    thread_ids    TEXT DEFAULT '',
    note          TEXT DEFAULT ''
);

-- ---------------------------------------------------------------- 告警推送日志
CREATE TABLE IF NOT EXISTS alert_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    video_id    TEXT NOT NULL,
    channel     TEXT NOT NULL,                 -- local | feishu_im | feishu_card（或它们的组合）
    kind        TEXT NOT NULL DEFAULT 'alert', -- alert=点赞告警 | review=评论提醒
    payload     TEXT DEFAULT '',
    sent_at     TEXT NOT NULL,
    UNIQUE(run_id, video_id, channel)          -- 推送级幂等
);
