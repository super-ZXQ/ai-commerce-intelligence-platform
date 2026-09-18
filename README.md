# 基于大模型的智能电商数据决策平台

🐍 **Python 3.12** · 🚀 **FastAPI** · 📊 **Streamlit** · 🦜 **LangChain** · 🧠 **Chroma** · 🐬 **MySQL 8** · 🔴 **Redis 7** · 🐳 **Docker** · 📄 **MIT License**

[![CI](https://github.com/super-ZXQ/ai-commerce-intelligence-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/super-ZXQ/ai-commerce-intelligence-platform/actions/workflows/ci.yml)
[![Release](https://github.com/super-ZXQ/ai-commerce-intelligence-platform/actions/workflows/release.yml/badge.svg)](https://github.com/super-ZXQ/ai-commerce-intelligence-platform/actions/workflows/release.yml)

## 简介

基于 **102,287 条公开电商订单样本**，完成从数据清洗、特征工程到多维分析与可视化的完整链路。系统采用前后端分离架构，提供交互式 BI 看板、分节点 Agent 智能查询、RAG 业务知识检索、RESTful API、RFM 用户画像和实时监控。

## 功能概览

| 模块 | 技术栈 | 功能 |
|------|--------|------|
| **BI 数据看板** | Streamlit + Plotly | 交互式数据大屏，多维度交叉筛选 |
| **AI 分析助手 + RAG** | LangGraph + LangChain + SQLGlot + Chroma | 分节点路由、结构化 Text-to-SQL、一次纠错、多轮会话、引用与公开轨迹 |
| **FastAPI 后端** | FastAPI + SQLAlchemy | 34 个 API 操作，含认证/限流/缓存/监控/RFM 与订单事件摄入 |
| **订单事件摄入** | MySQL 事务 + 幂等账本 | 创建/退款/取消事件、版本水位乱序保护、精准缓存失效与新鲜度查询 |
| **RFM 用户画像** | SQLAlchemy + 量化分群 | R/F/M 五分位评分 → 8 类用户分群 + 流失预警 |
| **数据分析 Notebook** | Jupyter + Pandas | 数据清洗、销售/时间/用户多维分析 |
| **RAG 业务知识库** | Chroma + BGE-small-zh-v1.5 | 6 份业务文档（术语/数据字典/KPI/规则/API/黄金查询）向量检索 |
| **测试 & 评估** | pytest + 离线/真实模型评估器 | 252 项自动化测试（隔离 MySQL 实收全绿）+ 135 条路由回归（100 curated + 25 鲁棒性 + 10 留出）+ 15 条词法检索回归 + Text-to-SQL 评估 + 15 条 GLM 真实评测 |

## 在线演示

| 应用 | 链接 | 说明 |
|------|------|------|
| **Docker 统一入口** | `http://localhost/` | 导航页（默认入口） |
| **BI 数据看板** | `http://localhost/BI/` | 交互式数据大屏 |
| **AI 分析助手** | `http://localhost/ai/` | 自然语言查数 + RAG 知识库 |
| **API 文档** | `http://localhost/docs` | Swagger UI |
| **API 体验页** | `http://localhost/demo` | 可视化大屏 + AI 查询 |
| **系统监控** | `http://localhost/monitor` | 实时监控面板 |
| **健康检查** | `http://localhost/health-panel` | 组件健康状态 |

## 界面预览

### AI 分析助手

![AI 分析助手：模型连接、RAG、对话入口](docs/screenshots/ai-assistant.png)

### BI 销售看板

![BI 销售看板：核心指标、趋势和平台占比](docs/screenshots/bi-dashboard.png)

## 快速开始

### Docker Compose 一键部署（推荐）

```bash
git clone https://github.com/super-ZXQ/ai-commerce-intelligence-platform.git
cd ai-commerce-intelligence-platform

cp deploy/.env.example .env
# 编辑 .env 填入四类 MySQL 密码、JWT Secret、LLM API Key

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
├── ea-db-bootstrap → 一次性创建最小权限数据库账号
├── ea-mysql        → MySQL 8.0 (Docker 内网 :3306) + 自动建表
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
| `/health` `/health/detailed` `/metrics` | backend:8000 | 公开健康检查与 Prometheus 指标 |
| `/demo` `/monitor` `/health-panel` | backend:8000 | 系统页面 |

部署模式下仅 Nginx 对宿主机暴露 80 端口，业务服务、数据库和缓存只通过 Docker 内部网络通信。`db-bootstrap` 会为 API、AI、事件摄入和数据同步分别创建最小权限账号；API 与 AI 仅有查询权限，`ea_events` 仅能更新 `orders` 与 `order_events`。后端启动前会按 CSV SHA-256 校验订单版本；CSV 是公开初始化快照，不是运行期唯一数据入口。新增订单、退款和取消通过事件 API 写入 MySQL。订单 CSV 导出按 5,000 行分块查询并流式返回，不会将整个导出文件同时留在应用内存中；Excel 因工作簿格式限制仍需在内存中生成。RFM 完整用户明细使用后端进程内有界快照复用，Redis 只保存小型汇总结果，避免产生十几 MB 的单 Key。AI 助手的 Chroma 数据与 RAG 指标快照写入命名卷 `chroma_data`，后端以只读方式挂载同一卷用于监控。Nginx 已配置 `proxy_http_version 1.1`、`Upgrade` 和 `Connection` 头，支持 Streamlit WebSocket，避免反向代理后页面白屏。

### 本地开发

```bash
git clone https://github.com/super-ZXQ/ai-commerce-intelligence-platform.git
cd ai-commerce-intelligence-platform

python -m venv .venv && .venv\Scripts\activate

pip install -r requirements.txt
pip install -r ai-ecommerce-assistant/requirements.txt
pip install -r backend/requirements-dev.txt
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
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请设置强密码

LLM_API_KEY=sk-你的DeepSeekKey
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
```

也可使用智谱 OpenAI 兼容接口；Key 只写入本机 `.env`，不要提交：

```env
LLM_API_KEY=<你的临时 Key>
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
LLM_MODEL=glm-4.7-flash
```

**`ai-ecommerce-assistant/.env`**：

```env
LLM_API_KEY=sk-你的DeepSeekKey
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat

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

FastAPI 与 Streamlit 只负责各自的配置和展示，统一调用 `agent_core.AgentRuntime`。执行轨迹只记录节点动作、耗时和脱敏错误类型，不包含模型隐藏思维链。

```text
用户请求
  → 输入安全检查
  → 加载最近 6 轮会话（用户 + thread_id 隔离，TTL 30 分钟）
  → 确定性意图路由
      ├─ blocked       → 安全拒绝
      ├─ clarification → 澄清问题
      ├─ knowledge     → Top 3 RAG 检索 ──────────────┐
      ├─ data          → Schema 读取 → SQL 生成       │
      └─ hybrid        → RAG 检索 → Schema → SQL 生成 │
                                      ↓              │
                              SQLGlot AST 只读校验     │
                                      ↓              │
                            LIMIT 500 + 10 秒超时       │
                                      ↓              │
                         只读执行 → 最多一次 SQL 纠错   │
                                      └──────┬───────┘
                                             ↓
                                  答案合成 + 来源引用
                                             ↓
                                  保存会话与公开执行轨迹
```

安全边界包括输入拦截、SQL AST 校验、SQLAlchemy 执行前拦截和数据库只读账号。SQL 仅能访问 `orders`，禁止多语句、写操作、锁表、`SLEEP`、`BENCHMARK`、`LOAD_FILE`，并自动限制最多 500 行。

### 关键技术决策与取舍

- **共享 Runtime**：FastAPI 与 Streamlit 注入同一张 LangGraph，避免两套 Agent 行为漂移；代价是核心层必须保持 Web 框架无关。
- **确定性路由优先**：安全、澄清和常见业务意图不消耗模型额度，结果可重复；复杂语义仍受规则覆盖范围限制。
- **JSON Mode + Pydantic**：兼容不同 OpenAI 协议提供方，同时拒绝 Markdown SQL；结构正确不代表结果正确，因此还要执行结果评测。
- **四层 SQL 防护**：输入规则、SQLGlot AST、SQLAlchemy 执行拦截、数据库只读账号互相独立，单层失效不会直接获得写权限。
- **数据库侧执行时限**：MySQL 查询注入 `MAX_EXECUTION_TIME`，由数据库终止慢查询并释放连接；`asyncio.wait_for` 只保留为宽限兜底。SQLite 演示模式不声称具备同等数据库侧保护。
- **真实结果评测**：候选 SQL 与参考 SQL 比较 102,287 行数据上的结果集，不要求字符串相同；模型评测显式运行，不在 CI 偷用 Key。
- **有界降级**：Redis 不可用时回落到最多 1,000 个内存会话；SQL 只纠错一次，避免无限 Agent 循环。
- **编排边界**：当前是固定 LangGraph 工作流编排，模型负责结构化 SQL 与答案生成，不声称具备自主工具规划或开放式多 Agent 协作能力。

### 架构演进：从 ReAct 工具调用到确定性工作流

Agent 层经历过一次重构，两个版本都保留在 Git 历史中：

| | v1（已归档） | v2（当前） |
|---|---|---|
| 决策方 | 模型自主选择工具 | 确定性规则路由 |
| 检索触发 | 模型调用 `query_business_knowledge` | 工作流按意图（`knowledge` / `hybrid`）调度 |
| 来源还原 | 从 LangChain `intermediate_steps` 反解 | `AgentSource` 直接写入图状态，无需反解 |
| 回归基线 | 难以建立（同一问题多次执行路径不稳定） | 135 条路由回归集（100 curated + 25 鲁棒性 + 10 留出），可防退化 |
| 代码位置 | `rag/tools.py`、`rag/prompts.py`、`rag/extractor.py` | `agent_core/runtime.py`、`agent_core/routing.py` |

**为什么收敛**：v1 让模型自主决定何时检索，灵活性更高，但带来三个问题——

1. 同一问题多次执行的工具路径不稳定，无法建立可复现的回归基线；
2. 模型倾向保守地"先查一次知识库"，简单的数据问题也要多付一次检索延迟；
3. 来源还原依赖 LangChain 的 `intermediate_steps` 内部结构，框架升级时易碎。

**v2 的取舍**：路由规则化后，安全拦截、澄清和常见业务意图不消耗模型额度，
结果可重复，并能用 100 条评测集防退化（多数类基线 52/100，当前 100/100）；
检索改由工作流按意图调度，`knowledge` 分支完全不接触数据库，
`data` 分支不加载知识库，互不拖累。

**代价**：放弃模型自主工具规划能力。规则覆盖不到的长尾表述会误判意图；
且 100/100 只用于防回归——数据集与规则同仓维护，**不代表未见问题上的泛化准确率**。

> 📐 双检索器（向量检索 vs 词面基线）并存的取舍与收敛计划见 [ADR 0001](docs/adr/0001-dual-retriever.md)。

**A/B 量化**：为验证这一取舍而非仅口头声明，`agent_core/eval/ab_routing_eval.py`
将规则路由（A 侧）与 LLM 路由（B 侧，`agent_core/llm_router.py`）放在同一评测集上对照：

| 路由器 | curated（100 条） | robustness（25 条 dev，参与调优） | heldout（10 条留出，gold 冻结） | 单条延迟 | 成本 |
|--------|------------------|--------------------------------|------------------------------|----------|------|
| A：确定性规则（生产在用） | **100%** | 加固前 **24%** → 加固后 **96%** | **60%**（真实泛化） | ~0.007ms | ¥0 |
| B：LLM 路由（对照实现） | 需配置 `LLM_API_KEY` 补齐（未配置时明确跳过，**不伪造数字**） | 同左 | 同左 | 网络往返级 | token 计费 |

结论：规则路由在**当前评测集分布下**具有确定性、零成本和可解释优势。按 dev 集失败模式加固后，
口语改写（"咋算"）、隐私变体（"电话"）、倒序写操作（"把 orders 表删了"）与注入改写均可正确拦截/分类；
留出集 60% 如实披露残余缺口（导出客户资料、收货地址、记录行写操作、泛宾语盘点）——
**调优集与留出集分开报告，避免按考卷改答案**。这也是保留 LLM 路由对照与
混合路由（规则优先 + LLM 兜底）演进路径的原因。详见 `agent_core/eval/ab_routing_report.md`。

**查看 v1 实现**（归档 tag）：

```bash
git show archive-react-v1:ai-ecommerce-assistant/rag/tools.py
git show archive-react-v1:ai-ecommerce-assistant/rag/prompts.py
git show archive-react-v1:ai-ecommerce-assistant/rag/extractor.py
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

### RAG 与 Agent 模块组成

| 文件 | 职责 |
|------|------|
| `embeddings.py` | BGE-small-zh-v1.5 Embedding 工厂（多线程单例 + 设备自动检测） |
| `vector_store.py` | Chroma 封装：增删查改、阈值过滤、元数据过滤、增量构建 |
| `retriever.py` | LRU + TTL 缓存、超时保护、上下文格式化、统计埋点 |
| `agent_core/runtime.py` | LangGraph 分节点工作流、一次纠错、超时和公开轨迹 |
| `agent_core/model_adapter.py` | OpenAI 兼容模型、结构化 SQL 输出和 Token 元数据 |
| `agent_core/session.py` | Redis 优先、有界内存降级的隔离会话存储 |
| `agent_core/sql_safety.py` | SQLGlot AST 白名单与自动 LIMIT |
| `agent_core/rag/` | 后端零外部服务 Markdown 检索基线 |
| `ai-ecommerce-assistant/rag/` | Streamlit 的 Chroma/BGE 检索、缓存和监控兼容层 |
| `metrics.py` | 跨进程 stats 共享：原子写入 JSON、综合 Prometheus 渲染、结构化事件日志（JSONL 可选） |

### 决策树

- 定义、公式、规则问题 → `knowledge`，只执行 RAG，不访问数据库
- 数量、排名、趋势问题 → `data`，读取 Schema 后生成并执行只读 SQL
- 同时需要口径和实际值 → `hybrid`，先 RAG、再 Schema 与 SQL
- 模糊问题 → `clarification`；隐私、提示词注入和写库请求 → `blocked`

回答时引用业务知识，**UI 自动折叠展示**"📚 参考知识"面板（来源、章节、相关度、200 字预览），不污染主回答。

## 检索监控与指标埋点

RAG 模块内置轻量级监控层，把"检索质量 + 性能 + 用户反馈"三类信号汇总到 JSON / JSONL 文件，FastAPI 再以 Prometheus 格式暴露给监控系统。**不依赖 Redis / Kafka，本地开发 + Docker 双环境统一**。

### 1. 三类指标

| 维度 | 来源 | 字段 |
|------|------|------|
| 检索器（retriever） | `Retriever.get_stats()` | `cache_hits` / `store_hits` / `timeouts` / `no_results` / `avg_latency_ms` / `hit_rate_pct` / `score_buckets` / `total_queries` |
| 工具调用（tool） | `metrics.record_tool_call()` | `tool_call_count` / `tool_no_hit_count` / `tool_error_count`（v1 ReAct 由模型触发；v2 工作流下无生产调用方，保留为监控 API，恒为 0） |
| 结构化事件 | `metrics.log_event()` | `ts` / `event` / `query` / `top1_score` / `hits` / `ms` / `cache_hit` / `source` |

### 2. 端点（无需鉴权）

| 路径 | 格式 | 说明 |
|------|------|------|
| `GET /api/monitor/rag-stats` | JSON | RAG 综合快照；未启动 AI 助手时返回 503 |
| `GET /api/monitor/rag-stats.prom` | Prometheus text | 11 个指标（counter + gauge） |
| `GET /metrics` | Prometheus text | 后端 / 业务指标（与 RAG 并列） |

示例：

```bash
# JSON 快照
curl http://localhost:8000/api/monitor/rag-stats

# Prometheus 抓取（后端请求指标 + RAG 指标）
curl http://localhost:8000/metrics

# 仅抓取 RAG 指标
curl http://localhost:8000/api/monitor/rag-stats.prom
```

返回示例（节选）：

```
# HELP rag_query_total Total RAG queries (cache + store)
# TYPE rag_query_total counter
rag_query_total 42

# HELP rag_score_bucket Top1 retrieval score distribution
# TYPE rag_score_bucket counter
rag_score_bucket{range="[0.4,0.6)"} 5
rag_score_bucket{range="[0.8,1.0]"} 12

# HELP rag_tool_call_total Agent invoked the knowledge tool
# TYPE rag_tool_call_total counter
# v2 收敛为确定性工作流后，工具调用不再由模型发起，该计数恒为 0
rag_tool_call_total 0
```

### 3. 落盘策略

- **综合 stats（JSON）**：`retriever` 在每次检索后节流（5s）调用 `dump_stats()`，把 retriever stats + tool stats 合并后**原子写入**（tmp + `os.replace`）`ai-ecommerce-assistant/data/rag_stats.json`。Docker 中该目录由 AI 助手读写、后端只读共享。
- **事件流（JSONL，可选）**：默认关闭，避免拖慢。设置环境变量 `RAG_EVENTS_LOG=1` 即开启 `data/rag_events.jsonl` 追加写入；每行一条结构化 JSON 事件，可直接被 Vector / Filebeat / Promtail 采集。
- **用户反馈（JSONL，append-only）**：聊天界面的 👍/👎 按钮触发 `record_feedback()`，追加写入 `ai-ecommerce-assistant/eval/feedback.jsonl`；用于线下分析"检索误召回率"。

### 4. 关键设计点

- **节流 + 原子写入**：避免每次检索都 IO；用 `tmp + os.replace` 保证读到一致快照（不会读到半截 JSON）。
- **Top1 score 桶分布**（5 桶：`[0,0.2)` / `[0.2,0.4)` / `[0.4,0.6)` / `[0.6,0.8)` / `[0.8,1.0]`）：监控阈值 `score_threshold=0.4` 是否合理。若 `[0,0.2)` 占比飙升 → 阈值过低；若 `[0.8,1.0]` 极少 → 检索质量退化。
- **缓存命中率**：超过 80% 说明缓存命中良好；持续 0% 说明缓存键设计有误或 TTL 过期。
- **结构化事件 logger 独立通道**（`rag.events`，propagate=False），便于 ELK / Loki 聚合；本仓库内置的 JSONL 落盘是兜底方案。

## 运维与 CI/CD

### 1. GitHub Actions

| Workflow | 触发 | 职责 |
|----------|------|------|
| `.github/workflows/ci.yml` | PR / push main | AI 助手 88 项测试（Python 3.12 + 3.13）+ 后端 164 项测试（MySQL 8）+ Ruff + 编译 + 离线 Agent 评测；日志管道启用 `pipefail`，测试失败不会被 `tee` 掩盖 |
| `.github/workflows/docker-smoke.yml` | PR / push main | 空卷构建全栈、断言 102,287 行、验证 7 个入口与 WebSocket 握手 |
| `.github/workflows/release.yml` | push main / tag `v*.*.*` / 手动 | 构建 backend / streamlit / ai-assistant 三个 Docker 镜像，**多架构**（linux/amd64 + linux/arm64），推送到 `ghcr.io/super-zxq/ai-commerce-intelligence-platform-{backend,streamlit,ai-assistant}` |

**Tag 策略**（由 `docker/metadata-action` 自动管理）：

| 触发 | tag 示例 |
|------|----------|
| push main | `:latest`、`:main-<short-sha>` |
| push tag `v1.0.3` | `:1.0.3`、`:1.0`、`:latest` |
| pull request | `:pr-123`（不推送） |
| workflow_dispatch | `:latest`、`:main-<short-sha>` |

**使用预构建镜像**（替换 docker-compose 中 build）：

```yaml
services:
  backend:
    image: ghcr.io/super-zxq/ai-commerce-intelligence-platform-backend:1.0.3
    # ... 其余配置不变
```

后端 CI 通过 [`backend/scripts/init_ci_schema.py`](backend/scripts/init_ci_schema.py) 初始化 schema 和最小测试数据；测试输出与初始化日志会作为 artifact 保留 7 天，便于排查失败。

### 2. 全栈健康检查脚本

`scripts/health_check.py` —— 单文件脚本，本地和 CI 都可调用。

```bash
# 默认检查全栈（localhost 各端口）
python scripts/health_check.py

# Docker Compose（Nginx 统一入口）
python scripts/health_check.py --docker

# CI 用：失败时非零退出
python scripts/health_check.py --fail-on-error --output report.json

# 自定义目标
python scripts/health_check.py \
  --backend http://api.example.com \
  --bi https://bi.example.com \
  --ai https://ai.example.com \
  --db "mysql+pymysql://user:pass@host:3306/db" \
  --redis redis://host:6379/0

# 只跑部分检查
python scripts/health_check.py --checks backend,db --fail-on-error
```

**检查项**（每项独立输出 latency_ms + status + error）：

| 名称 | 检查方式 | 期望 |
|------|----------|------|
| `backend:/health` | GET | status 字段存在 |
| `backend:/health/detailed` | GET | 200 |
| `backend:/api/monitor/services-status` | GET | 200 |
| `backend:/api/monitor/rag-stats` | GET | 200（503 = AI 助手未启动） |
| `bi:streamlit` | GET `/_stcore/health` | 200 |
| `ai:streamlit` | GET `/_stcore/health` | 200 |
| `mysql:select_1` | pymysql `SELECT 1` | 连接成功 |
| `redis:ping` | redis-py `PING` | 返回 True |

**JSON 报告示例**：

```json
{
  "status": "ok",
  "checked_at": "2026-06-10T07:00:00+00:00",
  "total": 8, "ok": 8, "warn": 0, "error": 0, "skipped": 0,
  "results": [
    {"name": "backend:/health", "target": "http://localhost:8000/health",
     "status": "ok", "latency_ms": 12.3, "timestamp": "..."}
  ]
}
```

### 3. 升级流程

```bash
# 1. 本地通过所有测试
cd ai-ecommerce-assistant && python -m pytest tests/ -v
cd ../backend && python -m pytest tests/ -v

# 2. 提交 + 推送（触发 CI）
git add -A
git commit -m "feat: xxxxx"
git push origin main

# 3. CI 通过后打 tag（触发镜像构建 + 推送 ghcr.io）
git tag v1.0.3
git push origin v1.0.3

# 4. 生产拉新镜像
docker compose pull && docker compose up -d
```

## 测试与评估

### 自动化测试（252 项，实收全绿）

```bash
# 后端（164 项，完整运行需要 MySQL）
python -m pytest backend/tests/ -v

# AI/RAG（88 项）
python -m pytest ai-ecommerce-assistant/tests/ -v
```

**测试覆盖：**

后端（164 项）：
- `test_agent_runtime_core.py` — 分支工具顺序、SQL AST、一次重试、用户会话隔离（含重新生成的末轮回删）、Redis 降级、Token `null` 语义和 API 兼容
- `test_agent_workflow.py` / `test_agent_evaluation.py` — 意图路由和离线发布门槛；安全短路与工具顺序由 Runtime 测试覆盖
- `test_routing_ab.py` — 路由 A/B 三层基线回归：curated ≥97% / dev ≥92%（加固后）/ 留出集 ≥50%（冻结 gold，真实泛化下限）
- `test_agent_streaming.py` — 流式步骤与 Token 增量下发，返回值与非流式调用一致
- `test_live_model_evaluation.py` — 真实评测集约束、结果集比较与共享 Schema 描述
- `test_api.py` / `test_core_safety.py` / `test_order_event_producer.py` / `test_security_hardening.py` / `test_mysql_execution_timeout.py` — API 契约、订单事件幂等/乱序/事务回滚、缓存标签失效、合成流可复现、SQL 只读防护（含 FOR SHARE/NOWAIT 锁子句）与数据库侧执行时限

AI/RAG（88 项）：
- `test_vector_store.py` — Chroma 增删查改（fake embedder，不依赖真实模型）
- `test_retriever.py` — 缓存/TTL/LRU/阈值/超时/stats，以及**检索异常不写缓存**回归
- `test_rag_metrics.py` — score_buckets / 原子写入 / JSON 加载 / 结构化事件 / JSONL 落盘 / 反馈写文件
- `test_build_kb.py` — 文档切分函数（doc_type、doc_id、滑动窗口）
- `test_lazy_rag.py` — 93MB Embedding 模型懒加载；导入 `rag` 包不触发下载；status 引用始终可读到最新状态
- `test_render_highlights.py` — 答案渲染纯函数：HTML 实体/行内代码/代码块中的数字不被高亮破坏
- `test_sql_eval.py` — Text-to-SQL 评估纯函数（gold SQL 过生产 AST 闸门、关键词归一化匹配），不依赖 API Key

AI/RAG 测试不依赖真实 BGE 模型，用 `tests/conftest.py` 里的 `FakeEmbeddings` 生成确定性归一化向量。

**测试规模（实收，非估填）** — 2026-09-18 在本仓库 `.venv`（Python 3.12）与隔离 MySQL schema 实测：

| 套件 | pytest 收集 | 结果 |
|------|------------|------|
| `backend/tests` | 164 | **164 passed**（14s） |
| `ai-ecommerce-assistant/tests` | 88 | **88 passed**（11s） |
| **合计** | **252** | **252 passed / 0 failed** |

数字为参数化展开后的 pytest 实收用例数，非静态函数计数。
后端套件需本地 MySQL 可连接（CI 中由 `init_ci_schema.py` 建表后运行）；AI/RAG 套件不依赖网络与真实模型。

前端：`backend/static/index.html` 拆分为 `css/studio.css` + `js/studio.js`，并经 Impeccable detector（0 critical/0 error）与桌面三视口（1366×768 / 1440×900 / 1920×1080）验收 `badCount=0`。

### 安全边界

- LLM 生成 SQL 在显式执行和 SQLAlchemy Engine 执行前都会经过单语句只读校验，拦截写操作、文件读取/导出、存储过程和延时函数。
- Docker 镜像构建时升级到 `pip>=26.1.2`；Embedding 依赖使用 `transformers 5.x`，避开已知的 4.x checkpoint 反序列化漏洞。
- Chroma 仅以嵌入式本地库运行，不启动或暴露 Chroma Server API。当前 1.5.9 的四项未修复公告均要求攻击者访问 Server API（含认证、租户权限或集合更新端点），在本部署中不可达；不要额外启动 Chroma HTTP 服务。

### 离线 Agent 评测（不调用付费模型）

```bash
python -m agent_core.evaluation
```

- 路由集 100 条：25 条基础 SQL、20 条时间/聚合/同比/排序、15 条知识、15 条混合、15 条多轮/歧义、10 条安全问题。
- 词法检索回归集 15 条：`agent_core` 的零外部服务检索器使用字符 bigram，报告 Recall@3 100%、MRR 0.7444；该数字**不是 BGE/Chroma 向量检索效果**。
- 路由规则回归集为 100/100；多数类恒猜 `data` 的基线是 52/100，当前规则提升 48 个百分点。数据集与规则同仓维护，因此只用于防回归，不代表未见问题上的泛化准确率。
- **路由 A/B 对照（v2 规则 vs LLM）**：25 条 dev 鲁棒性集规则侧加固前 6/25 → 加固后 24/25；另设 10 条留出集（gold 冻结、不参与调优）实测 6/10 度量真实泛化，残留缺口（导出客户资料/收货地址/记录行/泛宾语盘点）入加固 backlog；`agent_core/llm_router.py` 为 B 侧对照实现，配置 `LLM_API_KEY` 后运行 `python agent_core/eval/ab_routing_eval.py` 补齐 LLM 数字（未配置时明确跳过，不伪造）。报告见 `agent_core/eval/ab_routing_report.md`。

### 兼容 RAG 评估（20 条 gold_qa）

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

评估集覆盖 10 条知识问答（术语/公式/字段/规则/API）+ 10 条数据查询。报告自动生成 `eval/report.md`（人类可读）+ `eval/report.json`（机器可读）。

**关键指标：**
- 命中率（命中含 expected_keywords 的文档数 / 总数）
- 实际命中 doc_type 与期望 doc_type 一致性
- Top1 score
- 单次检索耗时

### Text-to-SQL 评估（10 条 data 用例）

数据查询（d01-d10）的 SQL 生成能力由 `eval/run_sql_eval.py` 单独评估，补齐"检索有指标、生成靠感觉"的缺口：

```bash
cd ai-ecommerce-assistant

# 需要 LLM_API_KEY；生成 → 生产同一道 sqlglot AST 只读闸门 → gold 关键词命中
python eval/run_sql_eval.py

# AST 校验通过后落库执行，记录行数/执行错误
python eval/run_sql_eval.py --execute

# 落盘 JSON + Markdown 报告
python eval/run_sql_eval.py --output eval/sql_eval_report.json
```

**关键指标：**
- AST 只读校验通过率（与生产 `validate_and_limit_sql` 同一道闸门）
- gold 关键词全命中率（归一化大小写/空白/反引号后匹配）
- 综合通过率与逐条失败明细（生成的 SQL、未命中关键词）
- 可选：实际执行行数

评估核心逻辑（`check_sql_keywords` / gold SQL 过闸门）由 `tests/test_sql_eval.py` 在无 API Key 环境下覆盖。

### 真实模型评测

真实模型评测需要用户显式配置 API Key 和 `--allow-network`，不会在 CI 中消耗额度。最新可审计快照使用智谱 `glm-4-flash-250414`，在完整 102,287 行数据上的 15 条评测中通过 14 条（93.33%）：结构化输出 100%、SQL 结果正确率 90%、知识关键词覆盖率与引用完整率均为 100%，P50/P95 延迟为 1,508/8,941 ms，共使用 6,894 Tokens。

评测比较候选 SQL 与参考 SQL 的实际结果集，不使用 SQL 字符串相等或模型自评。唯一失败项及逐条输出保留在 [`docs/evaluation/`](docs/evaluation/README.md)；15 条小样本不代表所有真实问题的总体准确率。

### 已知限制与降级

- 未配置模型 Key：知识问题仍可返回检索来源；数据问题返回明确配置提示，不伪造结果。
- Redis 不可用：自动降级到最多 1,000 个会话的内存存储；会话保留最近 6 轮、TTL 30 分钟。
- RAG 无命中：继续执行可用分支并返回空来源，不伪造引用。
- SQL 校验或执行失败：仅使用脱敏错误纠正一次，第二次失败后停止循环并返回明确错误。
- 真实模型评测仅 15 条且输出具有随机性；当前快照有 1 条 SQL 在一次纠错后仍失败。
- 本地演示可使用 SQLite；生产与完整 CI 使用 MySQL 8，只读账号是安全边界的一部分。

## 项目结构

```
ai-commerce-intelligence-platform/
├── agent_core/                   # FastAPI / Streamlit 共享 Agent Runtime
│   ├── runtime.py                # LangGraph 分节点工作流
│   ├── routing.py                # 确定性意图路由（A/B 的 A 侧）
│   ├── llm_router.py             # LLM 意图路由对照（A/B 的 B 侧）
│   ├── model_adapter.py          # 结构化模型适配
│   ├── live_evaluation.py        # 显式联网的真实模型评测器
│   ├── session.py                # Redis / 内存会话
│   ├── sql_safety.py             # SQL AST 安全层
│   ├── rag/                      # 共享 Markdown 检索
│   └── eval/                     # 路由、RAG 与真实模型评测集
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
│   ├── utils/                    # 工具（认证/缓存/限流/SQL 只读校验）
│   ├── static/                   # HTML 静态页
│   ├── sql/                      # SQL 脚本
│   ├── scripts/                  # CI / 工具脚本
│   │   ├── init_ci_schema.py     # CI 建表 + 5 条 fake orders seed
│   │   └── sync_orders.py        # CSV 哈希校验 + 原子换表
│   ├── tests/                    # 164 项 API/事件集成测试
│   ├── requirements.txt          # 后端生产依赖
│   └── requirements-dev.txt      # 后端测试与静态检查依赖
├── streamlit_app.py              # BI 数据看板（Streamlit 多页面）
├── ai-ecommerce-assistant/        # AI 分析助手（含 RAG）
│   ├── app.py                    # 主应用
│   ├── build_knowledge_base.py   # 知识库构建脚本
│   ├── knowledge_base/           # 6 份业务知识 Markdown
│   ├── rag/                      # RAG 核心模块（检索由 agent_core 工作流调度）
│   │   ├── embeddings.py         # BGE Embedding 工厂
│   │   ├── vector_store.py       # Chroma 封装
│   │   ├── retriever.py          # 缓存/超时/格式化/埋点
│   │   └── metrics.py            # 跨进程 stats 共享 + Prometheus 渲染 + JSONL 事件
│   ├── tests/                    # 88 项 AI/RAG 测试
│   ├── eval/                     # 评估集 + 评估脚本
│   ├── data/chroma/              # Chroma 持久化目录
│   ├── pytest.ini                # pytest 配置
│   └── requirements.txt
├── .github/workflows/            # GitHub Actions CI/CD
│   ├── ci.yml                    # 测试流水线（PR 触发）
│   ├── docker-smoke.yml          # 空卷 Compose 冷启动验收
│   └── release.yml               # Docker 镜像构建（push main / tag 触发）
├── scripts/
│   └── health_check.py           # 全栈健康检查脚本（本地 + CI）
├── deploy/                       # 部署配置
│   ├── nginx.conf                # Nginx 反向代理
│   ├── redis.conf                # Redis 持久化配置
│   ├── mysql/
│   │   └── bootstrap-users.sh    # 创建最小权限数据库账号
│   └── .env.example              # 环境变量模板
├── data/                         # 数据文件
├── sql/                          # 建表/导入/分析脚本
├── notebook/                     # Jupyter 分析笔记本
├── docker-compose.yml            # Docker Compose 编排
├── Dockerfile                    # FastAPI 后端镜像
├── Dockerfile.streamlit          # Streamlit 镜像
├── requirements.streamlit.txt    # BI 镜像精简运行时依赖
└── requirements.txt              # Notebook / 本地数据分析依赖
```

## 技术栈

| 分类 | 技术 |
|------|------|
| 语言 | Python 3.12 |
| Web 框架 | FastAPI 0.110+ + Uvicorn |
| ORM | SQLAlchemy 2.0 (async) |
| 前端 | Streamlit 1.28+ + Plotly |
| AI | LangGraph + LangChain + OpenAI 兼容模型接口 |
| RAG | Chroma（向量库） + BGE-small-zh-v1.5（Embedding），由确定性工作流按意图调度 |
| 数据库 | MySQL 8.0 |
| 缓存 | Redis 7 |
| 反代 | Nginx |
| 容器 | Docker + Docker Compose |
| 测试 | pytest（164 项后端 + 88 项 AI/RAG，隔离 MySQL 实收全绿）+ 135 条路由回归（100 curated + 25 鲁棒性 + 10 留出）+ 15 条词法检索回归 + Text-to-SQL 评估 + 15 条 GLM 真实评测 |

## License

MIT
