# 电商数据分析系统

![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-v1.1-green?logo=fastapi)
![Streamlit](https://img.shields.io/badge/Streamlit-BI看板-red?logo=streamlit)
![LangChain](https://img.shields.io/badge/LangChain-AI助手-orange?logo=langchain)
![MySQL](https://img.shields.io/badge/MySQL-8.0-orange?logo=mysql)
![License](https://img.shields.io/badge/License-MIT-green)

## 项目简介

基于 **10万+ 条电商真实订单数据**，完成从数据清洗、特征工程到多维分析与可视化的完整链路。系统采用前后端分离架构，提供交互式 BI 看板、AI 智能查询、RESTful API 服务和实时监控四大核心能力。

| 模块 | 技术栈 | 功能 |
|------|--------|------|
| **BI 数据看板** | Streamlit + Plotly | 交互式数据大屏，多维度交叉筛选 |
| **AI 分析助手** | LangChain + Qwen + MySQL | 自然语言提问，自动生成 SQL 并绘图 |
| **FastAPI 后端接口** | FastAPI + SQLAlchemy + LangChain | 23 个 RESTful API，含 JWT 认证/限流/缓存/监控 |
| **数据分析 Notebook** | Jupyter + Pandas | 数据清洗、销售/时间/用户多维分析 |

## 在线演示

| 应用 | 链接 | 说明 |
|------|------|------|
| **BI 数据看板** | [点击体验](https://ecommerce-analysis-system-cqd8tpywoxneg8n3wqexfm.streamlit.app) | Streamlit Cloud 在线部署 |
| **AI 分析助手** | 本地 `http://localhost:8502` | 自然语言查数，自动 SQL + 图表 |
| **API 文档** | 本地 `http://localhost:8000/docs` | Swagger UI 交互式文档 |
| **API 体验页面** | 本地 `http://localhost:8000/demo` | 可视化数据大屏 + AI 查询 |
| **系统监控面板** | 本地 `http://localhost:8000/monitor` | 可视化监控仪表盘 |
| **健康检查面板** | 本地 `http://localhost:8000/health-panel` | 组件健康状态与指标看板 |

---

## FastAPI 后端接口

为项目提供完整的 RESTful API 服务，支持外部系统（小程序、移动端、前端）实时调用数据。

### 核心功能（22 个接口）

| 模块 | 接口数 | 说明 | 认证 |
|------|--------|------|------|
| **认证系统** | 3 | 登录 / 刷新Token / 当前用户 | 公开 |
| **系统接口** | 6 | 首页 / 健康检查 / 体验页 / 监控面板 / 健康面板 / API文档 | 公开 |
| **订单查询** | 3 | 列表（分页+排序）、详情、多条件筛选 | 公开 |
| **商品与用户** | 2 | 商品销售排名、用户消费排名 | 公开 |
| **数据分析** | 5 | 销售总览(缓存)、趋势、热销商品、用户行为(缓存)、平台分析 | 公开 |
| **AI 助手** | 1 | 自然语言 -> 自动 SQL -> 返回结果 | 需 JWT |
| **数据导出** | 2 | CSV / Excel 格式导出（分批查询防 OOM） | 公开 |
| **监控** | 2 | 实时指标统计、详细健康检查 | 公开 |

### 技术架构

```
┌─────────────────────────────────────────────────────┐
│              FastAPI Application (v1.1)              │
├──────────┬──────────┬──────────┬──────────┬────────┤
│  auth/   │ orders/  │products  │analytics │   ai/  │
│  routes  │  routes  │  routes  │  routes  │export/ │
│          │          │          │          │monitor/│
├──────────┴──────────┴──────────┴──────────┴────────┤
│              Services Layer                         │
│  order_service │ analytics_service │ ai_service     │
├─────────────────────────────────────────────────────┤
│         SQLAlchemy Async (aiomysql)                 │
│              MySQL Connection Pool (10+20)           │
├─────────────────────────────────────────────────────┤
│  JWT Auth  │ Rate Limit │ Cache │ Monitor           │
└─────────────────────────────────────────────────────┘
                    ↕
            ┌───────────────┐
            │  LangChain    │
            │  SQL Agent    │
            │  (Qwen LLM)   │
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
```

### 安全特性

| 特性 | 说明 | 配置位置 |
|------|------|---------|
| JWT Bearer Token 认证 | SHA256 密码哈希 + HS256 签名，24h 有效期 | `utils/auth.py` |
| API 请求频率限制 | 按路径差异化限流：登录 5次/min、AI 10次/min、导出 3次/2min | `utils/rate_limiter.py` |
| SQL 注入防护 | LIKE 查询自动转义 `%` `_` 特殊字符 | `services/order_service.py` |
| 敏感数据过滤 | AI 查询自动拦截隐私问题（手机号/身份证/密码） | `services/ai_service.py` |
| 响应缓存 | 销售总览 2min / 用户行为 3min，减少 DB 压力 | `utils/cache.py` |
| CORS 跨域控制 | 支持配置允许的来源列表 | `config.py` |
| 统一错误处理 | 全局异常捕获，生产环境隐藏堆栈 | `main.py` |

### 性能优化

| 优化项 | 详情 |
|--------|------|
| 异步全链路 | async/await 从路由到数据库（asyncio + aiomysql） |
| 连接池 | 10 核心连接 + 20 溢出连接，3600s 自动回收 |
| 分批导出 | 每批 5000 条，防止大数据量 OOM |
| 用户行为优化 | 5 次 SQL 合并为 2 次 |
| 缓存加速 | 热门查询结果内存缓存，TTL 可配 |
| 数据库索引 | 7 个关键索引（见 `sql/optimize_indexes.sql`） |
| 请求日志 | 自动记录方法/路径/状态码/耗时/限流信息 |

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
| JWT 认证 | python-jose | HS256 签名 + SHA256 密码哈希 |
| 数据处理 | pandas, numpy | pandas 2.0+ |
| 可视化 | matplotlib, plotly | 静态图 + 交互式图表 |
| BI 看板 | Streamlit | 交互式数据大屏 |
| AI 框架 | LangChain, LangChain-OpenAI | SQL Agent + LLM 调用 |
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
| API 接口 | 23 个 | 含认证/限流/监控 |
| 默认账号 | admin/admin123 | JWT 认证账号 |
| 测试用例 | 27 个 | 异步单元测试 |
| 支持平台 | 6 个 | APP/微信公众号/Web网站/淘宝/微信小商店/wap网站 |

---

## 项目结构

```
ecommerce_analysis/
├── backend/                        # FastAPI 后端服务
│   ├── main.py                     # 应用入口（CORS/中间件/路由注册/限流/日志）
│   ├── config.py                   # 配置管理（Pydantic Settings + JWT Secret）
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
│   │   └── monitor.py              # 监控路由（metrics/detailed health）
│   │
│   ├── services/
│   │   ├── order_service.py        # 订单逻辑（SQL注入防护/分页优化）
│   │   ├── analytics_service.py    # 分析逻辑（缓存装饰器/查询优化）
│   │   └── ai_service.py           # AI 查询逻辑（敏感过滤/重试/解析）
│   │
│   ├── static/
│   │   ├── index.html              # 统一入口导航页面
│   │   ├── monitor.html            # 系统监控可视化面板
│   │   └── health.html             # 健康检查可视化面板
│   │
│   ├── utils/
│   │   ├── auth.py                 # JWT 工具（生成/验证/密码哈希）
│   │   ├── rate_limiter.py         # 限流中间件（按路径差异化）
│   │   └── cache.py                # 内存缓存（@cached 装饰器/TTL）
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
├── ai-ecommerce-assistant/          # AI Streamlit 助手（LangChain + Qwen）
├── streamlit_app.py                 # Streamlit BI 看板主程序
├── app/bi_dashboard.py             # Streamlit Cloud 部署入口
├── data/cleaned_orders.csv         # 清洗后数据
├── notebook/                       # 5 个分析 Notebook
├── sql/                            # 建表/导入/高级分析 SQL
├── output/                         # 可视化输出
└── README.md
```

---

## 运行方式

### 快速启动

```bash
# 1. 安装依赖
pip install -r backend/requirements.txt

# 2. 配置 .env（已预置默认值，仅需确认 MySQL 密码）
# backend/.env 中修改 DB_PASSWORD 即可

# 3. 启动服务
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# 访问：
# 统一入口:    http://localhost:8000/
# Swagger 文档: http://localhost:8000/docs
# 体验页面:    http://localhost:8000/demo
# 监控面板:    http://localhost:8000/monitor
# 健康面板:    http://localhost:8000/health-panel
```

### 完整部署

```bash
git clone https://github.com/2681107509-dev/ecommerce-analysis-system.git
cd ecommerce_analysis

python -m venv .venv && .venv\Scripts\activate

pip install -r requirements.txt
pip install -r ai-ecommerce-assistant/requirements.txt
pip install -r backend/requirements.txt

# 启动三个服务（可同时运行）
streamlit run app/bi_dashboard.py                          # BI 看板 :8501
streamlit run ai-ecommerce-assistant/app.py                # AI 助手 :8502
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

# JWT 认证
JWT_SECRET=your-secret-key-change-in-production

# AI 助手（可选）
LLM_API_KEY=sk-你的阿里云Key
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus
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
httpx>=0.25.0
pytest>=7.4.0
pytest-asyncio>=0.21.0
```

---

## 迭代规划

- **v1.0**：17 个基础 API + Swagger 文档 + AI 查询 + 数据导出
- **v1.1**：JWT 认证 + API 限流 + 响应缓存 + 监控指标 + 27 个测试用例 + Postman Collection + 数据库索引优化 + 可视化监控面板 + 健康检查面板 + 统一入口导航
- **规划中**：接入 Redis 生产级缓存、Airflow 定时调度、Docker 容器化部署
