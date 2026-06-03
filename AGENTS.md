# 电商数据分析系统 — Agent 规则手册

## 项目概览

电商订单数据分析系统，基于 10 万+ 真实订单，提供 BI 看板、AI 分析、RFM 用户画像、RESTful API。

**技术栈**：Python 3.12, FastAPI, SQLAlchemy (async), Streamlit, Plotly, LangChain, MySQL 8, Redis 7, Docker Compose, Nginx

**虚拟环境**：`.venv\Scripts\`

## 启动服务

### 本地开发（直连模式，三端口）

```powershell
# 1. FastAPI 后端 (:8000)
Start-Process -WindowStyle Hidden -FilePath ".venv\Scripts\python.exe" -ArgumentList "-m uvicorn backend.main:app --host 0.0.0.0 --port 8000"
# 2. BI 看板 (:8501)
Start-Process -WindowStyle Hidden -FilePath ".venv\Scripts\streamlit.exe" -ArgumentList "run streamlit_app.py --server.port 8501"
# 3. AI 助手 (:8505)
Start-Process -WindowStyle Hidden -FilePath ".venv\Scripts\streamlit.exe" -ArgumentList "run ai-ecommerce-assistant/app.py --server.port 8505"
```

### Docker 部署（Nginx 统一入口 :80）

```powershell
# 启动全部服务
docker-compose up -d
# 停止
docker-compose down
# 重建某服务（代码改动后）
docker-compose up -d --build backend
# 重载 Nginx 配置
docker exec ea-nginx nginx -s reload
```

**Nginx 反向代理路由表：**

| 路径 | 后端 | 说明 |
|------|------|------|
| `/` | backend:8000 | 统一导航页（默认入口） |
| `/BI` | streamlit:8501 | BI 看板 |
| `/ai/` | ai-assistant:8502 | AI 助手（baseUrlPath=/ai） |
| `/api/*` | backend:8000 | RESTful API |
| `/nav` | backend:8000 | 导航页（备用路由） |
| `/docs` `/redoc` `/openapi.json` | backend:8000 | Swagger / ReDoc 文档 |
| `/health` `/demo` `/monitor` `/health-panel` | backend:8000 | 系统页面 |

主数据文件：`data/cleaned_orders.csv`

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
- **`Start-Process` 必须用 `-WindowStyle Hidden`** — Windows 后台运行
- **RFM 评分语义**：R 评分 ≤ 阈值 = 高价值（近期活跃），F/M 评分 ≥ 阈值 = 高价值
- **`aggregate(lambda x: x.mode().iloc[0])`** — 分组取众数前需检查 `len(x.mode()) > 0`
- **LLM SQL 必须只读** — `ai_service._is_read_only_sql()` 拦截 DROP/DELETE/UPDATE/INSERT/ALTER/TRUNCATE/CREATE
- **`@cached` 防击穿** — 基于 `asyncio.Lock` 的 double-check lock，过期时异步重建
- **内存缓存上限 1000 条** — 满时先清理过期条目再驱逐最旧条目；`cleanup_memory_cache()` 同步清理 `_cache_locks`
- **公开监控端点** — `/api/monitor/services-status`、`/health/detailed`、`/metrics` 无需 JWT（导航页/监控面板/健康面板调用）

## 排查路径

- Streamlit 报错 → 看终端输出 / 检查 `columns()` 和 `reindex` 参数
- FastAPI 端口占用 → `netstat -ano | Select-String ":8000"` 找 PID 后 `Stop-Process`
- 数据文件 → `data/cleaned_orders.csv`
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

## 测试

```powershell
.venv\Scripts\python.exe -m pytest backend/tests/ -v
```

## 深入文档

- `README.md` — 完整功能说明、API 列表、部署方式
