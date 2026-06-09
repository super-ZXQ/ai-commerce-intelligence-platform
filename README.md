# 基于大模型的智能电商数据决策平台

![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green?logo=fastapi)
![Streamlit](https://img.shields.io/badge/Streamlit-1.41-red?logo=streamlit)
![LangChain](https://img.shields.io/badge/LangChain-0.3-orange?logo=langchain)
![Chroma](https://img.shields.io/badge/Chroma-Vector_DB-blueviolet?logo=databricks)
![MySQL](https://img.shields.io/badge/MySQL-8.0-orange?logo=mysql)
![Redis](https://img.shields.io/badge/Redis-7-red?logo=redis)
![Docker](https://img.shields.io/badge/Docker--Compose-blue?logo=docker)
![License](https://img.shields.io/badge/License-MIT-green)

## 简介

基于 **10 万+ 条电商真实订单数据**，完成从数据清洗、特征工程到多维分析与可视化的完整链路。系统采用前后端分离架构，提供交互式 BI 看板、AI 智能查询（含 RAG 业务知识检索增强）、RESTful API 服务、RFM 用户画像和实时监控五大核心能力。

## 功能概览

| 模块 | 技术栈 | 功能 |
|------|--------|------|
| **BI 数据看板** | Streamlit + Plotly | 交互式数据大屏，多维度交叉筛选 |
| **AI 分析助手 + RAG** | LangChain + DeepSeek + Chroma + BGE | 自然语言提问，融合业务知识库与 SQL 数据查询，引用来源透明 |
| **FastAPI 后端** | FastAPI + SQLAlchemy | 31 个 RESTful API，含认证/限流/缓存/监控/RFM |
| **RFM 用户画像** | SQLAlchemy + 量化分群 | R/F/M 五分位评分 → 8 类用户分群 + 流失预警 |
| **数据分析 Notebook** | Jupyter + Pandas | 数据清洗、销售/时间/用户多维分析 |
| **RAG 业务知识库** | Chroma + BGE-small-zh-v1.5 | 6 份业务文档（术语/数据字典/KPI/规则/API/黄金查询）向量检索 |
| **测试 & 评估** | pytest + 自研评估器 | 76 个 RAG 单元测试 + 20 条 gold_qa 评估集 |

## 在线演示

| 应用 | 链接 | 说明 |
|------|------|------|
| **BI 数据看板** | [Streamlit Cloud](https://streamlit.io) | 在线部署 |
| **Docker 统一入口** | `http://localhost/` | 导航页（默认入口） |
| **BI 数据看板** | `http://localhost/BI/` | 交互式数据大屏 |
| **AI 分析助手** | `http://localhost/ai/` | 自然语言查数 + RAG 知识库 |
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
| `/ai/` | ai-assistant:8502 | AI 分析助手（baseUrlPath=/ai），内置 RAG 知识库 |
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

## RAG 业务知识检索增强

AI 分析助手内置 RAG 能力，能同时利用**业务知识库**和**SQL 数据查询**回答用户问题。

### 架构

```
┌────────────────────────────────────────────────────────┐
│            Streamlit UI（:8505）                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ Sidebar 状态  │  │  聊天主界面   │  │ 参考知识面板 │  │
│  │ (向量库/命中率)│  │  (SQL+知识)  │  │ (来源透明)  │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
└────────────────────────┬───────────────────────────────┘
                         │ invoke({input})
┌────────────────────────▼───────────────────────────────┐
│  LangChain SQL Agent（ReAct）                          │
│  ┌──────────────────┐  ┌────────────────────────────┐  │
│  │ SQLDatabaseToolkit │ │ query_business_knowledge  │  │
│  │  (sql_db_query等)  │ │     (RAG 工具)           │  │
│  └──────────────────┘  └────────────┬───────────────┘  │
│                                       │ retrieve()    │
│                            ┌──────────▼──────────┐     │
│                            │     Retriever       │     │
│                            │ 缓存/超时/格式化    │     │
│                            │ get_stats() 埋点    │     │
│                            └──────────┬──────────┘     │
│                                       │ search()       │
│                            ┌──────────▼──────────┐     │
│                            │   Chroma 向量库     │     │
│                            │  (BGE-small-zh)    │     │
│                            └──────────▲──────────┘     │
│                                       │ build          │
│                            ┌──────────┴──────────┐     │
│                            │ knowledge_base/*.md │     │
│                            │  6 份业务知识文档   │     │
│                            └─────────────────────┘     │
└────────────────────────────────────────────────────────┘
```

### 知识库文档（`ai-ecommerce-assistant/knowledge_base/`）

| 文件 | 用途 |
|------|------|
| `business_glossary.md` | 业务术语定义（复购率、客单价、GMV 等） |
| `data_dictionary.md` | orders 表字段语义、枚举值、单位 |
| `kpi_formulas.md` | KPI 计算公式（SQL 模板） |
| `business_rules.md` | 业务规则与基准（退款率正常范围、活跃阈值） |
| `gold_queries.md` | 黄金 SQL 查询样例（按问题类型分类） |
| `api_docs.md` | 平台 API 用法、Swagger 入口 |

### 构建向量库

```bash
# 全量重建
python ai-ecommerce-assistant/build_knowledge_base.py --rebuild

# 增量构建（默认）
python ai-ecommerce-assistant/build_knowledge_base.py

# 自定义知识库目录
python ai-ecommerce-assistant/build_knowledge_base.py --kb-dir ./my_kb
```

切分策略：按 H2 标题切片 → 单 chunk 上限 1500 字符 → 长 section 按 H3 优先切 → 滑动窗口 200 字符重叠。生成稳定 `doc_id`（基于 source+section+content 哈希）支持增量更新。

### RAG 模块组成（`ai-ecommerce-assistant/rag/`）

| 文件 | 职责 |
|------|------|
| `embeddings.py` | BGE-small-zh-v1.5 Embedding 工厂（多线程单例 + 设备自动检测） |
| `vector_store.py` | Chroma 封装：增删查改、阈值过滤、元数据过滤、增量构建 |
| `retriever.py` | LRU + TTL 缓存、超时保护、上下文格式化、统计埋点 |
| `prompts.py` | 工具描述、决策树规则、Agent prefix 增强 |
| `tools.py` | `build_knowledge_tool()` LangChain Tool 工厂 |
| `extractor.py` | 从 Agent `intermediate_steps` 还原 RAG 来源（无 streamlit 依赖） |

### 决策树

- 用户问"X 怎么算 / 是什么 / 多少算正常" → Agent 调 `query_business_knowledge` 拿业务知识
- 用户问"X 是多少 / 销量 / 趋势" → Agent 直接走 SQL 工具查数据
- 不确定 → 先调知识库拿知识，再决定是否需要 SQL

回答时引用业务知识，**UI 自动折叠展示**"📚 参考知识"面板（来源、章节、相关度、200 字预览），不污染主回答。

## 测试与评估

### 单元测试（76 个用例）

```bash
# RAG + build_knowledge_base 全量测试
cd ai-ecommerce-assistant
python -m pytest tests/ -v

# 跑某个文件
python -m pytest tests/test_retriever.py -v
```

**测试覆盖：**
- `test_rag_prompts.py` — 提示词模板（工具说明、决策树、回答模板）
- `test_rag_tools.py` — sentinel 序列化 + Tool 工厂（空命中/异常/正常）
- `test_rag_extractor.py` — 从 `intermediate_steps` 还原来源（多步聚合、类型防御）
- `test_vector_store.py` — Chroma 增删查改（fake embedder，不依赖真实模型）
- `test_retriever.py` — 缓存/TTL/LRU/阈值/超时/格式化/stats
- `test_build_kb.py` — 文档切分函数（doc_type、doc_id、滑动窗口）

测试不依赖真实 BGE 模型（首次下载 93MB），用 `tests/conftest.py` 里的 `FakeEmbeddings`（确定性 hash → 384 维归一化向量）。

### 评估集（20 条 gold_qa）

```bash
cd ai-ecommerce-assistant

# 冒烟评估：fake embedder 验证评估流程（不依赖真实模型）
python eval/run_eval.py

# 真实评估：BGE + Chroma 命中真实率
# 首次会下载 BGE-small-zh-v1.5 ~93MB
python eval/run_eval.py --real

# 自定义报告路径
python eval/run_eval.py --report eval/my_report.md
```

评估集覆盖 10 条知识问答（术语/公式/字段/规则/API）+ 10 条数据查询（SQL 类不参与 RAG 评估）。报告自动生成 `eval/report.md`（人类可读）+ `eval/report.json`（机器可读）。

**关键指标：**
- 命中率（命中含 expected_keywords 的文档数 / 总数）
- 实际命中 doc_type 与期望 doc_type 一致性
- Top1 score
- 单次检索耗时

## 项目结构

```
ai-commerce-intelligence-platform/
├── backend/                      # FastAPI 后端
│   ├── main.py                   # 应用入口 + 生命周期
│   ├── config.py                 # Pydantic Settings 配置
│   ├── database.py               # AsyncSession 连接池
│   ├── routes/                   # 路由层（8 个模块）
│   │   ├── auth.py               # JWT 认证
│   │   ├── orders.py             # 订单查询
│   │   ├── analytics.py          # 数据分析
│   │   ├── ai.py                 # AI 助手
│   │   ├── export.py             # 数据导出
│   │   ├── monitor.py            # 系统监控
│   │   ├── rfm.py                # RFM 用户画像
│   │   └── products.py           # 商品/用户排名
│   ├── services/                 # 业务逻辑层
│   ├── models/                   # ORM 模型 + Pydantic Schemas
│   ├── utils/                    # 工具（认证/缓存/限流）
│   ├── static/                   # HTML 静态页
│   ├── sql/                      # SQL 脚本
│   ├── tests/                    # API 单元测试
│   └── requirements.txt
├── streamlit_app.py              # BI 数据看板（Streamlit 多页面）
├── ai-ecommerce-assistant/        # AI 分析助手（含 RAG）
│   ├── app.py                    # 主应用
│   ├── build_knowledge_base.py   # 知识库构建脚本
│   ├── knowledge_base/           # 6 份业务知识 Markdown
│   ├── rag/                      # RAG 核心模块
│   │   ├── embeddings.py         # BGE Embedding 工厂
│   │   ├── vector_store.py       # Chroma 封装
│   │   ├── retriever.py          # 缓存/超时/格式化/埋点
│   │   ├── prompts.py            # 提示词 + 工具说明
│   │   ├── tools.py              # LangChain Tool 工厂
│   │   └── extractor.py          # 来源还原（无 streamlit 依赖）
│   ├── tests/                    # 76 个 RAG 单元测试
│   ├── eval/                     # 评估集 + 评估脚本
│   ├── data/chroma/              # Chroma 持久化目录
│   ├── pytest.ini                # pytest 配置
│   └── requirements.txt
├── deploy/                       # 部署配置
│   ├── nginx.conf                # Nginx 反向代理
│   ├── redis.conf                # Redis 持久化配置
│   └── .env.example              # 环境变量模板
├── data/                         # 数据文件
├── sql/                          # 建表/导入/分析脚本
├── notebook/                     # Jupyter 分析笔记本
├── docker-compose.yml            # Docker Compose 编排
├── Dockerfile                    # FastAPI 后端镜像
├── Dockerfile.streamlit          # Streamlit 镜像
└── requirements.txt              # 顶层依赖（可省略）
```

## 技术栈

| 分类 | 技术 |
|------|------|
| 语言 | Python 3.12 |
| Web 框架 | FastAPI 0.115 + Uvicorn |
| ORM | SQLAlchemy 2.0 (async) |
| 前端 | Streamlit 1.41 + Plotly |
| AI | LangChain + DeepSeek V4 Flash |
| RAG | Chroma（向量库） + BGE-small-zh-v1.5（Embedding） + LangChain Tool |
| 数据库 | MySQL 8.0 |
| 缓存 | Redis 7 |
| 反代 | Nginx |
| 容器 | Docker + Docker Compose |
| 测试 | pytest（76 个 RAG 用例 + 20 条 gold_qa 评估集） |

## License

MIT
