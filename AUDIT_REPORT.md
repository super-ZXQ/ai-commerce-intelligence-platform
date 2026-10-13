# 全仓库审计（2026-10-13）

审计分支：`codex/comprehensive-security-audit`。初始工作区干净。先阅读源码、测试、部署与评测脚本，再修改；基线根目录 `python -m pytest --tb=short -q`：**301 passed，30.48s**，原始输出 `output/audit-baseline.txt`（未纳入 Git）。通过现有测试不等于满足本任务新增的安全边界。

状态定义：CONFIRMED=源码或测试已确认；SUSPECTED=尚需环境/实验验证；NOT_REPRODUCED=针对性测试未复现；NOT_APPLICABLE=当前架构不适用。以下为修改前证据；修复进展见 FIX_REPORT.md，未验收项目不得视为完成。

| ID / 优先级 | 状态与源码证据 | 复现条件、根因与影响 | 最小方案与验证 |
|---|---|---|---|
| TASK-001 / P0 | CONFIRMED：`deploy/nginx.conf` 的 `/BI/`、`/ai/` 无 auth_request；两个 Streamlit 入口无登录门禁；API `/api/ai/query` 有 get_current_user | 匿名浏览器可进入页面；AI app 把服务端 Key 放入会话的密码输入框，用户改 Base URL 可发送服务端凭据到其他站点 | 网关 JWT 会话校验 + Streamlit 服务端重复校验；服务端 Key 不进入输入框、不跟随用户 URL；验证匿名/伪造/过期/有效会话，保留公开健康端点 |
| TASK-002 / P0 | CONFIRMED：`agent_core/sql_safety.py:validate_and_limit_sql` 只限制表名，无列权限；`db_schema.describe_table` 暴露全字段 | `SELECT user_name FROM orders`、`SELECT * FROM orders` 可通过；CTE 全局名称相减可遮蔽物理表检查；有数据库限定名前缀未拒绝 | AST scope 解析与列白名单，敏感字段仅允许去重计数和内部用户行为分组；外层不得输出标识；至少30条回归，真实数据库执行聚合 |
| TASK-003 / P0 | CONFIRMED：`model_adapter._client/test_connection` 无 URL/目标校验；app 模型设置接受任意 URL | 用户输入本机/内网 URL 触发服务器请求；SDK默认网络行为未明确；DNS二次解析存在风险 | HTTPS精确主机白名单、检查所有解析IP、每次连接校验并绑定已检查IP，禁止重定向与环境代理，显式超时；单测覆盖IP/重定向/DNS |
| TASK-004 / P0 | CONFIRMED：`backend/config.py:ai_database_url` 自动回退主账号；bootstrap AI有全业务库 SELECT 且不撤销历史授权 | 未配AI账号或老账号曾获写权限时安全边界失效；助手直接使用 DB_USER | 生产启动拒绝缺失/复用主账号，开发回退须显式开启；bootstrap撤销旧权限，迁移后仅授权 orders 必要字段 |
| TASK-005 / P0 | CONFIRMED：`order_event_service._apply_in_transaction` 用 `MAX(Order.id)+1`；ORM/新建DDL已有自增，baseline存量升级跳过主键 | 不同订单并发可分配相同ID；旧库未迁移自增；现有测试只覆盖2个相同事件 | 使用数据库自增，新增历史列迁移；真实MySQL 50/100不同订单、重复事件、同订单状态竞争、超时重试、回滚测试 |
| TASK-006 / P1 | CONFIRMED：`build_knowledge_base._make_doc_id` 用 content[:200]；delete错误被吞仍报告ok | 前200字不变的修改无法更新向量；删除失败产生旧向量；重复Chunk可能重复ID | 完整规范化内容SHA256、构建去重、删除失败显式失败、版本发布；FakeEmbedding+真实Chroma集成回归 |
| TASK-007 / P1 | CONFIRMED：`rag/retriever.py:_key` 缺 filter/knowledge_version，返回缓存对象本体 | 同查询不同Filter串结果；重建后TTL内旧结果；调用者修改缓存返回值污染其他请求 | 规范化JSON Key、知识版本、复制结果、失效世代；并发检索/失效测试 |
| TASK-008 / P1 | CONFIRMED：LIMIT仅处理大整数，不拒绝占位符/负数；全局CTE名称校验、未知函数未默认拒绝 | 未证明安全的SQL可通过；OFFSET无上限；JOIN仅限数量不限制笛卡尔积 | 复用TASK-002安全策略，限制AST节点/函数/查询规模/LIMIT/OFFSET/笛卡尔积；真实MySQL执行 |
| TASK-009 / P1 | CONFIRMED：API/助手 BUSINESS_CONTEXT 写死2025；`streamlit_app.load_data` 静态CSV且无TTL；API analytics ACTIVE口径，AI提示包含退款 | 新增/退款/取消后AI与BI/API不一致；AI缓存无数据版本且问题key无历史上下文 | 共享业务口径与实际时间范围；显式标注静态BI；动态数据库路径、移除易污染的最终答案缓存；事件后真实对照 |
| TASK-010 / P1 | CONFIRMED：`routes/ai.py` INFO记录完整query；app record_feedback写问题与答案片段；retriever记录问题 | 问题/结果可能含隐私或Key；反馈无限增长；app SQL异常日志含完整异常和trace | 默认只记长度、类别等元数据；反馈不存内容；轮转/保留策略；凭据/隐私回归 |
| TASK-011 / P1 | CONFIRMED：`agent_core/eval/ab_routing_eval.py` 只有A/B，无C；报告存在未经实验支持的“最优解/预期领先”结论 | 没有三方案同集实验；源码注释显示原heldout部分失败模式后来已参与安全规则加固，不能再称其完全独立 | 冻结新增独立集，A/B/C共享标签和确定性安全策略；真实调用记录准确率/Macro-F1/延迟/token（成本仅估算）；不再调原heldout |
| TASK-012 / P1 | CONFIRMED：API使用词面Markdown，UI使用BGE/Chroma；run_eval默认Mock，缺MRR/无答案及统一对照 | 两路径证据可能不同；Mock hit不能说明embedding效果 | 同一冻结检索集评测词面/BGE/Hybrid；记录Recall@3/MRR/拒答/延迟；无收益证据不切换默认检索器 |
| TASK-013 / P1 | CONFIRMED：`live_evaluation.py` 结果比较在SQLite；UI run_sql_eval为关键词，--execute仅统计行数 | 无真实MySQL结果集正确性验收；SQL生成成功不代表结果正确 | 冻结新集，真实MySQL执行候选/参考SQL并比较结果；记录重试、错误、token、P50/P95；禁止改gold掩盖失败 |
| TASK-014 / P1 | CONFIRMED（部分）：runtime已有SQL失败硬拒答，但knowledge无来源仍调用回答模型；提示词把检索内容直接拼入指令且无引用输出校验 | 无证据可产生业务断言；文档注入可影响模型；来源可以伪造 | 无证据硬拒答，提示分隔不可信数据，引用验证/可信来源追加；注入及失败路径回归 |
| TASK-015 / P2 | CONFIRMED（部分）：Retriever超时只统计未中断；VectorStore count/get_all_ids/delete吞异常；baseline创建索引吞异常；bootstrap迁移密码fallback与compose空密码不一致 | 故障可能被报成空库/成功；缓存跨worker失效、连接池及资源需实测；依赖漏洞状态SUSPECTED | 先根因修复，不重构全仓；Ruff、依赖审计、MySQL故障注入、Docker配置校验；报告未执行范围 |
| TASK-016 / P2 | CONFIRMED：已有README/ADR/评测报告，但与本任务验收范围不同 | 历史报告不能代替本次运行；架构/边界需反映修复后实现 | 更新README、架构/安全链路/实验限制、三份报告与面试总结，按模块提交 |

建议顺序：001（匿名模型/数据访问）→002/008（SQL边界）→003（SSRF/凭据外送）→004（数据库最小权限）→005（并发主键）→006/007（知识一致性）→009/010/014→011/012/013→015/016。保留现有 LangGraph、RFM、BI、订单事件与观测设施。

审计环境：Windows PowerShell、现有 `.venv`；Docker daemon可用，初始无运行容器；两个本地env存在模型配置（仅检查是否配置，不输出密钥）。需用独立测试schema隔离真实MySQL并发和模型生成SQL，避免改动用户业务数据。
