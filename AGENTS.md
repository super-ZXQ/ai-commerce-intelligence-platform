# AI Commerce Intelligence Platform — Agent 规则手册

## 项目概览

AI 商业智能分析平台，基于 10 万+ 公开电商订单样本，提供 BI 看板、AI 分析、RFM 用户画像、RESTful API。

**技术栈**：Python 3.12, FastAPI, SQLAlchemy (async), Streamlit, Plotly, LangChain, MySQL 8, Redis 7, Docker Compose, Nginx

**虚拟环境**：`.venv/bin/`（Windows 为 `.venv\Scripts\`）

## 启动服务

### 本地开发（直连模式，三端口，macOS/Linux）

```bash
# 1. FastAPI 后端 (:8000)
.venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 &
# 2. BI 看板 (:8501)
.venv/bin/streamlit run streamlit_app.py --server.port 8501 &
# 3. AI 助手 (:8505)
.venv/bin/streamlit run ai-ecommerce-assistant/app.py --server.port 8505 &
```

Windows 对应命令：`Start-Process -WindowStyle Hidden -FilePath ".venv\Scripts\python.exe" ...`。

### Docker 部署（Nginx 统一入口 :80）

```bash
# 启动全部服务
docker compose up -d
# 停止
docker compose down
# 重建某服务（代码改动后）
docker compose up -d --build backend
docker compose up -d --build streamlit
# 重载 Nginx 配置
docker exec ea-nginx nginx -s reload
```

**Nginx 反向代理路由表：**

| 路径 | 后端 | 说明 |
|------|------|------|
| `/` | backend:8000 | 统一导航页（默认入口） |
| `/BI/` | streamlit:8501 | BI 看板（baseUrlPath=/BI，访问 `/BI` 会 301 跳转到 `/BI/`） |
| `/ai/` | ai-assistant:8502 | AI 助手（baseUrlPath=/ai） |
| `/api/*` | backend:8000 | RESTful API |
| `/nav` | backend:8000 | 导航页（备用路由） |
| `/docs` `/redoc` `/openapi.json` | backend:8000 | Swagger / ReDoc 文档 |
| `/health` `/livez` `/readyz` `/metrics` | backend:8000 | 公开健康与聚合指标（`/readyz` 含 Redis 检查） |
| `/demo` `/monitor` `/health-panel` | backend:8000 | 页面；`/monitor`、`/health-panel` 需 `admin:manage` |

初始化快照（只读）：`data/cleaned_orders.csv`。运行期以 MySQL 为准；新增/修正订单走导入接口，禁止替换 CSV 后重启生效。

## 页面结构

`streamlit_app.py` 包含两个页面（`st.sidebar.selectbox` 切换）：
- **📊 销售总览** — 指标卡 + 趋势/平台/TOP10 图表
- **👥 RFM 客户分层** — 三 Tab 布局

RFM 计算在 `streamlit_app.py` 内联 `compute_rfm_data()`，缓存通过用户名/订单数/金额哈希。

## 企业级配色（所有图表必须统一映射）

```python
ENTERPRISE_COLORS_FULL = {
    "重要价值客户": "#1565C0",
    "重要发展客户": "#1E88E5",
    "重要保持客户": "#1E88E5",
    "重要挽留客户": "#FF5722",
    "一般价值客户": "#43A047",
    "一般发展客户": "#43A047",
    "一般保持客户": "#43A047",
    "一般挽留客户": "#7E57C2",
}
```

## 硬边界规则

- **禁止使用 `st.metric(delta_delta_color=...)`** — 当前 Streamlit 版本不支持此参数
- **禁止 `reindex(..., fill_value=0)` 对含字符串列的 DataFrame** — 字符串列与 `fill_value=0` 类型冲突，必须分列处理：数值列 `fillna(0)`，字符串列 `fillna('其他')`
- **`go.Pie` 使用 `labels=` 而非 `names=`** — `names` 可能被 Plotly 解析为属性路径
- **Windows 后台运行 `Start-Process` 必须用 `-WindowStyle Hidden`**；macOS/Linux 用 `&` 或 `nohup`
- **RFM 评分语义**：R 评分 ≤ 阈值 = 高价值（近期活跃），F/M 评分 ≥ 阈值 = 高价值
- **`aggregate(lambda x: x.mode().iloc[0])`** — 分组取众数前需检查 `len(x.mode()) > 0`
- **LLM SQL 必须只读** — AST 层 `agent_core/sql_safety.py` + 字符串层 `backend/utils/sql_guard.is_read_only_sql()` + 只读数据库账号三层拦截；LIMIT 一律强制收敛到 500 行，执行层再用 `yield_per` 流式读取
- **`@cached` 防击穿** — 基于 `asyncio.Lock` 的 double-check lock，过期时异步重建
- **内存缓存上限 1000 条** — 满时先清理过期条目再驱逐最旧条目；`cleanup_memory_cache()` 同步清理 `_cache_locks`
- **监控端点权限** — 仅 `/livez`、`/readyz`、`/health`、`/metrics` 公开；`/api/monitor/*` 与 `/health/detailed` 需要 `admin:manage`，异常详情只写服务端日志、不回显给调用方
- **登出必须吊销 Token** — `/api/auth/logout` 会递增 `token_version`，只删 Cookie 不足以让已签发的 Bearer Token 失效
- **生产环境 AI 只读账号** — `DEBUG=false` 时必须配置 `AI_DB_USER`/`AI_DB_PASSWORD`，禁止回落主库 root/admin 账号
- **默认单 worker** — `backend/main.py` 启动守卫检测 `WEB_CONCURRENCY` / `GUNICORN_WORKERS` / `UVICORN_WORKERS` / `--workers`；大于 1 且未设置 `ALLOW_MULTI_WORKER=true` 时拒绝启动（进程内限流与 AI 并发会按 worker 放大）
- **登录失败锁定** — 按「账号+客户端 IP」计数，5 次/5 分钟触发 5 分钟锁定，成功登录清零；禁止改成全局账号锁定（可被用来锁死 admin）
- **时间口径** — 业务列（`order_time`/`order_date`/`payment_time`/`order_hour`/`weekday`）是 Asia/Shanghai 墙钟；运维列（`created_at`/`updated_at`/`expires_at`/`occurred_at`）是 UTC-naive。MySQL 固定 `--default-time-zone=+00:00`，禁止把业务时间做 UTC 换算
- **请求追踪** — Nginx 生成 `$request_id` 并通过 `X-Request-ID` 传给 FastAPI；应用日志统一注入 request_id，AI 审计也复用它。`LOG_FORMAT=text|json` 控制人读/结构化日志
- **角色默认权限不写入 `scopes` 列** — 鉴权时 `effective_scopes()` 取「角色默认 ∪ 显式权限」，把角色默认值持久化会让后续收紧角色对已有账号失效（旧值被并集加回来）。`manage_auth create-user` 只在 `--scopes` 时存追加权限
- **用户名默认脱敏** — `viewer` 没有 `data:pii`，读取接口返回的 `user_name` 必须脱敏；analyst/admin 默认含 `data:pii`。RFM 明细引用缓存快照时必须复制后再脱敏，禁止原地修改缓存对象
- **公开探针必须边缘限流** — `/readyz` 每次占用业务库、事件库、认证库三个连接并 ping Redis；应用内限流把这些路径排除在桶外，因此必须由 Nginx `probe_edge` zone 兜底

## 排查路径

- Streamlit 报错 → 看终端输出 / 检查 `columns()` 和 `reindex` 参数
- FastAPI 端口占用 → macOS/Linux: `lsof -i :8000`；Windows: `netstat -ano | Select-String ":8000"` 找 PID 后 `Stop-Process`
- 初始化快照（只读）→ `data/cleaned_orders.csv`；运行期订单数据看 MySQL，勿直接替换 CSV
- Docker 启动卡住 → 重启 Docker Desktop / `wsl --shutdown`
- Docker 拉取失败 → 检查 `~/.docker/daemon.json` 镜像加速配置

## 开发规范

- **默认中文** — 所有回复、注释、文档优先使用中文
- **先分析再修改** — 改代码前先了解项目结构，不盲目改动
- **方案先行** — 新增功能前先给出架构方案，确认后再实现
- **保留已有功能** — 不删除用户已有的功能代码
- **中文注释** — 代码中添加中文注释说明用途
- **Async 优先** — FastAPI 接口统一使用 async/await
- **结构清晰** — 路由/服务/工具分层明确，保持项目可维护性
- **说明用途** — 生成代码时解释每段代码的用途

## 测试与静态检查

```bash
# 后端（需要本地 MySQL）
.venv/bin/python -m pytest backend/tests/ -v
# AI 助手
.venv/bin/python -m pytest ai-ecommerce-assistant/tests -q
# 静态检查（与 CI 完全一致；ruff.toml 已启用 E/F/B/S，notebook 由 extend-exclude 排除）
.venv/bin/ruff check .
```

## 深入文档

- `README.md` — 完整功能说明、API 列表、部署方式
