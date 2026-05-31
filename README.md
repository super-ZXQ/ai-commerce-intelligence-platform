# 电商数据分析系统

![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-v1.2-green?logo=fastapi)
![Streamlit](https://img.shields.io/badge/Streamlit-BI看板-red?logo=streamlit)
![LangChain](https://img.shields.io/badge/LangChain-AI助手-orange?logo=langchain)
![MySQL](https://img.shields.io/badge/MySQL-8.0-orange?logo=mysql)
![Redis](https://img.shields.io/badge/Redis-7-red?logo=redis)
![Docker](https://img.shields.io/badge/Docker-Compose-blue?logo=docker)
![License](https://img.shields.io/badge/License-MIT-green)

## 项目简介

基于 **10万+ 条电商真实订单数据**，完成从数据清洗、特征工程到多维分析与可视化的完整链路。系统采用前后端分离架构，提供交互式 BI 看板、AI 智能查询、RESTful API 服务、RFM 用户画像和实时监控五大核心能力。

| 模块 | 技术栈 | 功能 |
|------|--------|------|
| **BI 数据看板** | Streamlit + Plotly | 交互式数据大屏，多维度交叉筛选 |
| **AI 分析助手** | LangChain + DeepSeek V4 Flash + MySQL | 自然语言提问，自动生成 SQL 并绘图 |
| **FastAPI 后端接口** | FastAPI + SQLAlchemy + LangChain | 31 个 RESTful API，含 JWT 认证/限流/缓存/监控/RFM |
| **RFM 用户画像** | SQLAlchemy + 量化分群 | R/F/M 五分位评分 → 8 类用户分群 + 流失预警 |
| **数据分析 Notebook** | Jupyter + Pandas | 数据清洗、销售/时间/用户多维分析 |

## 在线演示

| 应用 | 链接 | 说明 |
|------|------|------|
| **BI 数据看板** | [点击体验](https://ecommerce-analysis-system-cqd8tpywoxneg8n3wqexfm.streamlit.app) | Streamlit Cloud 在线部署 |
| **AI 分析助手** | 本地 `http://localhost:8505` | 自然语言查数，自动 SQL + 图表 |
| **API 文档** | 本地 `http://localhost:8000/docs` | Swagger UI 交互式文档 |
| **API 体验页面** | 本地 `http://localhost:8000/demo` | 可视化数据大屏 + AI 查询 |
| **系统监控面板** | 本地 `http://localhost:8000/monitor` | 可视化监控仪表盘 |
| **健康检查面板** | 本地 `http://localhost:8000/health-panel` | 组件健康状态与指标看板 |

---

## Docker Compose 一键部署

```bash
# 1. 克隆项目
git clone https://github.com/2681107509-dev/ecommerce-analysis-system.git
cd ecommerce_analysis

# 2. 配置环境变量
cp deploy/.env.example .env
# 编辑 .env 填入 MySQL 密码、JWT Secret、LLM API Key

# 3. 一键启动全部服务
docker-compose up -d

# 4. 查看服务状态
docker-compose ps
```

**服务架构：**

```
docker-compose up -d
├── ea-mysql        → MySQL 8.0 (3306) + 自动建表+导入
├── ea-redis        → Redis 7 Alpine (6379) + AOF持久化 + LRU淘汰
├── ea-backend      → FastAPI (8000) → 依赖 mysql + redis
├── ea-streamlit    → BI 看板 (8501) → 依赖 mysql
└── ea-ai-assistant → AI 助手 (8505) → 依赖 mysql
```

| 特性 | 说明 |
|------|------|
| 健康检查链路 | MySQL → Redis → Backend → Streamlit/AI，全部 `healthcheck` + `depends_on` |
| 数据持久化 | MySQL `mysql_data` + Redis `redis_data` Docker Volume |
| Redis 配置 | AOF + RDB 双持久化、256MB 上限、allkeys-lru 淘汰 |
| 优雅依赖 | 后端等待 MySQL 就绪后才启动，Streamlit 等待后端 |

---

## FastAPI 后端接口

为项目提供完整的 RESTful API 服务，支持外部系统（小程序、移动端、前端）实时调用数据。

### 核心功能（31 个接口）

| 模块 | 接口数 | 说明 | 认证 |
|------|--------|------|------|
| **认证系统** | 3 | 登录 / 刷新Token / 当前用户 | 公开 |
| **系统接口** | 7 | 首页 / 健康检查 / 体验页 / 监控面板 / 健康面板 / API文档 | 公开 |
| **订单查询** | 3 | 列表（分页+排序）、详情、多条件筛选 | 公开 |
| **商品与用户** | 2 | 商品销售排名、用户消费排名 | 公开 |
| **数据分析** | 5 | 销售总览(缓存)、趋势、热销商品、用户行为(缓存)、平台分析 | 公开 |
| **AI 助手** | 1 | 自然语言 -> 自动 SQL -> 返回结果 | 需 JWT |
| **数据导出** | 2 | CSV / Excel 格式导出（分批查询防 OOM） | 公开 |
| **监控** | 3 | 实时指标统计、详细健康检查（含 Redis 状态）、外部服务状态 | 公开 |
| **RFM 用户画像** | 4 | 总览 / 分群 / 分群用户详情 / TOP 用户 | 公开 |

### 技术架构

```
┌─────────────────────────────────────────────────────┐
│              FastAPI Application (v1.2)              │
├──────────┬──────────┬──────────┬──────────┬──────────┤
│  auth/   │ orders/  │products  │analytics │   ai/    │
│  routes  │  routes  │  routes  │  routes  │ export/  │
│          │          │          │          │ monitor/ │
│          │          │          │          │   rfm/   │
├──────────┴──────────┴──────────┴──────────┴──────────┤
│              Services Layer                           │
│  order_service │ analytics_service │ ai_service       │
│  rfm_service (RFM量化分群引擎)                        │
├───────────────────────────────────────────────────────┤
│         SQLAlchemy Async (aiomysql)                 │
│              MySQL Connection Pool (10+20)           │
├─────────────────────────────────────────────────────┤
│  JWT Auth  │ Rate Limit │ Redis Cache │ Monitor     │
│  (bcrypt)  │ (TTL清理)  │ (双层降级)  │             │
└─────────────────────────────────────────────────────┘
                    ↕
            ┌───────────────┐
            │  LangChain    │
            │  SQL Agent    │
            │  (DeepSeek)   │
            └───────────────┘
```

### 认证与使用示例

```bash
# 1. 获取 Token
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}'

# 2. 使用 Token 调用受保护接口
curl -X POST http://localhost:8000/api/ai/query \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "销售额最高的3个商品"}'

# 3. 公开接口无需 Token
curl http://localhost:8000/api/analytics/sales-overview
curl http://localhost:8000/api/analytics/top-products?limit=5
curl "http://localhost:8000/api/orders/filter?platform_type=APP&page_size=10"
curl -o report.xlsx "http://localhost:8000/api/export/analytics?export_format=excel"

# 4. RFM 用户画像
curl http://localhost:8000/api/rfm/overview
curl http://localhost:8000/api/rfm/segments
curl "http://localhost:8000/api/rfm/segments/重要价值客户?page=1&page_size=20"
curl http://localhost:8000/api/rfm/top-users?limit=10

# 5. 外部服务状态检测
curl http://localhost:8000/api/monitor/services-status
```

### 安全特性

| 特性 | 说明 | 配置位置 |
|------|------|---------|
| JWT Bearer Token 认证 | bcrypt 密码哈希(rounds=12) + HS256 签名，24h 有效期 | `utils/auth.py` |
| JWT Secret 强制校验 | 生产环境未设置 JWT_SECRET 拒绝启动，开发模式降级告警 | `config.py` |
| API 请求频率限制 | 按路径差异化限流 + TTL 自动清理 + 线程安全 Lock | `utils/rate_limiter.py` |
| SQL 注入防护 | LIKE 查询自动转义 `%` `_` 特殊字符 | `services/order_service.py` |
| 敏感数据过滤 | AI 查询自动拦截隐私问题（手机号/身份证/密码） | `services/ai_service.py` |
| Redis 双层缓存 | Redis 优先 → 失败自动降级内存缓存，TTL 过期双保障 | `utils/cache.py` |
| CORS 跨域控制 | 白名单模式，仅允许本地端口访问 | `config.py` |
| 统一错误处理 | 全局异常捕获，生产环境隐藏堆栈 | `main.py` |
| 密钥保护 | `.env` 加入 `.gitignore`，防止密钥泄露 | `.gitignore` |

### 性能优化

| 优化项 | 详情 |
|--------|------|
| 异步全链路 | async/await 从路由到数据库（asyncio + aiomysql） |
| 连接池 | 10 核心连接 + 20 溢出连接，3600s 自动回收 |
| Redis 缓存 | 热门查询 Redis 缓存，AOF 持久化，LRU 淘汰，内存降级 |
| 分批导出 | 每批 5000 条，防止大数据量 OOM |
| 用户行为优化 | 5 次 SQL 合并为 2 次 |
| 缓存加速 | 销售总览 2min / 用户行为 3min / RFM 10min，TTL 可配 |
| 数据库索引 | 7 个关键索引（见 `sql/optimize_indexes.sql`） |
| 限流器优化 | 后台线程 300s 清理过期 key，线程安全 Lock，上限 10000 条 |
| 请求日志 | 自动记录方法/路径/状态码/耗时/限流信息 |

---

## RFM 用户画像

基于 RFM 模型（Recency 最近消费 / Frequency 消费频次 / Monetary 消费金额），对 78,060 名用户进行五分位量化评分和自动分群。

### 分群模型

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

### API 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/rfm/overview` | GET | RFM 总览：高价值/流失预警 + 分群分布 + 消费分布 |
| `/api/rfm/segments` | GET | 全量分群结果（支持自定义参考日期和分位数） |
| `/api/rfm/segments/{segment}` | GET | 指定分群用户详情（分页） |
| `/api/rfm/top-users` | GET | RFM 评分 TOP 用户 |

---

## Redis 缓存架构

采用 **Redis 优先 + 内存降级** 的双层缓存策略，确保服务高可用：

| 特性 | 实现 |
|------|------|
| **双层架构** | Redis 优先 → 连接失败自动降级内存缓存 |
| **TTL 过期** | Redis `SETEX` + 内存 `expires_at` 双重保障 |
| **持久化** | AOF（everysec）+ RDB（900s/300s/60s 三级快照） |
| **淘汰策略** | `maxmemory 256mb` + `allkeys-lru` |
| **模式失效** | `invalidate_pattern()` 同时清理 Redis + 内存 |
| **健康检查** | `/api/monitor/health/detailed` 含 Redis 连接状态 |
| **优雅降级** | Redis 不可用时自动切内存，日志告警 |

---

## AI 电商分析助手

直接用自然语言与数据对话：

```
用户：销售额最高的 3 个商品编号和金额
 AI：自动生成 SQL -> 执行查询 -> 数据表格 + 柱状图 -> 导出 CSV
```

| 功能 | 说明 |
|------|------|
| Text-to-SQL | 自然语言自动生成 SQL，无需手写查询 |
| 自动绘图 | 智能识别图表类型：折线图 / 柱状图 / 饼图 / 横向柱状图 |
| 智能重查询 | 检测"时间段最多"类问题，自动去除 LIMIT 重查完整数据 |
| 数据解析 | SQL 失败时自动从 AI 回答中解析数据绘制图表 |
| 查询缓存 | 重复问题即时返回，减少 API 调用 |
| 敏感过滤 | 拦截手机号、身份证等隐私查询 |
| 异常预警 | 自动检测异常指标并给出业务建议 |
| 数据导出 | 支持 CSV 下载查询结果 |

---

## 技术栈

| 类别 | 技术 | 说明 |
|------|------|------|
| 语言 | Python, SQL | Python 3.11, MySQL 8.0 |
| 后端框架 | FastAPI + Uvicorn | 异步高性能 RESTful API |
| ORM | SQLAlchemy (async) + aiomysql | 异步 ORM + 异步 MySQL 驱动 |
| 数据验证 | Pydantic v2 | 请求/响应模型校验 |
| JWT 认证 | python-jose + bcrypt | HS256 签名 + bcrypt 密码哈希(rounds=12) |
| 缓存 | Redis 7 + 内存降级 | 双层缓存 + AOF 持久化 + LRU 淘汰 |
| 容器化 | Docker Compose | MySQL + Redis + FastAPI + Streamlit 一键部署 |
| 数据处理 | pandas, numpy | pandas 2.0+ |
| 可视化 | matplotlib, plotly | 静态图 + 交互式图表 |
| 前端动画 | GSAP 3 + ScrollTrigger | 粒子背景 + 交错入场 + 3D 交互 |
| BI 看板 | Streamlit | 交互式数据大屏 |
| AI 框架 | LangChain, LangChain-OpenAI | SQL Agent + LLM 调用 |
| 用户画像 | RFM 模型 | 五分位量化评分 + 8 类自动分群 |
| 测试框架 | pytest + pytest-asyncio + httpx | 异步单元测试 |
| 开发环境 | Jupyter Notebook, VS Code | 模块化 Notebook 开发 |
| 版本控制 | Git, GitHub | 规范 commit 信息 |

---

## 关键指标

| 指标 | 数值 | 说明 |
|------|------|------|
| 原始数据量 | 102,318 条 | 电商订单记录 |
| 清洗后数据 | 100,286 条 | 删除 2,032 条异常数据 |
| 分析用户数 | 78,060 名 | 去重后独立用户 |
| 时间跨度 | 2025.01-2026.01 | 全年销售数据 |
| 总销售额 | 101,776,848.74 元 | 实际付款金额 |
| 复购率 | 25.39% | 消费 >=2 次的用户占比 |
| API 接口 | 31 个 | 含认证/限流/监控/RFM |
| 默认账号 | admin/admin123 | JWT 认证账号（bcrypt 哈希） |
| 测试用例 | 27 个 | 异步单元测试 |
| 支持平台 | 6 个 | APP/微信公众号/Web网站/淘宝/微信小商店/wap网站 |

---

## 项目结构

```
ecommerce_analysis/
├── backend/                        # FastAPI 后端服务
│   ├── main.py                     # 应用入口（CORS/中间件/路由注册/限流/日志/Redis初始化）
│   ├── config.py                   # 配置管理（Pydantic Settings + JWT/Redis/LLM）
│   ├── database.py                 # 异步连接池 + 健康检查
│   ├── demo.html                   # 可视化体验页面（含自动登录）
│   ├── requirements.txt            # Python 依赖
│   ├── pytest.ini                  # pytest 配置
│   │
│   ├── models/
│   │   ├── schemas.py              # Pydantic 模型（15+ 个，含分页/认证/AI）
│   │   └── database_models.py      # SQLAlchemy ORM 模型
│   │
│   ├── routes/
│   │   ├── auth.py                 # 认证路由（login/refresh/me）
│   │   ├── orders.py               # 订单路由（列表/详情/筛选/排序验证）
│   │   ├── products.py             # 商品与用户路由
│   │   ├── analytics.py            # 数据分析路由（5 个接口）
│   │   ├── ai.py                   # AI 助手路由（需 JWT 认证）
│   │   ├── export.py               # 导出路由（CSV/Excel 分批查询）
│   │   ├── monitor.py              # 监控路由（metrics/detailed health + Redis）
│   │   └── rfm.py                  # RFM 用户画像路由（4 个接口）
│   │
│   ├── services/
│   │   ├── order_service.py        # 订单逻辑（SQL注入防护/分页优化）
│   │   ├── analytics_service.py    # 分析逻辑（缓存装饰器/查询优化）
│   │   ├── ai_service.py           # AI 查询逻辑（敏感过滤/重试/解析）
│   │   └── rfm_service.py          # RFM 量化分群引擎（五分位评分/8类分群/流失预警）
│   │
│   ├── static/
│   │   ├── index.html              # 统一入口导航页面
│   │   ├── monitor.html            # 系统监控可视化面板
│   │   └── health.html             # 健康检查可视化面板
│   │
│   ├── utils/
│   │   ├── auth.py                 # JWT 工具（bcrypt 密码哈希/生成/验证）
│   │   ├── rate_limiter.py         # 限流中间件（TTL清理/Lock/上限淘汰）
│   │   └── cache.py                # 双层缓存（Redis优先 + 内存降级）
│   │
│   ├── tests/
│   │   └── test_api.py             # 27 个异步单元测试
│   │
│   ├── postman/
│   │   └── Ecommerce_API.postman_collection.json
│   │
│   └── sql/
│       └── optimize_indexes.sql    # 数据库索引优化脚本（7个索引）
│
├── deploy/                          # 部署配置
│   ├── redis.conf                   # Redis 生产配置（AOF/RDB/LRU/持久化）
│   └── .env.example                 # 环境变量模板
│
├── ai-ecommerce-assistant/          # AI Streamlit 助手（LangChain + Qwen）
├── streamlit_app.py                 # Streamlit BI 看板主程序
├── docker-compose.yml               # Docker Compose 一键部署编排
├── Dockerfile                       # FastAPI 后端镜像
├── Dockerfile.streamlit             # BI 看板镜像
├── data/cleaned_orders.csv         # 清洗后数据
├── notebook/                       # 5 个分析 Notebook
├── sql/                            # 建表/导入/高级分析 SQL
└── README.md
```

---

## 运行方式

### 方式一：Docker Compose 一键部署（推荐）

```bash
git clone https://github.com/2681107509-dev/ecommerce-analysis-system.git
cd ecommerce_analysis

# 配置环境变量
cp deploy/.env.example .env
# 编辑 .env 填入 MySQL 密码、JWT Secret、LLM API Key

# 一键启动
docker-compose up -d

# 访问：
# 统一入口:    http://localhost:8000/
# Swagger 文档: http://localhost:8000/docs
# BI 看板:     http://localhost:8501
# AI 助手:     http://localhost:8505
```

### 方式二：本地开发

```bash
git clone https://github.com/2681107509-dev/ecommerce-analysis-system.git
cd ecommerce_analysis

python -m venv .venv && .venv\Scripts\activate

pip install -r requirements.txt
pip install -r ai-ecommerce-assistant/requirements.txt
pip install -r backend/requirements.txt

# 启动三个服务（可同时运行）
streamlit run streamlit_app.py                              # BI 看板 :8501
streamlit run ai-ecommerce-assistant/app.py                # AI 助手 :8505
python -m uvicorn backend.main:app --port 8000              # API 服务 :8000
```

### 环境变量配置

**`backend/.env`（FastAPI 后端）**：

```env
# 数据库
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=你的MySQL密码
DB_NAME=ecommerce_analysis

# Redis 缓存（可选，不启用则使用内存缓存）
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=
REDIS_DB=0
REDIS_ENABLED=false

# JWT 认证（生产环境必须设置强随机密钥）
JWT_SECRET=your-secret-key-change-in-production

# AI 助手（可选）
LLM_API_KEY=sk-你的阿里云Key
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus

# 应用配置
DEBUG=false
```

**`ai-ecommerce-assistant/.env`（AI 分析助手）**：
```env
# 数据库
DB_HOST=localhost
DB_USER=root
DB_PASSWORD=你的MySQL密码
DB_NAME=ecommerce_analysis

# LLM 配置（阿里云通义千问）
LLM_API_KEY=sk-你的阿里云Key
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus
```

### 默认账号

| 用户名 | 密码 | 角色 |
|--------|------|------|
| `admin` | `admin123` | 管理员 |
| `analyst` | `analyst123` | 分析师 |

> 密码使用 bcrypt(rounds=12) 哈希存储，非明文

### 运行测试

```bash
cd backend
python -m pytest tests/test_api.py -v
```

### 数据库索引优化

```bash
mysql -u root -p < backend/sql/optimize_indexes.sql
```

---

## 依赖清单

**FastAPI 后端（完整）**：
```
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
sqlalchemy[asyncio]>=2.0.0
aiomysql>=0.2.0
pymysql>=1.1.0
pydantic>=2.5.0
pydantic-settings>=2.1.0
pandas>=2.0.0
openpyxl>=3.1.0
python-dotenv>=1.0.0
langchain>=0.2.0
langchain-community>=0.2.0
langchain-openai>=0.1.0
python-jose[cryptography]>=3.3.0
bcrypt>=4.0.0
httpx>=0.25.0
pytest>=7.4.0
pytest-asyncio>=0.21.0
scipy>=1.11.0
numpy>=1.24.0
redis>=5.0.0
```

---

## 迭代规划

- **v1.0**：17 个基础 API + Swagger 文档 + AI 查询 + 数据导出
- **v1.1**：JWT 认证 + API 限流 + 响应缓存 + 监控指标 + 27 个测试用例 + Postman Collection + 数据库索引优化 + 可视化监控面板 + 健康检查面板 + 统一入口导航
- **v1.2**：安全加固（bcrypt 密码哈希 + JWT Secret 强制校验 + CORS 白名单 + 限流器 TTL 清理 + .gitignore 密钥保护）+ Redis 双层缓存（AOF 持久化 + LRU 淘汰 + 内存降级）+ RFM 用户画像（五分位评分 + 8 类分群 + 流失预警 + 4 个 API）+ Docker Compose 一键部署（MySQL + Redis + FastAPI + Streamlit + AI 助手）+ 外部服务状态检测接口 + 导航页跨域代理检测 + AI 服务单例缓存优化
- **v1.3**：AI 模型切换为 DeepSeek V4 Flash + RFM 客户分层可视化大屏（饼图/柱状图/热力图/三维散点图/直方图/消费对比图）+ RFM 代码审查与边界条件修复 + 统一图表美化 + 项目介绍手册
- **v1.4**：GSAP 动画增强（4 个 HTML 页面粒子背景 + 交错入场 + 3D 交互）+ RFM 评分算法修复（`_quantile_score` 非反转模式分布失衡）+ AI 服务缓存机制修复（移除冗余 `lru_cache`）+ RFM 分群详情复用缓存 + Streamlit 缓存键可靠性增强
- **规划中**：漏斗分析、购物篮关联分析（Apriori）、销量预测（Prophet）、RBAC 权限系统

---

## 项目演示介绍稿

> 建议演示时长：15-20 分钟

### 第 1 站：导航页 — 系统全貌入口

**访问地址**：`http://localhost:8000`

**台词**：

> 大家好，欢迎了解我们的电商数据分析系统。现在大家看到的，是系统的统一导航入口。这个页面采用深色科技风设计，顶部有系统名称和版本号，右侧实时显示 API 运行状态。页面背景有动态粒子网络效果，随鼠标移动产生交互。
>
> 页面中央是六大功能模块的入口卡片，每张卡片都带有交错入场动画，依次从下方弹入。卡片实时检测对应服务的运行状态——绿色"运行中"表示服务正常，橙色"未启动"则需要手动开启。卡片悬浮时有渐变高亮效果，点击即可跳转到对应功能。
>
> 这六大模块分别是：BI 数据看板、AI 分析助手、API 文档、API 体验页面、系统监控面板和健康检查面板。整个系统基于 10 万+ 条真实电商订单数据构建，接下来我将逐一为大家演示。

### 第 2 站：BI 数据看板 — 销售总览

**访问地址**：`http://localhost:8501` → 选择"📊 销售总览"

**台词**：

> 首先进入 BI 数据看板。左侧是筛选面板，可以按平台类型和日期范围进行交叉筛选，所有图表会实时联动更新。
>
> 顶部四个核心指标卡片一目了然：总销售额、总订单数、活跃用户数和客单价，每个指标还附带环比变化提示。
>
> 往下看，左侧是每日销售趋势折线图，可以清晰看到销售高峰和低谷；右侧是平台销售占比环形图，直观展示各平台的贡献度。再往下是商品销售额 TOP10 和用户消费 TOP10 的柱状图，帮助快速定位爆款商品和核心用户。
>
> 整个看板支持交互式操作，鼠标悬浮可查看详细数据，筛选条件实时生效。

### 第 3 站：RFM 客户分层 — 核心亮点

**访问地址**：`http://localhost:8501` → 选择"👥 RFM 客户分层"

**台词**：

> 接下来是我们系统的核心亮点——RFM 客户分层分析。RFM 是一种经典的客户价值评估模型，分别从 Recency（最近消费时间）、Frequency（消费频率）和 Monetary（消费金额）三个维度对客户进行分层。
>
> 左侧参数面板支持灵活调整：分位数分组数可以从 3 调到 10，值越细分层越精细；参考日期可以自定义；R、F、M 三个维度的评分阈值都可以独立调节，满足不同业务场景的分层需求。
>
> 看顶部指标区：分析用户数、平均最近消费天数、平均消费频次、平均消费金额，以及重要客户数、流失风险客户数和重要客户贡献金额占比——这些是决策者最关心的核心数字。
>
> 往下是客户分层分布的可视化：左侧环形饼图展示各分层的客户占比，右侧柱状图展示各分层的绝对人数。颜色编码统一，红色系代表"重要"客户，蓝绿色系代表"一般"客户。
>
> 热力图展示了 R 评分和 F 评分的交叉分布，颜色越深代表该组合的客户越多。三维散点图以立体视角展示客户在三个维度上的分布，支持鼠标拖拽旋转。R/F/M 评分分布直方图标注了当前阈值，帮助判断阈值设置是否合理。
>
> 消费金额对比图分别展示了各分层的总消费金额和人均消费金额，可以清晰看到"重要价值客户"虽然人数可能不多，但贡献了绝大部分营收。
>
> 最后是分层客户明细表，支持按分层筛选、分页浏览。页面底部还有 RFM 分层逻辑详解，包含每个分层的含义和运营建议。

### 第 4 站：AI 分析助手 — 智能查询

**访问地址**：`http://localhost:8505`

**台词**：

> 现在我们来看 AI 分析助手。这个模块集成了 LangChain 和 DeepSeek V4 Flash 大模型，实现了 Text-to-SQL 的智能查询能力。
>
> 使用非常简单：直接用自然语言提问，比如"销售额最高的 3 个商品编号和金额"、"APP 和公众号哪个销售额高"、"用户复购率是多少"，AI 会自动理解意图、生成 SQL、执行查询，并以表格和图表的形式呈现结果。
>
> 右侧是查询历史面板，可以快速回顾之前的提问。系统还支持 SQL 语句的展示和复制、查询耗时统计、CSV 数据导出等功能。对于敏感查询，系统内置了安全过滤机制，防止生成危险 SQL 操作。
>
> 这个模块的价值在于：让不会写 SQL 的业务人员也能自助查数据，大幅降低了数据获取的门槛和等待时间。

### 第 5 站：API 文档与体验

**访问地址**：`http://localhost:8000/docs`（Swagger 文档）| `http://localhost:8000/demo`（体验页面）

**台词**：

> 后端提供了 31 个 RESTful API 接口，FastAPI 自动生成了交互式 Swagger 文档。在文档页面可以直接调试每个接口，查看请求参数、响应模型和状态码。
>
> API 体验页面提供了更友好的可视化操作界面，包含销售总览、热销商品、平台占比等 6 大模块，一键查看数据，无需编写代码。

### 第 6 站：系统监控与健康检查

**访问地址**：`http://localhost:8000/monitor` | `http://localhost:8000/health-panel`

**台词**：

> 最后是运维保障模块。系统监控面板实时展示运行时长、请求统计、错误率、热门接口排名和组件健康状态。健康检查面板提供可视化的组件诊断，包括数据库连接、缓存状态、组件通过率和历史检测记录。
>
> 这两个面板确保系统运行状态透明可控，出现问题时可以快速定位。

### 演示注意事项

- 演示前确保所有服务已启动：FastAPI 后端（8000）、BI 看板（8501）、AI 助手（8505）
- 导航页会自动检测各服务状态，如有"未启动"的卡片，需先启动对应服务
- AI 助手首次提问可能需要几秒响应时间（模型冷启动），后续会更快
- RFM 页面调整参数后图表会自动刷新，如遇卡顿可降低分位数分组数
- 演示 AI 助手时建议准备 2-3 个预设问题，避免现场思考影响流畅度
