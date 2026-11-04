# QSA

QSA 是一个前后端分离的条件筛选系统：FastAPI + PostgreSQL + Redis 后端，React/Vite + shadcn 前端，包含同步 worker、QQ 机器人、PDF 报表和历史归档。

## 功能

- 条件筛选：股东户数、财报指标、N 日涨跌停、首板涨停量比、窗口低位、区间振幅等。
- 筛选历史：报表快照永久保留，支持回看、复制公开链接、下载 PDF、按原条件重新筛选。
- 公开报表：只读 token 链接，包含 K 线、MACD、KDJ、RSI、EXPMA 等日/周/月图表。
- PDF 报表：汇总表 + 最多 50 只标的详情页；超过 50 只只输出汇总。
- QQ 机器人：私聊/群聊会话式选股、条件确认、报表链接与 PDF、群订阅和每日推送。
- 个人号 QQ 机器人（可选，默认关闭）：通过 OneBot v11 协议端（SnowLuma / NapCat）接入个人号，支持白名单私聊、群 @ 订阅与每日推送（纯文本 + 报表链接）；非官方链路，存在账号风险。
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
            ├── QQ WebSocket（官方 qq-botpy SDK，出站）
            ├── 个人号 OneBot WebSocket（可选，默认关闭）
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
  app/qq_bot/       QQ 机器人（官方 qq-botpy SDK）与会话式选股；onebot/ 为个人号通道
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

仅源码运行后端/worker 时，`.env` 由应用读取配置，但不会自动导出到进程环境变量；需要代理时先执行
`set -a; . ./.env; set +a` 再启动，否则 `HTTP_PROXY` / `BAOSTOCK_SOCKS_PROXY` 不生效。

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

`.env.dev` 会通过 compose 的 `env_file` 注入 backend/worker（显式 `environment` 仍优先）；
代理等变量写在这里即可。修改后需要重建容器才会重新注入：

```bash
docker compose --env-file .env.dev -f docker-compose.dev.yml up -d backend worker
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
| `PERSONAL_QQ_*` | 个人号 OneBot 地址/Token 的环境变量兜底（系统设置优先，默认关闭） |
| `QSA_ADMIN_USERNAME` / `QSA_ADMIN_PASSWORD` | 空库时创建初始管理员 |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | 腾讯/AkShare 出站代理（requests），见「网络代理」 |
| `BAOSTOCK_SOCKS_PROXY` | BaoStock 的 SOCKS5 代理，如 `socks5h://host.docker.internal:7893` |

更多系统设置可在管理端「系统设置」中热更新。

### 个人号 QQ（OneBot v11，可选）

个人号通道与官方 QQ 机器人互相独立，默认关闭。在系统设置「个人号机器人」组填写 OneBot v11 协议端
（SnowLuma、NapCat 等）的正向 WebSocket 地址、HTTP 地址和 Access Token，并填写机器人自身 QQ 号
（`self_qq`）与私聊/群白名单；群内 @ 机器人并回复「订阅」后，每日选股完成会推送纯文本与报表链接
（个人号通道不发送 PDF 附件）。该链路不是官方通道，存在账号被风控或封禁的风险，按需开启。

### 网络代理

容器内访问宿主机上的代理必须使用 `host.docker.internal`：两个 compose 文件都已为 backend/worker 声明
`extra_hosts: ["host.docker.internal:host-gateway"]`（Linux 上 Docker 不会自动解析该域名）。
宿主机代理需要监听 `0.0.0.0` 或局域网地址（例如 Clash 的 allow-lan）；只监听 `127.0.0.1` 时代理连不上。
容器里的 `127.0.0.1` 指的是容器自身，不是宿主机。

腾讯 / AkShare 走 requests，通过标准代理变量生效（生产写 `.env`，完整开发栈写 `.env.dev`）：

```ini
HTTP_PROXY=http://host.docker.internal:7890
HTTPS_PROXY=http://host.docker.internal:7890
NO_PROXY=localhost,127.0.0.1,postgres,redis,backend
```

BaoStock 使用原始 TCP（端口 10030），`HTTP_PROXY` / `HTTPS_PROXY` 对它无效，必须单独配置 SOCKS5：

```ini
BAOSTOCK_SOCKS_PROXY=socks5h://host.docker.internal:7893
```

`socks5h://` 表示域名由代理端解析，`socks5://` 表示本地解析；不写协议时按 `socks5h://` 处理。

BaoStock 的匿名登录握手较慢（实测 10–40 秒，容器或服务端限流时更久），客户端登录超时为 90 秒并会自动重试一次；
日志出现 `10002007 网络接收错误` 时，先看同一条登录日志打印的传输方式（`direct TCP` 或 `SOCKS5 host:port`）与耗时，
再判断是出站网络受限还是代理配置问题。

环境变量只在创建容器时注入，改完 `.env` / `.env.dev` 后需要重建容器：

```bash
# 生产
docker compose up -d backend worker

# 完整开发栈
docker compose --env-file .env.dev -f docker-compose.dev.yml up -d backend worker
```

容器内自检 BaoStock 连通性：

```bash
docker compose -f docker-compose.dev.yml --env-file .env.dev exec worker python - <<'PY'
from app.datasources.baostock import BaoStockClient
with BaoStockClient() as client:
    print(client.fetch_trade_dates("2026-01-01", "2026-01-10").head())
PY
```

管理端「系统设置 → 数据同步 → 网络代理」（`sync.proxy_url`）只影响腾讯/AkShare，并且一旦非空就会覆盖
进程内的 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`（清空或重启后恢复环境变量）；BaoStock 不受它影响。
用环境变量配置代理时请把界面里这一项留空，或填同样的 `http://host.docker.internal:7890`；
不要填 `127.0.0.1`，那会把请求指向容器自身。

BaoStock 匿名会话不支持并发：worker 正在回补时，不要再用 CLI 或另一个进程登录 BaoStock，
第二次登录会把前一个会话顶掉，之后的查询会返回 `10001001 用户未登录`。
日报的缺失日线已改为优先使用 Tencent；BaoStock 仍用于全量回补和历史因子核对。

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
- API 与 worker 固定单副本：QQ 机器人 WebSocket、APScheduler 和队列消费者不支持横向扩容。
- 修改 PDF 模板后提升 `PDF_CACHE_VERSION`，避免继续返回旧缓存。

## 相关文档

- [AGENTS.md](AGENTS.md)：编码代理约束、验证命令和常见坑
- [.env.example](.env.example)：部署环境变量模板
- [.env.dev.example](.env.dev.example)：开发环境变量模板
- [docker-compose.yml](docker-compose.yml)：GHCR 部署编排
- [docker-compose.dev.yml](docker-compose.dev.yml)：开发编排
