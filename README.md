# QSA

QSA 是一个前后端分离的条件筛选系统：FastAPI + PostgreSQL + Redis 后端，React/Vite + shadcn 前端，包含同步 worker、QQ 机器人、PDF 报表和历史归档。

## 功能

- 条件筛选：股东户数、财报指标、N 日涨跌停、首板涨停量比、窗口低位、区间振幅等。
- 筛选历史：报表快照永久保留，支持回看、复制公开链接、下载 PDF、按原条件重新筛选。
- 公开报表：只读 token 链接，包含 K 线、MACD、KDJ、RSI、EXPMA 等日/周/月图表。
- PDF 报表：汇总表 + 最多 50 只标的详情页；超过 50 只只输出汇总。
- QQ 机器人：私聊/群聊会话式选股、条件确认、报表链接与 PDF、群订阅和每日推送。
- 数据同步：腾讯、BaoStock、AkShare 多源行情，财报、股东户数、交易日历、涨跌停事件。
- 历史数据：1990 年至今的月度 SQLite 归档，启动时自动导入 PostgreSQL。
- 管理能力：用户管理、系统设置热更新、审计日志、手动回补、全量回补、每日定时任务。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12、uv、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、psycopg3 |
| 任务 | APScheduler、同步 worker、PostgreSQL advisory lock |
| 数据 | PostgreSQL 18.6、Redis 7 |
| 前端 | React 19、TypeScript、Vite、Tailwind CSS v4、shadcn/radix-ui、TanStack Query、ECharts |
| 报表 | ReportLab、matplotlib |
| 部署 | Docker Compose、GHCR、GitHub Actions |

## 架构

```text
浏览器 / QQ
    │
    ▼
frontend (nginx)
    ├── 静态页面、/reports/:token
    └── /api/* 反向代理
            │
            ▼
        backend (FastAPI) ──► PostgreSQL
            │                  Redis
            ├── QQ WebSocket（出站）
            ├── PDF → data/reports
            └── 历史归档 → data/history
            ▲
            │
        worker（定时任务 + 同步队列）
```

## 目录

```text
backend/
  app/api/          HTTP 路由
  app/services/     报表、用户、PDF、历史归档等业务逻辑
  app/sync/         行情、财报、股东、涨跌停、pipeline
  app/screening/    因子注册表与 SQL 构建
  app/qq_bot/       QQ WebSocket 与会话式选股
  migrations/       Alembic 迁移
  tests/            pytest
frontend/
  src/pages/        页面
  src/components/   布局与业务组件
  src/lib/          API 客户端与类型
data/
  history/          月度 SQLite 归档
  bootstrap/        部署用完整数据库 dump
  reports/          PDF 缓存
docker-compose.yml      直接拉取 GHCR 镜像的部署栈
docker-compose.dev.yml  完整开发栈
```

## Docker 部署（GHCR）

要求：Docker 24+、Docker Compose v2。

```bash
cp .env.example .env
# 编辑 POSTGRES_PASSWORD、CORS_ORIGINS、QQ_BOT_PUBLIC_BASE_URL 等
```

`.env` 至少包含：

```ini
POSTGRES_PASSWORD=强密码
QSA_IMAGE_TAG=latest
HTTP_PORT=8080
CORS_ORIGINS=http://服务器地址:8080
QQ_BOT_PUBLIC_BASE_URL=http://服务器地址:8080
SESSION_COOKIE_SECURE=false
```

Compose 会自动读取 `.env`，直接启动：

```bash
# 私有 GHCR 包需要先登录
docker login ghcr.io

docker compose pull
docker compose up -d
docker compose logs -f migrate
```

镜像来自：

```text
ghcr.io/josark2005/qsa-backend:latest
ghcr.io/josark2005/qsa-frontend:latest
```

访问 `http://服务器:8080/`。只有前端端口对外，PostgreSQL、Redis、backend、worker 只在内部网络。

固定版本时把 `QSA_IMAGE_TAG` 改成具体 commit SHA。

### 预置数据

将完整数据库备份放到：

```text
data/bootstrap/qsa-full.dump
```

`db-restore` 仅在数据库尚未初始化时恢复它。备份必须是 `pg_dump -Fc` 生成的 custom format。

月度归档放在：

```text
data/history/*.db
```

`data/postgres` 是 PostgreSQL 数据目录，首次启动前保持为空，不要手动放 dump。

没有完整 dump 时会执行全新安装：导入 `data/history`，并使用 `.env` 中的 `QSA_ADMIN_USERNAME` / `QSA_ADMIN_PASSWORD` 创建管理员。

## 本地开发

### 基础服务

```bash
cp .env.dev.example .env
docker compose -f docker-compose.dev.yml up -d postgres redis
```

### 后端

```bash
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

worker：

```bash
cd backend
uv run python -m app.worker
```

### 前端

```bash
cd frontend
pnpm install
pnpm dev
```

前端默认把 `/api` 代理到 `http://127.0.0.1:8000`，可通过 `VITE_PROXY_TARGET` 修改。

### 完整开发栈

```bash
cp .env.dev.example .env.dev
docker compose --env-file .env.dev -f docker-compose.dev.yml up --build
```

开发端口：`5173` 前端、`8000` 后端、`5432` PostgreSQL、`6379` Redis。

## 测试

```bash
cd backend
uv run pytest -q

cd frontend
pnpm lint
pnpm build
```

## 主要环境变量

| 变量 | 说明 |
|---|---|
| `POSTGRES_*` | 数据库名、用户、密码 |
| `DATABASE_URL` | 后端数据库连接，Compose 中自动拼接 |
| `REDIS_URL` | Redis 连接 |
| `QSA_IMAGE_TAG` | GHCR 镜像标签，默认 `latest` |
| `HTTP_PORT` | 前端发布端口，默认 `8080` |
| `CORS_ORIGINS` | 允许的前端来源 |
| `SESSION_COOKIE_SECURE` | HTTPS 环境设为 `true` |
| `HISTORY_DIR` | 月度归档目录，容器内为 `/app/data/history` |
| `QSA_REPORT_PDF_DIR` | PDF 目录，容器内为 `/app/data/reports` |
| `QQ_BOT_APP_ID` / `QQ_BOT_APP_SECRET` | QQ 机器人凭据 |
| `QQ_BOT_PUBLIC_BASE_URL` | 报表链接公网前缀 |
| `QSA_ADMIN_USERNAME` / `QSA_ADMIN_PASSWORD` | 空库时创建初始管理员 |

更多系统设置可在管理端「系统设置」中热更新。

## CI/CD

工作流：`.github/workflows/ci.yml`

- PR：后端测试、前端 lint/build、compose 校验、镜像构建（不推送）。
- `main` 或 `v*` tag：推送 backend/frontend 镜像到 GHCR，标签为 commit SHA 与 `latest`。
- Actions 不负责部署；部署方在目标机器执行 `docker compose pull` 和 `docker compose up -d`。

## 运维注意

- 生产只暴露前端端口，建议在 frontend 或宿主机反向代理上启用 HTTPS。
- HTTPS 下设置 `SESSION_COOKIE_SECURE=true`，并更新 `general.public_base_url`。
- `data/postgres`、`data/redis`、`data/history`、`data/bootstrap` 等同于生产数据，不要随意删除。
- `data/bootstrap/`、`.env`、`.env.dev`、`*.db` 已被 gitignore，不要提交 dump 或密钥。
- API 与 worker 固定单副本：QQ WebSocket、APScheduler 和队列消费者不支持横向扩容。
- 修改 PDF 模板后提升 `PDF_CACHE_VERSION`，避免继续返回旧缓存。

## 相关文档

- [AGENTS.md](AGENTS.md)：编码代理约束、验证命令和常见坑
- [.env.example](.env.example)：部署环境变量模板
- [.env.dev.example](.env.dev.example)：开发环境变量模板
- [docker-compose.yml](docker-compose.yml)：GHCR 部署编排
- [docker-compose.dev.yml](docker-compose.dev.yml)：开发编排
