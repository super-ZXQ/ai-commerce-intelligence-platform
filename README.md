# 基于大模型的智能电商数据决策平台

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
| **BI 数据看板** | [Streamlit Cloud](https://streamlit.io) | 在线部署 |
| **Docker 统一入口** | `http://localhost/` | 导航页（默认入口） |
| **BI 数据看板** | `http://localhost/BI/` | 交互式数据大屏 |
| **AI 分析助手** | `http://localhost/ai/` | 自然语言查数 |
| **API 文档** | `http://localhost/docs` | Swagger UI |
| **API 体验页** | `http://localhost/demo` | 可视化大屏 + AI 查询 |
| **系统监控** | `http://localhost/monitor` | 实时监控面板 |
| **健康检查** | `http://localhost/health-panel` | 组件健康状态 |

## 快速开始

### Docker Compose 一键部署（推荐）

```bash
git clone https://github.com/2681107509-dev/ai-commerce-intelligence-platform.git
cd ai-commerce-intelligence-platform

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
| `/` | backend:8000 | 统一导航页（默认入口） |
| `/BI/` | streamlit:8501 | BI 看板（baseUrlPath=/BI，访问 `/BI` 会 301 跳转到 `/BI/`） |
| `/ai/` | ai-assistant:8502 | AI 分析助手（baseUrlPath=/ai） |
| `/api/*` | backend:8000 | RESTful API |
| `/nav` | backend:8000 | 导航页（备用路由） |
| `/docs` `/redoc` `/openapi.json` | backend:8000 | Swagger / ReDoc 文档 |
| `/health` `/demo` `/monitor` `/health-panel` | backend:8000 | 系统页面 |

部署模式下仅 Nginx 对宿主机暴露 80 端口，业务服务、数据库和缓存只通过 Docker 内部网络通信。Nginx 已配置 `proxy_http_version 1.1`、`Upgrade` 和 `Connection` 头，支持 Streamlit WebSocket，避免反向代理后页面白屏。

### 本地开发

```bash
git clone https://github.com/2681107509-dev/ai-commerce-intelligence-platform.git
cd ai-commerce-intelligence-platform

python -m venv .venv && .venv\Scripts\activate

pip install -r requirements.txt
pip install -r ai-ecommerce-assistant/requirements.txt
pip install -r backend/requirements.txt
```

**配置环境变量：**

**`backend/.env`**：

```env
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=你的MySQL密码
DB_NAME=ai_commerce_intelligence_platform

REDIS_ENABLED=false
REDIS_HOST=localhost
REDIS_PORT=6379

JWT_SECRET=your-secret-key-change-in-production

LLM_API_KEY=sk-你的DeepSeekKey
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash
```

**`ai-ecommerce-assistant/.env`**：

```env
LLM_API_KEY=sk-你的DeepSeekKey
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash

DB_USER=root
DB_PASSWORD=你的MySQL密码
DB_HOST=localhost
DB_PORT=3306
DB_NAME=ai_commerce_intelligence_platform
```

**启动三端口开发模式：**

```powershell
# 1. FastAPI 后端 (:8000)
Start-Process -WindowStyle Hidden -FilePath ".venv\Scripts\python.exe" -ArgumentList "-m uvicorn backend.main:app --host 0.0.0.0 --port 8000"
# 2. BI 看板 (:8501)
Start-Process -WindowStyle Hidden -FilePath ".venv\Scripts\streamlit.exe" -ArgumentList "run streamlit_app.py --server.port 8501"
# 3. AI 助手 (:8505)
Start-Process -WindowStyle Hidden -FilePath ".venv\Scripts\streamlit.exe" -ArgumentList "run ai-ecommerce-assistant/app.py --server.port 8505"
```

**初始化数据库：**

```bash
# 登录 MySQL
mysql -u root -p
# 执行建表和导入
SOURCE sql/01_create_table.sql;
SOURCE sql/02_import_data.sql;
```

## 项目结构

```
ai-commerce-intelligence-platform/
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
│   ├── models/                 # ORM 模型 + Pydantic Schemas
│   ├── utils/                  # 工具（认证/缓存/限流）
│   ├── static/                 # HTML 静态页
│   ├── sql/                    # SQL 脚本
│   ├── tests/                  # API 单元测试
│   └── requirements.txt
├── streamlit_app.py            # BI 数据看板（Streamlit 多页面）
├── ai-ecommerce-assistant/      # AI 分析助手
│   ├── app.py                  # 主应用
│   └── requirements.txt
├── deploy/                     # 部署配置
│   ├── nginx.conf              # Nginx 反向代理
│   ├── redis.conf              # Redis 持久化配置
│   └── .env.example            # 环境变量模板
├── data/                       # 数据文件
├── sql/                        # 建表/导入/分析脚本
├── notebook/                   # Jupyter 分析笔记本
├── docker-compose.yml          # Docker Compose 编排
├── Dockerfile                  # FastAPI 后端镜像
├── Dockerfile.streamlit        # Streamlit 镜像
└── requirements.txt            # 顶层依赖（可省略）
```

## 技术栈

| 分类 | 技术 |
|------|------|
| 语言 | Python 3.12 |
| Web 框架 | FastAPI 0.115 + Uvicorn |
| ORM | SQLAlchemy 2.0 (async) |
| 前端 | Streamlit 1.41 + Plotly |
| AI | LangChain + DeepSeek V4 Flash |
| 数据库 | MySQL 8.0 |
| 缓存 | Redis 7 |
| 反代 | Nginx |
| 容器 | Docker + Docker Compose |
| 测试 | pytest, pytest-asyncio, httpx |
