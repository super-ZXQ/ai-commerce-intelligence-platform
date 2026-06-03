# 电商数据分析系统

![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green?logo=fastapi)
![Streamlit](https://img.shields.io/badge/Streamlit-1.41-red?logo=streamlit)
![LangChain](https://img.shields.io/badge/LangChain-0.3-orange?logo=langchain)
![MySQL](https://img.shields.io/badge/MySQL-8.0-orange?logo=mysql)
![Redis](https://img.shields.io/badge/Redis-7-red?logo=redis)
![Docker](https://img.shields.io/badge/Docker--Compose-blue?logo=docker)
![License](https://img.shields.io/badge/License-MIT-green)

## 简介

基于 **10 万+ 条电商真实订单数据**，完成从数据清洗、特征工程到多维分析与可视化的完整链路。系统采用前后端分离架构，提供交互式 BI 看板、AI 智能查询、RESTful API 服务、RFM 用户画像和实时监控五大核心能力。

## 功能概览

| 模块 | 技术栈 | 功能 |
|------|--------|------|
| **BI 数据看板** | Streamlit + Plotly | 交互式数据大屏，多维度交叉筛选 |
| **AI 分析助手** | LangChain + DeepSeek V4 Flash | 自然语言提问，自动生成 SQL 并绘图 |
| **FastAPI 后端** | FastAPI + SQLAlchemy | 31 个 RESTful API，含认证/限流/缓存/监控/RFM |
| **RFM 用户画像** | SQLAlchemy + 量化分群 | R/F/M 五分位评分 → 8 类用户分群 + 流失预警 |
| **数据分析 Notebook** | Jupyter + Pandas | 数据清洗、销售/时间/用户多维分析 |

## 在线演示

| 应用 | 链接 | 说明 |
|------|------|------|
| **BI 数据看板** | [Streamlit Cloud](https://ecommerce-analysis-system-cqd8tpywoxneg8n3wqexfm.streamlit.app) | 在线部署 |
| **Docker 统一入口** | `http://localhost/` | 导航页（默认入口） |
| **BI 数据看板** | `http://localhost/BI` | 交互式数据大屏 |
| **AI 分析助手** | `http://localhost/ai/` | 自然语言查数 |
| **API 文档** | `http://localhost/docs` | Swagger UI |
| **API 体验页** | `http://localhost/demo` | 可视化大屏 + AI 查询 |
| **系统监控** | `http://localhost/monitor` | 实时监控面板 |
| **健康检查** | `http://localhost/health-panel` | 组件健康状态 |

## 快速开始

### Docker Compose 一键部署（推荐）

```bash
git clone https://github.com/2681107509-dev/ecommerce-analysis-system.git
cd ecommerce_analysis

cp deploy/.env.example .env
# 编辑 .env 填入 MySQL 密码、JWT Secret、LLM API Key

docker compose up -d --build
docker compose ps
```

**服务架构：**

```
docker compose up -d
├── ea-nginx        → Nginx 统一入口 (:80)
├── ea-streamlit    → BI 看板 (Docker 内网 :8501)
├── ea-backend      → FastAPI (Docker 内网 :8000)
├── ea-ai-assistant → AI 助手 (Docker 内网 :8502, baseUrlPath=/ai)
├── ea-mysql        → MySQL 8.0 (Docker 内网 :3306) + 自动建表+导入
└── ea-redis        → Redis 7 Alpine (Docker 内网 :6379) + AOF持久化 + LRU淘汰
```

**Nginx 路由分流：**

| 路径 | 转发服务 | 说明 |
|------|----------|------|
| `/` | `backend:8000` | 统一导航页（默认入口） |
| `/BI` | `streamlit:8501` | BI 数据看板 |
| `/api/*` | `backend:8000` | FastAPI REST API |
| `/docs` / `/redoc` / `/openapi.json` | `backend:8000` | API 文档 |
| `/demo` / `/monitor` / `/health-panel` | `backend:8000` | 后端演示、监控和健康页面 |
| `/ai/` | `ai-assistant:8502` | AI 分析助手 |

部署模式下仅 Nginx 对宿主机暴露 80 端口，业务服务、数据库和缓存只通过 Docker 内部网络通信。Nginx 已配置 `proxy_http_version 1.1`、`Upgrade` 和 `Connection` 头，支持 Streamlit WebSocket，避免反向代理后页面白屏。

### 本地开发

```bash
git clone https://github.com/2681107509-dev/ecommerce-analysis-system.git
cd ecommerce_analysis

python -m venv .venv && .venv\Scripts\activate

pip install -r requirements.txt
pip install -r ai-ecommerce-assistant/requirements.txt
pip install -r backend/requirements.txt

# 启动三个服务（可同时运行）
streamlit run streamlit_app.py                         # BI 看板 :8501
streamlit run ai-ecommerce-assistant/app.py            # AI 助手 :8505
python -m uvicorn backend.main:app --port 8000         # API 服务 :8000
```

本地开发模式仍保留三个服务直连端口，便于调试；Docker 部署模式统一通过 `http://localhost/` 访问。

## 环境变量

**`backend/.env`**：

```env
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=你的MySQL密码
DB_NAME=ecommerce_analysis

REDIS_ENABLED=false
REDIS_HOST=localhost
REDIS_PORT=6379

JWT_SECRET=your-secret-key-change-in-production

LLM_API_KEY=sk-你的DeepSeekKey
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash

DEBUG=false
```

### 默认账号

| 用户名 | 密码 | 角色 |
|--------|------|------|
| `admin` | `admin123` | 管理员 |
| `analyst` | `analyst123` | 分析师 |

> 密码使用 bcrypt(rounds=12) 哈希存储

## API 接口

### 接口列表（31 个）

| 模块 | 接口数 | 说明 | 认证 |
|------|--------|------|------|
| **认证系统** | 3 | 登录 / 刷新Token / 当前用户 | 公开 |
| **系统接口** | 7 | 首页 / 健康检查 / 体验页 / 监控 / 健康面板 / 文档 | 公开 |
| **订单查询** | 3 | 列表（分页+排序）、详情、多条件筛选 | JWT |
| **商品与用户** | 2 | 商品销售排名、用户消费排名 | JWT |
| **数据分析** | 5 | 销售总览、趋势、热销商品、用户行为、平台分析 | JWT |
| **AI 助手** | 1 | 自然语言 → SQL → 结果 | JWT |
| **数据导出** | 2 | CSV / Excel 导出（分批查询防 OOM） | JWT |
| **监控** | 3 | 实时指标、健康检查、外部服务状态 | 部分公开 |
| **RFM 用户画像** | 4 | 总览 / 分群 / 分群详情 / TOP 用户 | JWT |

### 使用示例

```bash
# 获取 Token
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}'

# 调用受保护接口
curl -X POST http://localhost:8000/api/ai/query \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "销售额最高的3个商品"}'

# 数据分析（需认证）
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/analytics/sales-overview

# RFM 用户画像（需认证）
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/rfm/overview
```

## 技术架构

```
┌─────────────────────────────────────────────────────────┐
│                   Nginx (:80)                           │
│          WebSocket 透传 + 路由分流 + Gzip              │
└──────┬──────────┬──────────┬──────────┬─────────────────┘
       │          │          │          │
       ▼          ▼          ▼          ▼
┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
│Streamlit │ │ FastAPI  │ │ AI 助手  │ │ Swagger  │
│  BI 看板  │ │  后端    │ │ LangChain│ │   Docs   │
└──────────┘ └────┬─────┘ └────┬─────┘ └──────────┘
                  │            │
       ┌──────────┴────────────┴──────────┐
       │         业务逻辑层                │
       │  order_service │ analytics_service│
       │  ai_service    │ rfm_service     │
       ├───────────────────────────────────┤
       │         数据访问层                │
       │  SQLAlchemy Async (aiomysql)     │
       │  MySQL 连接池 (10+20)            │
       ├───────────────────────────────────┤
       │  JWT Auth │ Rate Limit │ Redis   │
       │ (bcrypt)  │ (TTL清理)  │ (双层)  │
       └───────────────────────────────────┘
```

## RFM 用户画像

基于 RFM 模型（Recency / Frequency / Monetary），对 78,060 名用户进行五分位量化评分和自动分群。

### 分群规则

```
R≥4 F≥4 M≥4 → 重要价值客户    R≥4 F<4 M≥4 → 重要保持客户
R≥4 F≥4 M<4 → 重要发展客户    R≥4 F<4 M<4 → 重要挽留客户
R<4 F≥4 M≥4 → 一般价值客户    R<4 F<4 M≥4 → 一般保持客户
R<4 F≥4 M<4 → 一般发展客户    R<4 F<4 M<4 → 一般挽留客户
```

### 数据概览

| 指标 | 数值 |
|------|------|
| 分析用户数 | 78,060 名 |
| 平均消费间隔 | 147.5 天 |
| 平均消费频次 | 1.31 次 |
| 平均消费金额 | ¥1,303.83 |
| 高价值客户 | 31,227 人（40%），贡献 ¥4,482 万 |

## 安全特性

| 特性 | 说明 |
|------|------|
| JWT 认证 | bcrypt 密码哈希(rounds=12) + HS256 签名，24h 有效期，含 iat 签发时间 |
| 全端点认证 | 所有数据端点（订单/分析/RFM/商品/监控/导出）均需 JWT |
| LLM SQL 只读 | AI 生成的 SQL 执行前拦截 DROP/DELETE/UPDATE/INSERT/ALTER/TRUNCATE/CREATE |
| 请求限流 | 按路径差异化限流 + TTL 自动清理 + 线程安全 |
| 输入校验 | 日期参数 regex 校验（YYYY-MM-DD）、导出格式白名单（csv/excel） |
| SQL 注入防护 | LIKE 查询自动转义特殊字符 |
| 敏感过滤 | AI 查询自动拦截隐私问题 |
| Redis 缓存 | 双层架构（Redis + 内存降级），内存缓存上限 1000 条 |
| 缓存防击穿 | `@cached` 基于 asyncio.Lock 的 double-check lock，过期时异步重建 |
| CORS 控制 | 白名单模式 |
| X-Forwarded-For | 取最右端 IP 防止伪造 |

## 性能优化

| 优化项 | 详情 |
|--------|------|
| 异步全链路 | async/await 从路由到数据库（aiomysql） |
| 连接池 | 10 核心 + 20 溢出，3600s 自动回收 |
| 缓存加速 | 销售总览 2min / 用户行为 3min / RFM 10min |
| 缓存防击穿 | asyncio.Lock double-check lock，防止并发重建 |
| 内存缓存上限 | max_size=1000，满时先清过期再驱逐最旧 |
| 分批导出 | 每批 5000 条，防止 OOM |
| 数据库索引 | 7 个关键索引（含 order_date） |

## 技术栈

| 类别 | 技术 |
|------|------|
| 后端 | Python 3.12, FastAPI, SQLAlchemy (async), aiomysql |
| 数据库 | MySQL 8.0, Redis 7 |
| AI | LangChain, DeepSeek V4 Flash |
| 前端 | Streamlit, Plotly, GSAP 3 |
| 认证 | python-jose, bcrypt |
| 部署 | Docker Compose, Nginx |
| 测试 | pytest, pytest-asyncio, httpx |

## 项目结构

```
ecommerce_analysis/
├── backend/                    # FastAPI 后端
│   ├── main.py                 # 应用入口 + 生命周期
│   ├── config.py               # Pydantic Settings 配置
│   ├── database.py             # AsyncSession 连接池
│   ├── routes/                 # 路由层（8 个模块）
│   │   ├── auth.py             # JWT 认证
│   │   ├── orders.py           # 订单查询
│   │   ├── analytics.py        # 数据分析
│   │   ├── ai.py               # AI 助手
│   │   ├── export.py           # 数据导出
│   │   ├── monitor.py          # 系统监控
│   │   ├── rfm.py              # RFM 用户画像
│   │   └── products.py         # 商品/用户排名
│   ├── services/               # 业务逻辑层
│   ├── models/                 # SQLAlchemy 模型
│   ├── utils/                  # 工具（auth/cache/rate_limiter）
│   ├── static/                 # HTML 页面（GSAP 动画）
│   └── tests/                  # 单元测试
├── ai-ecommerce-assistant/     # AI Streamlit 助手
├── streamlit_app.py            # BI 看板主程序
├── deploy/                     # Nginx + Docker 部署配置
│   ├── nginx.conf              # 反向代理配置
│   └── .env.example            # 环境变量模板
├── docker-compose.yml          # Docker 编排
├── data/                       # 清洗后数据
├── notebook/                   # 分析 Notebook
└── sql/                        # 建表/导入 SQL
```

## 数据指标

| 指标 | 数值 |
|------|------|
| 原始数据量 | 102,318 条 |
| 清洗后数据 | 100,286 条 |
| 分析用户数 | 78,060 名 |
| 时间跨度 | 2025.01 - 2026.01 |
| 总销售额 | ¥101,776,848.74 |
| 复购率 | 25.39% |
| API 接口 | 31 个 |
| 测试用例 | 27 个 |
| 支持平台 | 6 个 |

## 运行测试

```bash
# 激活虚拟环境
.venv\Scripts\activate

# 运行全部测试
python -m pytest backend/tests/ -v
```

## 更新日志

- **v1.6** — 全面安全加固 + 代码质量修复
  - 所有数据端点加 JWT 认证（orders/analytics/rfm/products/monitor）
  - LLM SQL 执行前加只读拦截（防止 DROP/DELETE/UPDATE/INSERT）
  - AI 错误响应不再泄露原始异常信息
  - 日期参数加 regex 校验（非法格式返回 422）
  - 导出格式白名单校验（仅允许 csv/excel）
  - 缓存防击穿（asyncio.Lock double-check lock）
  - 内存缓存上限 1000 条 + LRU 驱逐
  - RFM 冗余计算消除（全量缓存 all_users，查询减半）
  - X-Forwarded-For 取最右端 IP 防伪造
  - JWT 加 iat 签发时间 claim
- **v1.5** — 代码质量全面审查：修复12项问题（连接池优化、Agent缓存重建、RFM分页查询、导出OOM防护、Redis SCAN替代KEYS、Plotly颜色格式修复等）
- **v1.4** — GSAP 动画增强 + RFM 评分算法修复 + 缓存机制优化
- **v1.3** — DeepSeek V4 Flash + RFM 可视化大屏 + 图表美化
- **v1.2** — 安全加固 + Redis 双层缓存 + RFM 用户画像 + Docker 部署
- **v1.1** — JWT 认证 + 限流 + 缓存 + 监控 + 测试
- **v1.0** — 基础 API + Swagger + AI 查询 + 数据导出

## License

MIT
