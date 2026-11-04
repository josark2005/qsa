# AGENTS.md

QSA 是一个前后端分离的条件筛选系统：FastAPI + PostgreSQL + Redis 后端，React/Vite + shadcn 前端，另含同步 worker、QQ 机器人和 PDF 报表。本文给编码代理提供项目约束和验证方式。

## 技术栈与目录

- 后端：Python 3.12、uv、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、psycopg3、APScheduler、ReportLab、matplotlib。
- 前端：React 19、TypeScript、Vite、Tailwind CSS v4、shadcn/radix-ui、TanStack Query、ECharts。
- 基础设施：PostgreSQL 18.6、Redis 7。
- `backend/app/api/routes/`：HTTP 路由。
- `backend/app/services/`：报表、用户、会话、历史归档、PDF 等业务逻辑。
- `backend/app/sync/`：行情/财报/股东同步、涨跌停重算、pipeline、全量回补。
- `backend/app/screening/`：因子注册表 `registry.py`、SQL 构建 `query_builder.py`。
- `backend/app/qq_bot/`：QQ 机器人（官方 qq-botpy SDK）、会话式选股、群订阅；`onebot/` 为独立的个人号 OneBot v11 通道。
- `backend/migrations/versions/`：Alembic 迁移；以 `alembic heads` 为准，当前为 `d4b7c2e9f1a3`。
- `frontend/src/pages/`：页面；`components/`：布局与业务组件；`lib/api.ts`、`lib/types.ts`：API 契约。
- `data/history/`：1990 至今的月度 SQLite 归档；`data/reports/`：PDF 缓存；`data/bootstrap/`：部署用数据库 dump。
- `docker-compose.yml`：本地/服务器部署栈，直接拉取 GHCR 镜像；`docker-compose.dev.yml`：完整开发栈。

## 启动与验证

Windows 当前开发环境：

```bash
# 后端
cd backend
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# worker（另开终端）
cd backend
.venv/Scripts/python.exe -m app.worker

# 前端
cd frontend
pnpm dev

# 后端测试
cd backend
.venv/Scripts/python.exe -m pytest -q

# 前端类型检查、构建、lint
cd frontend
pnpm build
pnpm lint
```

Linux/容器中使用 `.venv/bin/python`。Docker 相关命令在 Git Bash 下如遇路径被改写，加 `MSYS_NO_PATHCONV=1`。

提交前至少运行：

1. `cd backend && .venv/Scripts/python.exe -m pytest -q`
2. `cd frontend && pnpm build`
3. 修改 compose 时：`docker compose -f <file> config`
4. 修改 UI 时：浏览器实际走一遍相关流程，不能只依赖类型检查。

## 数据库与迁移

- 生产/测试数据库结构必须通过 Alembic 迁移维护；不要依赖 `Base.metadata.create_all` 更新生产库。
- 新增/修改表后生成迁移并运行 `alembic upgrade head`，同时确认 downgrade 合理。
- 测试使用 SQLite 内存/临时库并自动 `create_all`，不要写依赖 PostgreSQL 专有语法的测试代码。
- 历史归档通过 `history_imports` 记录文件名和 SHA256；重复导入会跳过。不要无理由 `--force` 重导 3GB 归档。
- 全量部署 dump 位于 `data/bootstrap/qsa-full.dump`；生产首次启动仅在 `users` 表不存在时恢复。切换数据源前先备份，不要随意删除 `data/postgres`、`data/redis`、`data/dev`。

## 后端约定

- Python 代码遵循现有 4 空格缩进与 `from __future__ import annotations` 风格。
- 路由只做参数/权限/事务边界，复杂逻辑放 `services/` 或 `sync/`。
- 除登录外，所有写操作路由需要 `verify_csrf`；管理路由使用 `require_admin`，登录后的业务路由使用 `require_ready_user`。
- 时间统一使用 `Asia/Shanghai`；数据库时间使用 UTC。
- 系统设置集中在 `app/core/settings_registry.py`。修改默认值要同时考虑数据库已有值；SettingsService 会热重载，敏感项必须保持掩码与审计脱敏。
- 登录标识是用户名，不是邮箱。用户名 2–32 位，不含空格和 `@`；不开放注册，账号由管理员创建。
- 密码最少 8 位（系统设置可调）；新增用户默认首次登录强制改密。
- 用户删除/禁用/降级必须保留至少一个有效管理员。

## 筛选因子

新增筛选条件时通常需要同时修改：

1. `backend/app/screening/registry.py`：`FactorSpec`、参数、标签、默认值。
2. `backend/app/screening/query_builder.py`：SQL 表达式；相关子查询务必正确 `.correlate(Instrument)`，避免 `cardinality violation`。
3. `frontend/src/pages/ScreenerPage.tsx` 和 `frontend/src/lib/format.ts`（如需特殊显示）。
4. 后端测试；必要时更新默认条件。

默认条件保存在系统设置 `screening.default_conditions`，不是只改代码默认值。默认筛选查询超时为 30 秒，窗口查询依赖 `ix_daily_bars_window_cover` 覆盖索引。

## 报表与 PDF

- 报表以快照形式保存在 `screen_reports`，公开链接只依赖 token，不要求登录。
- 报表最多保存 1000 行；PDF 个股详情最多 50 只，超过 50 只只输出汇总表。
- 非当日报表必须显示 stale 警告；不要把历史报表伪装成实时结果。
- 修改 PDF 模板/表头后要提升 `report_pdf.py` 的 `PDF_CACHE_VERSION`，否则旧缓存仍会被返回。
- PDF 不生成个股 K 线/趋势图：详情页只有指标表（每页最多 3 只）。K 线图在网页报表里看：表格的「走势」列是近 20 个交易日 SVG 缩略图（可用「走势」开关关闭，偏好存 localStorage），点行打开详情 Dialog（日/周/月线 + 技术指标）。
- 网页报表表格是 shadcn DataTable（@tanstack/react-table）：支持名称/代码搜索、板块筛选、点击表头排序（数值列首次点击升序）、重置与分页；新增交互优先复用 shadcn/ui 组件。
- 详情 Dialog 的技术图固定只画最近 20 根 K 线并关闭 dataZoom（`StockTechnicalChart` 的 `maxBars`/`enableZoom`）；默认只显示 K 线 + 成交量，开启「技术指标」开关后才显示 MA/EXPMA/MACD/KDJ/RSI（指标仍按完整历史计算）；TrendsPage 仍用完整历史 + 缩放并默认显示全部指标。
- 详情图高度按窗口自适应（`computeChartHeight`，弹窗 95vh），显示全部指标时 5 个子图一屏展示、不滚动；隐藏指标时只保留 K 线 + 成交量两个子图。`StockTechnicalChart` 的 `height` 参数会按比例重排子图与标题位置。
- 缩略图数据来自 `GET /api/public/reports/{token}/thumbnails?symbols=...`（`services/report_thumbnails.py`）：每页一次批量请求，最多 60 个代码、每标的最近 30 根日线，前复权（`close_adj/close`），只返回报表内的标的。
- K 线前复权在 `analytics/indicators.py` 统一计算：`adj_factor`（BaoStock backAdjustFactor，后复权因子）应单调不减；历史归档与增量数据混写导致因子回退时自动退回原始价格并记录 warning，避免出现单日 -58% 的假跌。排查这类问题时先看该标的 `daily_bars.adj_factor` 是否一致。
- 图表 tooltip 使用 `StockTechnicalChart` 内的聚合 formatter（日期 / 开高低收 / 成交量 / MA / EXPMA / MACD / KDJ / RSI 各一行）并设置 `confine: true`，避免弹窗顶部被裁切。
- 容器内依赖 `fonts-noto-cjk` 生成中文 PDF（matplotlib 已不用于 PDF，依赖暂时保留）；不要移除字体。

## 同步与数据源

- 不使用 Tushare Pro；数据源为腾讯、BaoStock、AkShare（含北交所、日历、财务、股东）。
- 腾讯负责快速价格/增量，BaoStock 负责沪深历史与完整字段，AkShare 补充北交所等；BaoStock 可能限流/登录失败。
- 2017 年及以前数据已做 Tencent + Sina 多源核对并固化在月度归档；2018 年之后仍有部分标的稀疏，不要假设全量完整。
- 先导入已有 SQLite 归档，再考虑重新下载；不要重复抓取已有数据。
- 回补代理在系统设置 `sync.proxy_url` 配置；腾讯/AkShare 通过 requests 环境变量生效，该设置非空时会覆盖进程内的 `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`（BaoStock 不受影响）。BaoStock 原始 TCP 需要环境变量 `BAOSTOCK_SOCKS_PROXY=socks5h://host:port`（只认环境变量，界面设置无效），由 PySocks 定向走 SOCKS5；容器内访问宿主机代理必须用 `host.docker.internal`（依赖 compose 的 `extra_hosts: host-gateway`），宿主机代理须监听 0.0.0.0 或局域网地址，容器内不要写 `127.0.0.1`（那是容器自身）。BaoStock 匿名会话不能并发：worker 回补期间不要再起 CLI 或第二个进程登录，否则旧会话被顶掉，查询返回 `10001001 用户未登录`；同一进程内已用进程锁把登录-查询-登出串行化。匿名登录握手较慢（10–40 秒），客户端登录超时 90 秒并重试一次。日报缺失日线走 Tencent，避免依赖 BaoStock。
- worker 使用 PostgreSQL advisory lock 和 `sync_requests` 队列（`FOR UPDATE SKIP LOCKED`）。重启 worker 前先检查是否有 running/queued 请求。
- worker 日志：`worker_logs` 表（`models/worker_log.py`）由 `services/worker_logs.py` 的 `WorkerLogHandler` 异步落库（有界队列 + 1s/100 条批量写，启动只保留 20000 行 / 7 天，超长消息截断 4000 字符）；只捕获 `app.*` 的 INFO+ 和其它 logger 的 WARNING+，并通过 ContextVar `CURRENT_SYNC_REQUEST` 给手动同步请求打上 `request_id`。handler 只在 `worker.main()` 安装（pytest 不启动线程）。
- 日志查询 API：`GET /api/admin/logs`（admin）支持 `level` / `request_id` / `q` / `after_id`（增量轮询）/ `before_id`（向前翻页）/ `limit≤500`，返回按 id 升序的 `{items, has_more}`；参数非法或 after/before 同传返回 422。前端在 `/admin/data`：活动请求卡片内「数据更新日志」每 2s 增量 tail，底部「Worker 日志」卡片提供级别/关键词/请求 ID 筛选、自动刷新与「加载更早」。
- 数据修复：`python -m app.cli repair-adj-factors [--symbol 600299.SH ...] [--asset-type stock|etf|all] [--limit N] [--apply]`（默认 dry-run）扫描 `daily_bars.adj_factor`，把回退/缺失的因子按前向最大值补齐并重算 `close_adj`；K 线与指标是实时计算，修复后刷新网页即可，旧 PDF 缓存里的指标表不会自动变化。
- LLM 对话（个人号通道）：系统设置「LLM 对话」组（`llm.*`，默认 `llm.enabled=false`）接入 OpenAI 兼容的 `{base_url}/chat/completions`（`llm.base_url` / `llm.api_key` / `llm.model` / `llm.system_prompt` / `llm.temperature` / `llm.max_tokens` / `llm.timeout_seconds`）。群内 @ 机器人发送「开启聊天模式」写入 Redis 键 `qsa:qq:chatmode:group:{group_id}`（JSON：mode/enabled_by/enabled_at/group_name，TTL=`llm.chat_mode_ttl_seconds`）；开启后该群的普通消息（无需 @）与 @ 机器人的消息（先匹配选择指令）都交给模型，模型可用 `#SILENT#` 表示不需要回应，会话层此时不发送任何消息；个人号私聊直接进入模型对话。「关闭聊天模式」删除该键并清空上下文。服务层：`services/qq_chat_mode.py`（开关状态读写/列表）、`services/llm_chat.py`（请求、工具循环、Redis 历史 `qsa:qq:llm:history:*`、失败记录 `qsa:llm:last_error`）、`services/llm_tools.py`（只读工具：search_instruments / get_instrument_profile / get_price_history / screen_instruments / list_screening_factors / web_search）、`services/web_search.py`（调用外部 SearXNG 实例，地址在 `llm.web_search_base_url`，实例需在 `settings.yml` 的 `search.formats` 启用 json，异常降级为错误文本）。会话层接线在 `qq_bot/conversation.py`：指令优先于选股对话，个人号私聊不再走选股状态机而直接进模型。查询结果只读、不产生报表；对话另有独立限流（`llm.chat_per_minute` / `llm.chat_per_day`）。管理端 `GET /api/admin/personal-qq/status` 返回 LLM 配置状态与当前聊天模式的群列表，`DELETE /api/admin/personal-qq/chat-mode?group_id=` 可强制关闭某个群。
- 个人号 QQ 机器人（OneBot v11）：与官方 qq-botpy 通道完全独立、默认关闭（`personal_qq.enabled=false`），通过第三方协议端（SnowLuma / NapCat 等）的 OneBot v11 正向 WebSocket 接收事件、HTTP 接口发送消息。该链路不是官方通道，存在账号被风控或封禁的风险，只在明确需要时开启。配置项在系统设置「个人号机器人」组：`personal_qq.ws_url`（如 `ws://host.docker.internal:3001`）、`personal_qq.http_url`（如 `http://host.docker.internal:3000`）、`personal_qq.access_token`（协议端未鉴权时留空）、`personal_qq.self_qq`（机器人自身 QQ 号，用于过滤自己消息和识别群 @）、`personal_qq.allow_private` / `personal_qq.allow_groups`（英文逗号分隔的白名单，留空表示忽略对应类型消息）。群内需 @ 机器人并回复「订阅」才会写入 `personal_qq_groups` 并在每日 16:00 选股完成后收到纯文本推送（含报表链接，个人号通道不发送 PDF 附件）；服务只允许单实例 leader（Redis 锁），协议端地址或 Token 变更会自动重连。管理接口为 `/api/admin/personal-qq/{status,groups,test}`。
- API 与 worker 都设计为单副本：QQ 机器人 WebSocket、APScheduler 和队列消费者不能随意横向扩容。

## 前端约定

- 界面文案使用中文；不要添加“数据仅供研究，不构成投资建议”这类模板免责声明。
- 当前产品要求界面不出现“股票 / A 股 / A-share”作为类型特征：使用“标的”“条件筛选”“名称 / 代码”等中性文案。新增文案不要回退这些词。
- 涨跌颜色遵循涨红跌绿。
- API 错误通过 `ApiError` 展示；不要吞掉后端返回的 message。
- 页面标题、表头、按钮文案改动要实际浏览器验证。
- `api.ts` 是唯一 HTTP 客户端入口；类型放 `types.ts`，不要在页面里重复定义接口。
- 前端开发代理目标由 `VITE_PROXY_TARGET` 控制，默认 `http://127.0.0.1:8000`；Docker 开发栈使用 `http://backend:8000`。

## Docker

- 所有运行时数据使用宿主机 bind mount，不使用 named volumes：
  - 生产 PostgreSQL：`./data/postgres`
  - 生产 Redis：`./data/redis`
  - 开发 PostgreSQL/Redis：`./data/dev/postgres`、`./data/dev/redis`
- 生产部署只使用 `docker-compose.yml`，应用镜像直接来自 `ghcr.io/josark2005/qsa-backend` 和 `ghcr.io/josark2005/qsa-frontend`，标签由 `QSA_IMAGE_TAG` 控制。
- 生产：只有 frontend 发布宿主机端口（默认 `8080`）；backend/worker/postgres/redis 只走内部网络。
- 开发：发布 `5173`（前端）、`8000`（后端）、`5432`（PostgreSQL）、`6379`（Redis），默认 `DEV_BIND_IP=0.0.0.0`。
- 开发前端只挂载 `src`、`public`、`index.html` 和配置；`node_modules` 保留在镜像内。修改依赖后需要重新 build 开发镜像。
- 生产 `.env` 通过 `env_file` 注入 backend/worker；完整开发栈把 `.env.dev` 通过 `env_file`（`required: false`）注入。两侧 backend/worker 都声明 `extra_hosts: ["host.docker.internal:host-gateway"]`，容器内访问宿主机代理用 `host.docker.internal`；改环境变量后要 `up -d backend worker` 重建容器，`restart` 不会重新读取 env。
- 生产初始化顺序：`db-restore` → `migrate` → `backend`/`worker` → `frontend`。
- `migrate` 负责迁移、首次历史导入和管理员创建；生产设置 `HISTORY_AUTO_IMPORT=false`，避免启动时重复扫描归档。
- 完整 dump 含密码哈希、QQ 密钥和系统设置，`data/bootstrap/` 已 gitignore，不要提交或外传。
- 不要删除 `data/postgres`、`data/redis`、`data/dev` 或 `data/bootstrap`；它们等同于数据库和部署数据。
- 生产环境启用 HTTPS 后把 `SESSION_COOKIE_SECURE=true`，并更新 `general.public_base_url` 与 `QQ_BOT_PUBLIC_BASE_URL`。

## CI/CD

- 工作流：`.github/workflows/ci.yml`。
- PR：后端测试、前端 lint/build、compose 配置校验、镜像构建（不推送）。
- main 或 `v*` tag：推送 `ghcr.io/josark2005/qsa-backend` 和 `ghcr.io/josark2005/qsa-frontend` 镜像，标签为完整 commit SHA 与 `latest`。
- Actions 不负责部署；部署方在目标机器执行 `docker compose pull` 和 `docker compose up -d`。
- 部署机需要 `.env`、`data/history` 和首次使用的 `data/bootstrap/qsa-full.dump`，`data` 目录不纳入 git。

## 常见坑

- 前端 Vite HMR 偶尔不会应用快速连续修改：先刷新/touch 文件，仍无效再重启 dev server。
- 修改后端因子后，运行中的 API 可能仍持有旧注册表；重启 API 再保存默认条件。
- QQ 机器人当前使用官方 qq-botpy SDK 的 WebSocket 出站连接；群聊需要 @ 机器人并订阅后才主动推送。个人号通道（OneBot v11）是另一套独立链路，默认关闭，不要把它和官方机器人凭据混用。
- 报表 PDF 首次生成较慢，nginx 已配置较长代理超时。
- 不要直接删除或重建 `data/history`、`data/reports`、`data/bootstrap`；先备份。
