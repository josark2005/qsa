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
- `backend/app/qq_bot/`：QQ WebSocket、会话式选股、群订阅。
- `backend/migrations/versions/`：Alembic 迁移；以 `alembic heads` 为准，当前为 `b2d4e6f8a0c1`。
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
- 容器内依赖 `fonts-noto-cjk` 生成中文 PDF 与图表；不要移除。

## 同步与数据源

- 不使用 Tushare Pro；数据源为腾讯、BaoStock、AkShare（含北交所、日历、财务、股东）。
- 腾讯负责快速价格/增量，BaoStock 负责沪深历史与完整字段，AkShare 补充北交所等；BaoStock 可能限流/登录失败。
- 2017 年及以前数据已做 Tencent + Sina 多源核对并固化在月度归档；2018 年之后仍有部分标的稀疏，不要假设全量完整。
- 先导入已有 SQLite 归档，再考虑重新下载；不要重复抓取已有数据。
- 回补代理在系统设置 `sync.proxy_url` 配置；腾讯/AkShare 通过 requests 环境变量生效。BaoStock 原始 TCP 需要 `BAOSTOCK_SOCKS_PROXY=socks5h://host:port`，由 PySocks 定向走 SOCKS5；日报缺失日线走 Tencent，避免依赖 BaoStock。
- worker 使用 PostgreSQL advisory lock 和 `sync_requests` 队列（`FOR UPDATE SKIP LOCKED`）。重启 worker 前先检查是否有 running/queued 请求。
- API 与 worker 都设计为单副本：QQ WebSocket、APScheduler 和队列消费者不能随意横向扩容。

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
- QQ 机器人当前以 WebSocket 出站连接为主；群聊需要 @ 机器人并订阅后才主动推送。
- 报表 PDF 首次生成较慢，nginx 已配置较长代理超时。
- 不要直接删除或重建 `data/history`、`data/reports`、`data/bootstrap`；先备份。
