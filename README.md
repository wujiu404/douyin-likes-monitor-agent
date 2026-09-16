# 抖音点赞监控与评论提醒 Agent

定时扫描抖音**公开**作品数据 → 落表 → 算点赞增量 → 超阈值告警 → 对问歌名的评论生成拟回复（**人工确认后才落表**）。

> **合规红线**：只读公开数据。不自动点赞、不自动评论、不自动关注。
> 拟回复只写进自己的数据库并推提醒，**任何情况下都不调用抖音的评论发布接口**。

> 本仓库只发布代码。完整设计文档（方案设计 / 架构说明 / 面试题对照 /
> 演示 Runbook / 验证清单 / 迁移手册等 11 篇）保存在作者本地 `docs/` 目录，
> 未随仓库发布。

---

## 1. 快速开始

```bash
# 1) 建虚拟环境并装依赖
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt     # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS / Linux

# 2) 配置（不填也能跑，全部有默认值）
cp .env.example .env

# 3) 启动
.venv/Scripts/python.exe run.py
```

打开 <http://127.0.0.1:8000> —— 就是控制台。
接口文档在 <http://127.0.0.1:8000/docs>。

**演示动线**（3 分钟看完核心能力）：

1. 首页点 **立即扫描** → 第 1 轮全是「首次见到」的视频，增量记 0、不告警（这是刻意的，见 §4.1）
2. 再点一次 **立即扫描** → 4 条爆款视频跨阈值 → 告警推送 → 自动起 4 个评论子图
3. 切到 **评论确认** → 每条都带着命中关键词、识别出的歌名、拟回复 → 勾选后提交
4. 切到 **配置** → 把 `scan_mode` 改成 `interval`、`threshold` 改成 `10` → 下一轮调度立即生效，不用重启

跑测试：

```bash
.venv/Scripts/python.exe -m pytest
```

217 个用例，全部离线（不碰网络、不碰飞书、不碰真实抖音）。

想一次性把**所有功能点**验一遍（含真实采集与飞书镜像）：

```bash
.venv/Scripts/python.exe tools/verify_all.py --skip-real   # 快检，约 2 分钟
.venv/Scripts/python.exe tools/verify_all.py               # 全检，约 5 分钟
```

逐项结论与实测结果记录在本地文档 `docs/09-功能验证清单.md`（未随仓库发布）。

---

## 2. 架构

### 2.1 分层

```
┌─────────────────────────────────────────────────────────────┐
│ 展示层        web/  （零构建静态页，FastAPI 直接托管）          │
├─────────────────────────────────────────────────────────────┤
│ 接口层        app/api/  4 个路由模块 / 18 个接口               │
├─────────────────────────────────────────────────────────────┤
│ 编排层        app/graph/ + app/nodes/                        │
│               监控主图（7 节点）+ 评论子图（7 节点）            │
├─────────────────────────────────────────────────────────────┤
│ 触发层        app/scheduler.py（APScheduler）                │
│               app/api/routes_scan.py（HTTP 手动入口）         │
│               ↑ 两者都调同一个 run_scan，行为完全一致           │
├─────────────────────────────────────────────────────────────┤
│ 能力层        app/providers/（数据源，三级降级链）             │
│               app/notifiers/（告警渠道）                     │
├─────────────────────────────────────────────────────────────┤
│ 存储层        app/storage/（业务数据，可换后端）               │
│               checkpointer（Graph 执行状态，独立支线）         │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 监控主图

```
START
  → load_accounts      读配置 + 取启用账号    只读，唯一读配置表的地方
  → collect_videos     降级链采集             只读外部
  → write_snapshots    写视频快照表           写，幂等键 run_id:video_id
  → compute_deltas     算增量                 只读上一轮快照
  → decide_alerts      判阈值 + 写增量告警表   写，幂等键 run_id:video_id:delta
  → [条件边] 有告警 → send_alerts  推提醒      写，幂等键 run_id:video_id:alert
  → run_comment_graph  fire-and-forget 起评论子图（不阻塞）  扫谁由 comment_scope 决定
END
```

**只有一处条件边**（有告警 / 没告警）。其余全串行——没有为了并行而并行的分支。

### 2.3 评论子图

```
scan_comments → match_keywords → identify_song → draft_reply
              → persist_drafts → notify_reviews → human_review(interrupt) → apply_decisions
```

顺序是刻意的，两个「必须在前面」各有一个坑：

- `persist_drafts` **必须**排在 `human_review` **之前**：`interrupt()` 一挂起，后面的节点就不跑了。
  如果落表放在确认之后，挂起期间「待确认」列表是空的，人工无从确认——整个环节死锁。
- `notify_reviews` 排在落表之后、挂起之前：提醒要带上拟回复，而拟回复此刻才刚有；
  排在挂起之后就永远不会执行。

`run_comment_graph` 扫哪些视频由《配置表》的 `comment_scope` 决定：默认 `all`（本轮采集到的全部），
可选 `alerted`（只扫有告警的，省额度）。**默认必须是 `all`**——选做要求没有和
「点赞增量超阈值」绑定，默认只扫告警视频的话，阈值 300 一设上去选做就永远不跑。

---

## 3. 目录

```
agent_douyin/
├── run.py                      一键启动
├── requirements.txt            宽松约束（开发用）
├── requirements.lock.txt       精确版本（换机/复现装这个）
├── pytest.ini
├── .env.example                全部配置项 + 注释
│
├── app/
│   ├── config.py               基础设施参数（改了要重启）
│   ├── main.py                 FastAPI 入口 + lifespan + 托管前端
│   ├── deps.py                 依赖容器装配
│   ├── scan_service.py         跑一轮扫描（触发层 → 编排层的唯一入口）
│   ├── scheduler.py            APScheduler，模式/时点从配置表读
│   │
│   ├── core/
│   │   ├── logging.py          结构化日志，每条带 run_id
│   │   ├── idempotency.py      幂等键的唯一来源
│   │   ├── errors.py           ScanSkipped —— 「跳过」不等于「出错」
│   │   ├── retry.py            退避重试
│   │   ├── coerce.py           配置值类型转换（显式 0 与「没填」是两件事）
│   │   ├── feishu_api.py       飞书域名 + 租户令牌缓存（表格与消息共用）
│   │   └── ratelimit.py        并发闸门 + 单账号串行
│   │
│   ├── storage/                业务数据存储
│   │   ├── base.py             StorageProvider 协议（面向业务的窄接口）
│   │   ├── schema.sql          SQLite 表结构（6 张表）
│   │   ├── sqlite.py           默认后端
│   │   └── feishu.py           飞书多维表格后端（含表结构自动初始化）
│   │
│   ├── providers/              数据源
│   │   ├── base.py             VideoProvider 协议
│   │   ├── mock.py             ★ 确定性的模拟数据源（演示档用）
│   │   ├── thirdparty.py       第三方接口（占位，未配置时自动跳过）
│   │   ├── browser.py          Playwright 真实采集（视频 + 评论 + sec_uid 解析）
│   │   └── registry.py         可配置的降级链
│   │
│   ├── notifiers/              告警渠道
│   │   ├── base.py             Notifier 协议（告警 + 评论提醒两条）
│   │   ├── card.py             卡片渲染单一来源（多渠共用同一套模板）
│   │   ├── local.py            本地（控制台 + 看板）
│   │   ├── feishu_im.py        飞书应用消息（直发你私聊）
│   │   ├── feishu_card.py      飞书群自定义机器人
│   │   └── composite.py        多渠道组合（`all(...)` 送达语义）
│   │
│   ├── graph/
│   │   ├── state.py            MonitorState / CommentState
│   │   ├── monitor_graph.py    主图装配
│   │   └── comment_graph.py    评论子图装配 + 发射器
│   │
│   ├── nodes/                  节点实现（一文件一节点）
│   │   ├── accounts.py  collect.py  snapshot.py  delta.py
│   │   ├── alert.py  comment_trigger.py
│   │   └── comments/   scan.py  keywords.py  song.py  draft.py  review.py
│   │
│   └── api/
│       ├── routes_scan.py      POST /scan、GET /scan/status、GET /runs
│       ├── routes_review.py    GET/POST /reviews/{thread_id}  ← 人工确认回口
│       ├── routes_data.py      stats / accounts / videos / deltas / alerts
│       └── routes_config.py    GET/PUT /config、PUT /config/preset
│
├── web/                        零构建前端（原生 HTML/CSS/JS）
│   ├── index.html  style.css  app.js
│
├── tools/                      运维/演示脚本
│   ├── login_douyin.py         一次性扫码登录（接入真实数据前跑一次）
│   ├── check_douyin_login.py   ★ 登录态体检（服务在跑就问服务，没跑才自己拉 Edge；演示/加账号后先跑它）
│   ├── resolve_sec_uid.py      短链/抖音号 → 标准 sec_uid，并回写账号表
│   ├── demo_mode.py            ★ 演示档 / 正式档一键切换（不用重启）
│   ├── feishu_status.py        飞书各表行数体检；`--set k=v` 模拟手改配置
│   ├── verify_all.py           ★ 一键功能自检（离线测试 + 隔离实例全链路 + 真实采集）
│   └── build_docs_html.py      docs 下 md 合并成单文件 HTML
│
├── tests/                      217 个用例，全离线（不碰网络/飞书/真实抖音）
│   ├── test_keywords.py        纯函数：关键词命中 + 否定词陷阱
│   ├── test_song_identify.py   曲目识别四级优先 + 原声噪声过滤
│   ├── test_storage_sqlite.py  幂等性 + 账号就地更新
│   ├── test_mock_provider.py   单调递增 + 降级链（三种「拿不到数据」分开处理）
│   ├── test_browser_login.py   登录态判据（匿名设备 cookie 不算登录）
│   ├── test_monitor_graph.py   主图端到端 + fire-and-forget
│   ├── test_comment_graph.py   挂起/恢复 + 重放幂等 + 评论扫描范围
│   ├── test_notifier_channels.py 卡片渲染 + 组合送达语义 + 收件人识别
│   └── test_api.py             HTTP 层集成
│
└── docs/                       本地设计文档（未随仓库发布，见文首说明）
```

---

## 4. 五条工程纪律

这几条不是风格偏好，是踩过坑之后定下来的。改动前请先读对应的注释。

### 4.1 确定性的判断一律用普通 Python，LLM 只出现在两个地方

算增量、判阈值、解析点赞数、关键词命中——**全是代码**。
LLM 只负责「把已经确定的信息说成人话」（`draft.py`），关掉 `LLM_ENABLED` 就走模板兜底，链路照样跑通。

`mock.py` 里的模拟点赞数也是确定性的：`点赞 = 基准值 + 增长率 × 第几轮 × 每轮等效分钟数`，
每个视频的参数由 `video_id` 哈希派生。
**是按轮次推进，不是按墙钟时间**——否则手动连点两次扫描相隔只有几秒，增量会是 0，
演示时得干等 3 分钟才能看到告警。跨进程重启靠 `set_round_baseline()` 续上起点，避免点赞回落。

### 4.2 副作用节点必须幂等

LangGraph checkpointer 恢复时会**重放节点**。所以写飞书 / 写库之前先查幂等键：

| 表 | 幂等键 |
|---|---|
| 视频快照 | `run_id:video_id` |
| 增量与告警 | `run_id:video_id:delta` |
| 告警推送 | `run_id:video_id:alert`（`alert_log` 上有 UNIQUE 约束） |
| 评论命中 | `comment:{comment_id}`（**不含 run_id**：一条评论一生只落一次、只提醒一次） |

这不是优化项。**业务存储的写入与 checkpoint 落盘不在同一事务内**，
恢复重放时可能出现「快照已写、checkpoint 未落」，没有幂等键就会产生脏数据。

### 4.3 评论子图是 fire-and-forget

`run_comment_graph` 节点只 `asyncio.create_task` 起子图就**立刻返回**，主图随即走到 END。

如果改成 `await` 等子图完成，会同时坏掉三件事：

1. **调度** —— 主图 run 挂着不结束，`max_instances=1` + `coalesce=True` 会让下一轮扫描永远排不上
2. **可观测性** —— 「一个 run 的轨迹可以 grep 出来」要求 run 能收敛
3. **台账** —— 主图不结束就写不成《扫描轮次表》

代价：主图轨迹里不含评论最终结果，要靠 `thread_id` 关联
（所以 `thread_id` 必须和 `run_id` 一起落表）。评论确认走 `POST /api/reviews/{thread_id}` 独立回口。

> 注意区分：`scan_service` 在主图 END **之后**会等一小会儿（`COMMENT_DRAIN_TIMEOUT`，默认 2 秒）
> 让子图跑到挂起点，这样扫描响应里带的「待确认评论数」是准的。这是图**之外**的等待，
> 主图早已收敛，不影响上面三件事。它同时是响应时间的上界——别设大。

### 4.4 配置表是控制面，不是存储层

`threshold`、`scan_mode`、`scan_interval_minutes`、`comment_keywords` 全部从数据库《配置表》读，
改表即生效，不用改代码、不用重启。它同时驱动**触发层**（扫描节奏）与**编排层**（阈值判定），
所以不归任何一层存储。

一轮扫描内配置是**一致的快照**：只有 `load_accounts` 节点读配置，后面只读 state，
不会出现「算增量时阈值还是 20、判告警时被人改成 10」这种撕裂。

⚠ 从配置面读数字**必须**走 `app/core/coerce.py` 的 `as_int`，不要写
`int(cfg.get("threshold", 20) or 20)`——`or` 会把**显式填的 0 当缺失**静默顶回 20，
而且日志打印的往往是 `cfg` 原值、跟真正进 state 的值对不上，极难查。
配套：`cast_config` 对**空单元格返回 `None`（"没填"）而不是 0**，
否则「清空阈值那格」会被读成「阈值 = 0」→ 每条增量都告警。空和 0 是两件事。

### 4.5 存储拆两条支线

| | 业务数据 | Graph 执行状态 |
|---|---|---|
| 后端 | `StorageProvider`（SQLite 默认 / 飞书可选） | checkpointer（SQLite） |
| 性质 | 永久保留、幂等去重、可迁移 | 可清理、需 ACID、本地文件 |
| 文件 | `data/app.db` | `data/checkpoints.db` |

**不要混用同一个库文件。** 两者的写入不在同一事务内，跨两者的重放恢复靠幂等键兜底。

飞书后端的**内存幂等索引有不变量**：`_snap_index` / `_delta_index` / `_comment_index`
存的是 **幂等键 → 真实 record_id**，不是「这个键存在」的布尔标记。
`mark_alerted` 这类「按幂等键更新记录」的操作要拿它当 record_id 去 PUT。
踩过：`create_deltas` 曾往索引里塞空串，于是**本进程内新建的告警行永远标不上「已推送」**，
而重启后 `reindex()` 填了真 id 又正常——只在同进程生命周期内出现的 bug，最难查。
（`_alert_index` / `_round_index` 是 set，只做存在性判断，不在此列。）

---

## 5. 换个存储后端

### 5.1 SQLite（默认）

零依赖、不受外部额度限制、迁移就是拷一个文件。**个人使用推荐这个。**

### 5.2 双写：本地 + 飞书多维表格（推荐给「数据想落飞书」的场景）

```env
STORAGE_BACKEND=sqlite+feishu
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_APP_TOKEN=bascnxxxxxx          # 多维表格 URL 里 /base/ 后面那段
```

职责不对称，**本地是权威源**：

- **读**全走本地 SQLite——看板快，飞书慢/挂了不影响任何功能
- **写**先本地后飞书——飞书写失败只打告警日志，扫描照常（飞书是影子不是依赖）
- 首次启动把本地已有数据**全量回填**进飞书（幂等写，可重复），打开表格第一眼就是全部数据
- 凭证配错/飞书不可达 → 自动降级纯本地，服务照常起
- 表结构（7 张表 + 字段）**首次启动自动建**，不用手动点

#### 配置表的两边控制（三路合并）

《配置表》是唯一「两边改都生效」的表，靠 `data/config_sync.json` 里的**上次同步快照**
做三路合并，不会出现「一边的旧值把另一边的修改顶掉」：

| 场景 | 结果 |
|---|---|
| 只有飞书改过 | 飞书胜，同步回本地 |
| 只有本地（看板/API）改过 | 本地胜，推给飞书 |
| 两边都改过 / 没有快照 | **飞书胜**（《配置表》是对外声明的控制面）|

> 快照是派生态，删掉只会退化成「冲突时以飞书为准」，不丢数据。
> 踩过的坑：早期回填是「以主库为准」无条件覆盖，于是**在飞书改的阈值重启就被顶回去**，
> 等于只在重启前有效。见 `app/storage/mirror.py` 模块注释。

**《监控账号表》不做合并，一律以本地为准**（回填覆盖）。理由：账号决定「扫谁」，
属于必须单一权威的安全属性，不允许两边各说各话；加/停账号走看板的「账号」页。

#### 开通飞书这套的完整步骤（踩过的坑都在这）

1. [开发者后台](https://open.feishu.cn/app) 建**企业自建应用**（个人版飞书也能建，就是"一人企业"）。
2. **开通权限**（权限管理页搜索添加，或直接改 URL 里的 scopes 批量申请）：

   | Scope | 用途 |
   |---|---|
   | `bitable:app` | 读写多维表格（建表/建字段/写记录）——**核心，必须有** |
   | `base:app:create` | 用 `lark-cli base +base-create` 建表格 |
   | `drive:drive` | 改表格的分享权限（把自己加成协作者）|

3. **发布版本**——权限管理旁边「版本管理与发布」→ 创建版本 → 申请发布。
   ⚠ **不发布权限不生效**，代码里会看到 `app_scope_not_applied`。
4. 建多维表格（两种都行）：
   ```bash
   lark-cli base +base-create --name "抖音点赞监控" --as bot   # URL 与 app_token 会打印出来
   ```
   或在飞书里手动新建一个多维表格，复制 URL 里 `/base/` 后面那段当 `FEISHU_APP_TOKEN`。
5. **让表格对自己可见**——应用建的表格归应用所有，你本人默认打不开。二选一：
   - 把链接分享改成「组织内可编辑」（`PATCH /open-apis/drive/v1/permissions/{token}/public`，需要 `drive:drive`）；
   - 或手动把应用加成协作者。
6. 凭证写进 `.env` 的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_APP_TOKEN`，重启服务。
   启动日志里看到 `幂等索引重建完成：快照 N / 增量 N /…` 就说明通了。

体检 / 验证配置生效：

```bash
python tools/check_douyin_login.py               # 抖音登录态体检（0/1/2，加账号后、彩排前先跑）
python tools/feishu_status.py                    # 看各表行数 + 配置表当前值
python tools/feishu_status.py --set threshold=5  # 只改飞书那一格，模拟手改
python tools/clean_mock_hits.py                  # 盘点模拟数据的历史命中（dry-run）
python tools/clean_mock_hits.py --apply           # 真删命中/提醒（本地 + 飞书，先自动备份）
python tools/clean_mock_hits.py --purge-mock --apply
#   ↑ 演示前重置：连 mock 的快照/增量/点赞告警一起清
#     （mock 的 video_id 是固定的 acc01_v01…，留着它下一场第一轮就不算「首见」，
#      会直接跳出 8 条告警；顺带避免面试官在《视频快照表》里看到假 ID）
#   ⚠ 任何清理之后都要重启服务：飞书的幂等索引是启动时从飞书重建的
```

### 5.3 飞书多维表格为唯一存储

```env
STORAGE_BACKEND=feishu
```

同上建表逻辑；适合演示（可视化效果最好），额度上限大致是单表 2000 行、
每月约 1 万次 API 调用。飞书后端额外做了一件事：**幂等键在本地内存里建镜像**。
多维表格没有便宜的 EXISTS 查询，而 `exists_*` 在重放时会被高频调用，
所以启动时把各表的幂等键拉进内存索引，写完回填。
代价是「写入飞书」与「更新索引」不在同一事务内——
若进程在中间崩溃，重启后索引会**从飞书重建**（索引不是权威源，飞书才是）。

> ⚠ **在外部删过飞书表的行之后，必须重启服务**。内存索引里仍留着已删记录的
> `idem_key → record_id`，`upsert_comment_hits` 之类的写入会认为「这条已存在」而**静默跳过**
> （`fresh` 为空即 `return 0`），表现为「本地有数据、飞书表里没有」。重启时 `init()` 里的
> `reindex()` 会把索引从飞书重建，问题立刻消失（`tools/clean_mock_hits.py` 清理后就属于这种情况）。

### 5.4 告警渠道

三种后端，可用 `+` 组合（和 `STORAGE_BACKEND` 同一套写法）：

```env
NOTIFIER_BACKEND=local                    # 默认：控制台 + 看板
NOTIFIER_BACKEND=feishu_im                # 直发你本人的飞书私聊
NOTIFIER_BACKEND=local+feishu_im          # 两个都要（推荐）
NOTIFIER_BACKEND=feishu_card              # 发到某个飞书群（需群机器人 webhook）
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
```

| 后端 | 收件人 | 前提 | 占用开放平台额度 |
|---|---|---|---|
| `local` | 控制台 + 看板「告警」页 | 无 | 否 |
| `feishu_im` | **你本人的飞书私聊**（或指定人/群） | 已有自建应用即可 | 是（约 1 万次/月） |
| `feishu_card` | 某个群 | 建群自定义机器人拿 webhook | 否 |

**`feishu_im` 是推荐主力通道**：本项目已经因为多维表格建了自建应用，
复用它发消息零额外成本，而且比「自己拉个群再塞个机器人」体验好得多。
告警以一张红色交互式卡片送达，内容形如：

> **示例账号**　翻唱《某某》
> 点赞 12 → 128　增量 **+116**
> [查看视频](https://www.douyin.com/video/7412…)
> 轮次 `R2026…`　阈值 300　时间 2026-09-15 20:02:18

评论链路的「待确认拟回复」走同一个渠道（`Notifier.send_reviews`），
卡片里带评论原文、命中的关键词、拟回复，以及一行合规提示：

> **待确认拟回复**　示例账号 / 外卖小哥当众高歌…
> 评论：这是什么歌啊，好好听
> 命中：什么歌、好听
> 拟回复：这首是《夜空中最亮的星》 — 逃跑计划，喜欢可以搜来听听～
> （仅待确认，系统不会自动发送）

两类提醒在《告警推送日志》里用 `kind` 列区分（`alert` / `review`），
幂等键也各自独立：告警按轮次（`run_id:video_id:渠道`），评论提醒按 `comment_id`
（**不含 run_id**，同一条评论一生只提醒一次）。

收件人**默认不用配**：飞书没有「查我自己 open_id」的接口，
但应用信息里的 `creator_id` 就是创建者（= 你）的 open_id ——
这个应用是你自己扫码建的，所以代码会去取一次并缓存。
要发给别人/群时才需要显式配，`FEISHU_NOTIFY_RECEIVE_ID_TYPE` 支持
`open_id` / `user_id` / `email` / `chat_id`（群 ID，`oc_` 开头）。

组合渠道的送达语义是**全部渠道都成功才算送达**（返回 `all(...)`）。
刻意的：否则「飞书挂了但本地日志写成功」会被记成已推送，推送坏了你永远发现不了。
`增量与告警表` 的「已推送」列会因此保持 `False`，一眼可见。
单个渠道抛异常会被兜住，不影响后面的渠道；告警发不出去**永远不会让整轮扫描失败**。

---

## 6. 数据源降级链

```env
PROVIDER_CHAIN=thirdparty,browser,mock
```

按顺序尝试，**拿到数据**就返回。每次尝试都记进 `trace`，最终写进快照表的「数据来源」字段——
演示时能直接回答「这一轮的数据是模拟的还是真实的」。

**三种「拿不到数据」是分开处理的**（2026-09-16 修的一串真坑）：

| 情况 | 例 | 处理 |
|---|---|---|
| 真实源**没接** | 没买第三方服务、`.env` 没配浏览器目录 | 安静降级，链尾是 `mock` 就用它兜底（保证演示不中断），**不写轮次备注** |
| 真实源**坏了** | 登录态过期、被风控、接口报错 | 记下原因，**不再让 mock 顶替**，本轮如实失败（台账写明原因） |
| 真实源**成功但返回空** —— 明确知道没有 | 账号回看窗口内没发新作品、这条视频没人评论 | **不再让 mock 补数据**：如实返回空（视频 → 本轮跳过 + 409；评论 → 0 命中） |

第二、三条必须存在，否则「账号这几天没发作品」会被 mock 补上 12 条假视频、
「视频没人评论」会被 mock 补上 12 条假评论，全部照常入库、照常推送，**看板上完全看不出是假的**。
第二条的判据是 `ProviderNotConfigured`：**mock 是「没有真实源可用」的兜底，不是「真实源坏了」的兜底**——
登录态过期时宁可本轮失败，也不往真实库里写一批看不出假的行。

**采集异常会落到《扫描轮次表》的备注上**：降级原因、哪个账号没抓到、哪个账号窗口内没新作品，
都写进台账（`collect_warnings` / `collect_failures`）。这条是给用户看的——
以前它们只进日志，看板上一片「成功」，用户只能来问「为什么扫描完什么都没增加」。

`mock` 开箱即用；`browser` 已实现真实采集（见 §6.1）；`thirdparty` 是付费服务的接入点占位。
降级链靠 `VideoProvider.synthetic` 区分「真实源」与「合成源」——只有 mock 是合成源。

**降级逻辑收在 `collect_videos` 节点内部，不做成三条条件边**——
降级是采集的实现细节，不是业务流程的分支。图只暴露一个「采集成功 / 失败」的结果。

### 6.1 browser：接入真实数据（一次性登录）

原理：Playwright 驱动**已登录抖音网页版的浏览器**（本机 Chrome 或 Edge），
打开博主主页/视频页，**监听页面自己发出的 XHR** 从响应里抽数据——
不去逆向接口签名，只做读，不自动点赞/评论/关注。

```env
# .env
BROWSER_USER_DATA_DIR=D:/chrome_profiles/douyin_agent   # 建议放项目外
BROWSER_HEADLESS=true
BROWSER_CHANNEL=msedge        # 本机装了 Chrome 就写 chrome
```

一次性登录（只要做一次，登录态长期有效）：

1. 建一个空目录当浏览器用户数据目录（上面的路径），`.env` 里 `BROWSER_HEADLESS=false`
2. 启动项目 → 看板点「手动扫描」→ 会弹出 Edge/Chrome 窗口并打开抖音
3. 在弹出的窗口里**扫码登录抖音**，确认能看到已登录状态
4. 停掉项目，`BROWSER_HEADLESS=true`，以后无头跑

一条命令版本：`.venv/Scripts/python.exe tools/login_douyin.py`（自动弹窗、自动检测登录、自动改回无头）。

**登录态体检**（加了账号要验证 / 演示彩排前先跑）：

```bash
python tools/check_douyin_login.py     # 0=正常 1=过期 2=判断不了（没配好/扫描中/拉不起浏览器）
```

这个工具**先问服务**：服务在跑就调 `GET /api/login_status`，让服务用**它自己的**浏览器读 cookie。
必须这样——profile 被服务的**热上下文**占着（`_ensure_context` 有意缓存，进程退出才关），
工具再自己拉一个 Edge 必然失败（实测 `Target page, context or browser has been closed`），
而「演示前体检」恰恰就是服务开着的时候。服务没跑，它才自己无头拉一个（此时 profile 是空的，没问题）。

为什么要单独体检：登录态掉了之后**扫描本身不报错**。抖音对未登录访客照样回一份作品列表，
只是不含最新作品（实测最新一条停在 9 天前）；按回看窗口过滤后正好 0 条，
和「账号这几天没发作品」长得一模一样，整轮会被跳过，三张表都不动。
现在这条路径已经堵上（采集前查 `sessionid` cookie，未登录直接报错 + 写轮次台账），
体检工具只是让你**在扫描前 10 秒**就知道，而不用等 110 秒再猜。

不想跑脚本也可以直接看接口：`curl -s --noproxy '*' http://127.0.0.1:8000/api/login_status`，
返回 `{"configured":…, "logged_in":true/false/null, "detail":"…"}`（`null` = 查不了，原因在 `detail`）。

之后流程与 mock 完全一致：看板「监控账号」页添加真实账号（名字 + sec_uid，
主页链接 `/user/` 后面那串）→ 手动扫描一轮 → 快照表里 `source=browser`、
video_id 是真实 aweme_id → 评论子图抓真实评论、关键词命中、拟回复等人工确认。

常见问题：

| 现象 | 处理 |
|---|---|
| trace 里 `browser:failed` 且提示登录过期 | 重做一次上面的扫码登录（`tools/login_douyin.py`） |
| 轮次台账写「跳过：回看 3 天内没有新作品」，三张表都不动 | 先跑 `tools/check_douyin_login.py`；登录态正常就是真的没发新作品 |
| 体检工具报 `launch_persistent_context: Target page, context or browser has been closed` | profile 被占着。服务在跑时不该出现（工具会先问服务）；真遇到就先停服，再杀掉遗留的 `msedge --user-data-dir=…douyin_agent` 进程树 |
| 新增账号扫描后三张表都没动 | 看该轮台账备注：账号级原因（没抓到 / 窗口内没新作品）现在都写在那里 |
| 未登录的全新 profile 打开抖音是「验证码中间页」 | 正常现象（已实测）。扫码登录后登录态落在用户目录里，之后不再拦 |
| 无头模式偶发被风控拦 | 把 `BROWSER_HEADLESS=false` 常开（桌面机可见窗口跑，稳定性最好） |
| 某账号永远 0 条视频 | sec_uid 无效，或该账号风控；拿浏览器人工开同链接对比，短链可跑 `tools/resolve_sec_uid.py --write` 固化 |
| 长期采集建议 | `BROWSER_ACCOUNT_DELAY` 别低于 8s；扫描频率用 cron 每天 2~3 次，别开 3 分钟 interval 常跑 |

---

## 7. 配置项速查

`.env` 里只有**基础设施**参数（改了要重启）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `APP_HOST` / `APP_PORT` | `127.0.0.1` / `8000` | 服务监听 |
| `DEBUG` | `true` | 同时控制 uvicorn 热重载与日志级别 |
| `STORAGE_BACKEND` | `sqlite` | `sqlite` / `feishu` |
| `SQLITE_PATH` | `data/app.db` | 业务库 |
| `CHECKPOINT_PATH` | `data/checkpoints.db` | 执行状态库（**别和上面共用**） |
| `PROVIDER_CHAIN` | `thirdparty,browser,mock` | 降级链 |
| `BROWSER_USER_DATA_DIR` | 空 | 填了才启用真实采集，见 §6.1 |
| `BROWSER_HEADLESS` | `true` | 首次登录时临时设 `false` |
| `BROWSER_CHANNEL` | `msedge` | 本机装 Chrome 改 `chrome` |
| `BROWSER_ACCOUNT_DELAY` | `8` | 账号间停顿秒数（风控） |
| `NOTIFIER_BACKEND` | `local` | `local` / `feishu_im` / `feishu_card`，可用 `+` 组合 |
| `FEISHU_NOTIFY_RECEIVE_ID` | 空 | 告警私聊对象；留空自动取应用创建者（= 你） |
| `SCHEDULER_ENABLED` | `true` | 关掉就只留手动触发 |
| `ENABLE_COMMENT_GRAPH` | `true` | 关掉则完全不起评论子图 |
| `COMMENT_DRAIN_TIMEOUT` | `2` | 收尾等待上限（秒），同时是响应时间上界 |
| `LLM_ENABLED` | `false` | 关掉走模板兜底 |

**业务**参数在《配置表》里，页面上就能改：

| 键 | 默认 | 说明 |
|---|---|---|
| `scan_mode` | `cron` | `cron`=固定时点 / `interval`=固定间隔 |
| `scan_cron_hours` | `12,18,22` | cron 模式的时点 |
| `scan_interval_minutes` | `3` | interval 模式的间隔 |
| `threshold` | `300` | 点赞增量超过它就告警；演示可改 10 / 20 |
| `lookback_days` | `3` | 只采集最近 N 天发布的视频 |
| `comment_keywords` | `什么歌,歌曲名,歌名,BGM,好听` | 评论命中关键词 |
| `provider_chain` | `browser,mock` | 数据源降级链，按顺序尝试；演示档填 `mock` |
| `comment_scope` | `all` | 评论扫哪些视频：`all`=本轮采集的全部 / `alerted`=仅有告警的 |

⚠ `interval` 模式**只能短时开**。3 分钟一轮约等于每小时 160 次外部调用，
长期常开会把外部额度打爆。

### 7.1 演示档 / 正式档一键切换

正式档（每天 12/18/22 点、阈值 300、真实数据源）与演示档（3 分钟一轮、阈值 20、mock）之间切换：

```bash
.venv/Scripts/python.exe tools/demo_mode.py         # 看现在是哪一档
.venv/Scripts/python.exe tools/demo_mode.py --on    # 切演示档
.venv/Scripts/python.exe tools/demo_mode.py --off   # 切回正式档
```

一次要改 6 格配置，手改很容易漏 —— 所以档位定义只留**一份**（`app/core/presets.py`），
命令行工具与看板上的「一键切演示档 / 恢复正式档」按钮调的是**同一个接口**
`PUT /api/config/preset`。它除了写《配置表》，还会让**运行中的进程立刻重载调度器**，
所以不用重启，打印里能直接看到「调度器已重载：每 3 分钟」。

> ⚠ **切档必须连 `provider_chain` 一起切**。这一格漏了会很难查：节奏和阈值都变了、
> 看起来像切成演示档了，但数据源还是 `browser,mock` ——真实源排第一且登录态正常，
> 每轮都走真实采集、**永远轮不到 mock**，于是「没有 mock 增量、也没有告警」。
> 表面像 mock 坏了，实际是档位只切了一半。命令行和看板现在都走同一个接口，不会再漏；
> 看板配置页还会显示当前档位，状态是「⚠ 混合档位」就说明是逐格改出来的半套配置。

**切完档，看板左下角那两格要分清**：「**数据源**」是**当前配置的链**（切完立刻变，它才是
「现在会走什么」的答案）；「**上轮**」是**最近一轮扫描实际用到的源**——切档不会重跑扫描，
所以它必然滞后一轮，与当前链首选不一致时会标黄。2026-09-16 之前只有一格、且读的是上一轮结果，
于是「恢复正式档」后那格还写着 `mock`，看着像档位没切成功。

**切档会「写完读回核对」**：切档是逐格写 6 个键、每格都「先本地、后飞书」，而配置读取
**优先读飞书**——写与读不同步，中间那几秒读到的可能是「半套档位」。所以服务端写完会用
`preset_of()` 核对一遍，不一致就补写（最多 3 次）；仍不一致就返回 `verified: false`，
看板提示「请再点一次」——**不假装成功**。并发的切档请求由一把进程内锁串行化
（用户连点按钮 + 自检同时切档会交叉覆盖，2026-09-16 真撞上过）。

> ⚠ 直接改 SQLite 文件（不走接口）**不会**触发调度重载：进程里的 APScheduler
> 还挂着旧 job，cron 模式下要等到下一次触发才重读配置。工具脚本因此一律走接口，
> 服务没在跑时才退回直接写库。
>
> 同理，**在飞书《配置表》里手改**（双写模式下 `get_config` 优先读飞书）会在
> **下一轮扫描收尾时**生效——调度器每轮结束都会重读一次配置。要立刻生效就走看板或接口。

真实账号演示时，点赞增量通常过不了阈值（小账号 3 分钟涨 0~1 个赞）。
所以演示档把数据源也切成 `mock`——它的增量按**轮次**确定性推进（爆款档每轮 +22~25），
现场可复现。真实采集方案用 `--off` 恢复的 `browser` 档，见 §6.1。

---

## 8. 已知边界

写在这里是为了避免以后被当成 bug 反复排查。

| 项 | 现状 |
|---|---|
| 关键词否定词识别 | 只看命中词**前两格**有没有否定词。「不好听」能挡住，但「没觉得好听」「好听吗？我觉得一般」会误命中。**交给人工确认兜住**，不把规则越堆越复杂。测试里把这些误命中固化成了断言。 |
| 主图轨迹不含评论结果 | fire-and-forget 的代价，靠 `thread_id` 关联 |
| 长尾视频增量 | 每轮约 +1~2 赞，相邻两轮可能同值；增长率下限被测试守着（每轮增量必须 ≥1） |
| `thirdparty` | 付费服务接入点占位（需要红狐/蝉妈妈等的服务商协议后补映射） |
| browser 采集稳定性 | 抖音前端改版 / 风控升级可能让 XHR 路径变化，届时改 `AWEME_POST_API` / `COMMENT_LIST_API` 常量即可 |
| UGC 原声没有歌名 | 翻唱/原创视频的曲目元信息是 `@某某创作的原声`，平台侧就没有歌名。`identify_song` 会过滤掉它，拟回复只报歌手并明说「没查到」，**不编歌名**。带真实曲目元信息的视频才会有「歌手 + 歌名」。 |
| 真实小账号演示不告警 | 几十粉丝的号 3 分钟点赞增量是 0~1，过不了阈值。这是数据事实不是 bug——演示用 `tools/demo_mode.py --on` 切 mock |
| **切回正式档了，看板左下角「数据源」还是 `mock`** | 看错了格（2026-09-16 已拆开）：**「数据源」是当前配置的链**，切完立刻变（显示 `browser → mock` 就说明切成功了）；**「上轮」才是上一轮实际用到的源**，它要等下一轮扫描才会跟着变——那格标黄就是在说这件事。旧版本只有一格且读的是上一轮结果，才会出现「切了档还显示 mock」 |
| **切了演示档，却没有 mock 增量、也没有告警** | 先看数据源链是不是**纯 `mock`**（`tools/demo_mode.py` 的打印，或看板配置页的「当前档位」）。只要它还是 `browser,mock`，真实源就排在第一、登录态正常时每轮都成功，**mock 永远轮不到** —— 这时的现象是「节奏变快了、阈值也降了，但增量都是 0」。2026-09-16 已修：命令行与看板按钮统一走 `PUT /api/config/preset`，一次切 6 格（含 `provider_chain`），不会再切出半套档位 |
| 短链 sec_uid 每轮重解析 | 账号表里若填的是 `v.douyin.com/xxx` 短链，每轮采集会多花几秒跟重定向。跑一次 `tools/resolve_sec_uid.py --write` 换成标准 `sec_uid` 即可 |
| 切档是逐格写、不是批量事务 | 飞书侧那 6 格仍是逐个 HTTP（本地库也是逐格 `UPDATE`），所以切档进行中的那几秒，读配置可能看到「半套档位」。服务端已用**锁 + 写后读回核对**把后果堵住（`verified` + 自动补写），但存储层协议没改——这是性能问题、不是正确性问题，真要优化就加 `set_configs()` 批量写 |
| 单机单进程 | 扫描锁与切档锁都是**进程内** `asyncio.Lock`。多进程部署要换成 Redis 锁，见 §9 |

---

## 9. 要上生产需要补什么

按优先级：

1. **扫描锁换成跨进程**（Redis / 数据库锁）。当前 `scan_service._scan_lock` 是进程内的，
   多 worker 部署时 `max_instances=1` 只保护单个进程。
2. **飞书后端的幂等索引换成持久化 + 失效重建**。当前崩溃后靠 `reindex()` 全量重建，
   表大了会很慢。
3. **`draft_reply` 接真实 LLM**：`LLM_ENABLED=true` + `LLM_API_KEY`。当前只实现了
   OpenAI 兼容的 `/chat/completions`，换别家改 `draft.py` 里的 `_llm_draft` 即可。
4. **`thirdparty` provider 落地**。`browser` 已经做完（含评论采集与短链解析），
   `browser.py` 里接了 `core.ratelimit.run_per_account`，遵守「账号间可并发、单账号内必须串行」这条风控纪律；
   `thirdparty` 只是付费服务的接入点占位。
5. **告警重试与死信**。当前推送失败只记日志，不重试。
6. **把 `errors` 的 reducer 加回来**——如果将来用 LangGraph 的 `Send` API 把
   「每账号采集」拆成并行节点的话。现在全串行，reducer 没有收益。

---

## 10. 面试题对照（速览）

细则与证据记录在本地文档 `docs/05-面试题达成对照.md`（未随仓库发布）。

| 要求 | 落点 | 状态 |
|---|---|---|
| 每天 12:00 / 18:00 / 22:00 扫描最近 3 天视频 | `《配置表》scan_cron_hours` + `app/scheduler.py` | ✅ |
| 点赞数写入飞书表格 | `STORAGE_BACKEND=sqlite+feishu` → 飞书《视频快照表》 | ✅ |
| 计算相邻两次扫描的点赞增量 | `app/nodes/delta.py` + `previous_snapshot` | ✅ |
| 增量超 300 发提醒 | `《配置表》threshold=300` + `app/nodes/alert.py` → 飞书私聊卡片 | ✅ |
| 演示时改 3 分钟间隔 / 阈值 10 或 20 | `tools/demo_mode.py --on` | ✅ |
| 选做：评论关键词 → 拟回复（含歌手歌名）→ 发提醒 | 评论子图 6 节点，`interrupt()` 人工确认 | ✅ |

数据获取：**真实接入（Playwright + 已登录浏览器）与模拟数据双实现**，
降级链本身也是《配置表》里的一格（`provider_chain`），现场可换。

