# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

主要访问者（按优先级）：

1. **招聘者 / HR**——筛选实习候选人，停留时间短，需要 15 秒内判断项目成色。
2. **技术面试官**——核验工程深度：会看架构、安全边界、测试与评测证据，可能对照源码。
3. **Agent / LLM 开发者**——评估 LangGraph 工作流、Text-to-SQL、RAG 的实现质量。
4. **源码审阅者**——从 GitHub 进入，核对 README 声明与代码是否一致。

使用场景：实习岗位（Agent 开发、大模型应用开发、AI 工程、Python 后端、数据智能应用）的作品集评估。访问者多数从 GitHub 仓库或简历链接到达，首次访问没有上下文。

## Product Purpose

基于 **102,287 条公开电商订单样本**的 AI 商业智能分析平台，覆盖数据清洗、BI 可视化、分节点 Agent 智能查询、RAG 业务知识检索、RESTful API、RFM 用户分层与系统监控的完整链路。

存在的理由：证明作者能交付「真正实现了 Agent 工作流、RAG、Text-to-SQL、安全校验、可观察性和评测体系」的完整 AI 应用，而不是 Streamlit 课程作业或 LLM 套壳。

成功标准：首次访问者在 15 秒内理解——这是什么项目、Agent 实际执行了什么、为什么不是简单包装、有哪些可验证的工程证据、如何体验系统/查看源码/架构/API/评测。

## Positioning

相邻产品（普通数据看板、ChatGPT 套壳）无法如实复制的机制：

- **共享 Agent Runtime**：FastAPI 与 Streamlit 注入同一张 LangGraph 状态图（`agent_core/`），两端行为零漂移。
- **确定性意图路由优先**：安全、澄清与常见业务意图不消耗模型额度，结果可重复（路由回归集 135 条同仓维护：100 curated + 25 鲁棒性 + 10 留出）。
- **四层独立 SQL 防护**：输入规则 → SQLGlot AST 白名单 → SQLAlchemy 执行拦截 → 数据库只读账号，单层失效不获得写权限。
- **结果集级评测**：候选 SQL 与参考 SQL 在完整 102,287 行数据上比较实际结果集，不比字符串、不用模型自评；真实模型评测显式联网运行，快照可审计。
- **公开执行轨迹**：只记录节点动作、耗时与脱敏错误类型，不含模型隐藏思维链。

## Operating Context

- **运行方式**：Docker Compose 本地部署，Nginx 统一入口 :80；本地开发为三端口直连（后端 :8000 / BI :8501 / AI 助手 :8505）。
- **无公网部署**：作品集的「体验」发生在访问者自己拉起的本地环境中；首页 CTA 文案不得暗示存在公网在线 Demo。
- **评审路径**：GitHub 仓库（README、CI 徽章、docs/evaluation 快照）与本地运行的页面互相印证。
- **CI**：GitHub Actions 三条工作流（ci / docker-smoke / release），多架构镜像推送 ghcr.io。

## Capabilities and Constraints

已确认功能（与代码核实一致）：

- 分节点 LangGraph 工作流：input_safety → load_history → route → retrieve / load_schema → generate_sql → validate_sql → execute_sql → synthesize → save_session → finalize。
- 意图五分类：blocked / clarification / knowledge / data / hybrid。
- RAG：Chroma + BGE-small-zh-v1.5，6 份业务知识文档，Top-3 检索，阈值 0.4；后端另有零外部服务 Markdown 词法检索基线。
- SQL 安全：单语句只读、自动 LIMIT 500、MySQL `MAX_EXECUTION_TIME` 10 秒、最多一次纠错后停止。
- 会话：Redis 优先、有界内存降级（1,000 会话 / 6 轮 / TTL 30 分钟），按 owner + thread_id 隔离。
- 可观察性：request_id、intent、steps（含每步耗时）、sources、SQL、usage（Token 可为 null）、latency；Prometheus 指标端点。
- BI：销售总览（指标卡/趋势/平台/TOP10）+ RFM 三 Tab（概览/价值矩阵/群体洞察），8 类客户分群。
- API：32 个文档化操作（25 个 router + 8 个 app 级路由减 1 个 schema 外路由），JWT 认证、限流、缓存、监控。

持久产品约束（未来工作必须保留）：

- 执行轨迹永不包含隐藏思维链；Token 无数据时显示「不可用」，不伪造为 0。
- 模型 API Key 只存当前会话（Streamlit session_state），不进 Git/日志/URL/持久化。
- RFM 配色映射与评分语义（R 低分为高价值，F/M 高分为高价值）不得改动。
- 技术栈锁定：FastAPI + Streamlit + Plotly + 原生 HTML/CSS/少量 JS + Nginx；不引入 React/Vue/Three.js/新构建链。
- 路由保持：`/`、`/BI/`、`/ai/`、`/docs`、`/demo`、`/monitor`、`/health-panel`。
- 禁止无证据的宣传词：「99.99% Accuracy」「企业级」「生产级」「百万用户」「大幅提升效率」。
- 页面文案语言：**中文为主**（技术名词与产品名保留英文）。

## Brand Commitments

- 名称：**AI Commerce Intelligence Platform**（副标题方向：让 Agent 理解业务知识、生成安全 SQL，并将十万级电商订单转化为可验证的经营洞察）。
- 用户钉住的视觉方向（绑定约束）：「克制版 AI Runtime + 企业级数据工作台」——首页允许深色技术氛围，BI 为浅色数据工作台，AI 助手聊天主体清晰、执行轨迹区可用深色技术面板。
- 用户提供的建议基础色与字体方向（作为设计约束记录，非最终 token）：深色底 #0B0D10 / 表面 #13171D / 主文字 #F8FAFC / 次级 #94A3B8；品牌蓝 #3B82F6、Agent/RAG 紫 #8B5CF6、SQL/数据青 #14B8A6、安全拦截橙 #F97316；BI 底 #F6F8FC / 卡片 #FFF / 边框 #E2E8F0 / 主文字 #1E293B；中文 Noto Sans SC 或可靠系统栈，英文标题 Manrope 或 Inter，代码 JetBrains Mono，均需提供回退。
- RFM 企业配色映射（见 `streamlit_app.py` 与 AGENTS.md，不得修改）。

## Evidence on Hand

已从仓库逐一核实（2026-09-02）：

- `data/cleaned_orders.csv`：102,288 行（含表头）= **102,287 条订单** ✓
- 评测集：`agent_core/eval/agent_cases.jsonl` 100 条 curated + `routing_robustness.jsonl` 25 条（dev）+ `routing_robustness_heldout.jsonl` 10 条（留出）路由回归、`rag_cases.jsonl` 15 条词法检索回归、`live_model_cases.jsonl` 15 条真实模型评测 ✓
- 真实模型评测快照：`docs/evaluation/README.md` + `glm-4-flash-250414.json`——2026-09-01 运行，glm-4-flash-250414，14/15 通过（93.33%），结构化输出 100%，SQL 结果正确率 90%，知识关键词覆盖与引用完整率 100%，P50/P95 延迟 1,508/8,941 ms，Token 6,894；唯一失败项（整体客单价）保留在报告中 ✓
- 离线评测声明（README）：路由回归 100/100（多数类基线 52/100）；25 条 dev 鲁棒性集规则侧加固前 6/25 → 加固后 24/25；10 条留出集（gold 冻结）6/10 度量真实泛化；词法检索 Recall@3 100%、MRR 0.7444——属同仓防回归指标，不代表泛化准确率，引用时须带限定语。LLM 路由对照（`llm_router.py`）需配置 `LLM_API_KEY` 运行 `agent_core/eval/ab_routing_eval.py` 补齐，未配置不生成数字（不伪造）
- 自动化测试（2026-09-18 在本仓库 `.venv` 与隔离 MySQL schema 实收）：后端 164 项 + AI/RAG 88 项 = **252 项，pytest 全绿**。数字为参数化展开后实收用例数，非静态函数计数。后端需本地 MySQL 可连接，CI 由 `init_ci_schema.py` 建表后运行
- 架构演进：v1 的 LangChain ReAct 工具调用（`rag/tools.py`、`rag/prompts.py`、`rag/extractor.py`）已删除，归档于 git tag `archive-react-v1`；v2 为确定性工作流（检索由 `agent_core` 按意图调度）。详见 README「架构演进」
- API 操作数：34 ✓（原 32 个操作 + 订单事件摄入/新鲜度 2 个操作）
- 版本：v1.0.3（tag 存在；当前分支为 v1.0.3 + msgpack 修复）✓
- 知识库：6 份 Markdown（术语/数据字典/KPI/规则/API/黄金查询）✓
- 现有页面截图：`docs/screenshots/ai-assistant.png`、`docs/screenshots/bi-dashboard.png`
- 现有首页 `backend/static/index.html`：深色导航页 + 全屏粒子 + 6 张服务卡片 + GSAP——本次重设计的替换对象（旧视觉作为反参考）

**不得虚构**：客户/用户数量、生产部署、公网地址、性能提升百分比、未实收的测试数。

## Product Principles

1. **可验证优于宣传**——每个声明都要能指到代码、CI 或评测快照；无法核实的指标不上页面。
2. **机制可见优于功能罗列**——首屏直接展示真实 Agent 执行链路，而不是「大标题 + 功能卡片墙」。
3. **诚实降级**——无数据即显示「不可用」，失败项保留在证据中；体验入口如实描述运行方式。
4. **页面各尽其职**——`/` 说服、BI 操作、AI 助手操作、docs 阅读；不为风格统一牺牲任务效率。
5. **动效必须有产品意义**——只用于说明真实机制（节点点亮、指标计数、状态反馈），支持降级。

## Accessibility & Inclusion

- 必须支持 `prefers-reduced-motion`、页面失焦降级、窄屏动效降级。
- 键盘焦点可见；正文对比度达标；内容默认可见，不依赖动画才能理解流程。
- 窄屏（390px）无横向溢出。
