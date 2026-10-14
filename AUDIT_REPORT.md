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
| TASK-016 / P2 | CONFIRMED：已有README/ADR/评测报告，但与本任务验收范围不同 | 历史报告不能代替本次运行；架构/边界需反映修复后实现 | 更新README、架构/安全链路/实验限制、三份技术报告与复现说明 |

建议顺序：001（匿名模型/数据访问）→002/008（SQL边界）→003（SSRF/凭据外送）→004（数据库最小权限）→005（并发主键）→006/007（知识一致性）→009/010/014→011/012/013→015/016。保留现有 LangGraph、RFM、BI、订单事件与观测设施。

审计环境：Windows PowerShell、现有 `.venv`；Docker daemon可用，初始无运行容器；两个本地env存在模型配置（仅检查是否配置，不输出密钥）。需用独立测试schema隔离真实MySQL并发和模型生成SQL，避免改动用户业务数据。

## 最终处置与验收状态

以下“修复”指源码及列出的测试通过，不能替代尚未执行的生产部署或模型验收。

| ID | 最终状态 | 实际证据与剩余边界 |
|---|---|---|
| 001 | 已修复并验证 | `test_page_auth.py`、`test_gateway_docker.py`、`test_server_key_boundary.py`；匿名/伪造/过期拒绝、真实Nginx资源路径保护、直接脚本stop、服务器Key不随用户URL发送；完整浏览器与公网TLS未运行 |
| 002 | 已修复并验证 | `test_sql_access_policy.py`55个允许/拒绝SQL；`db_permissions.py`生产核验实际授权；正常聚合/CTE/UNION继续通过 |
| 003 | 已修复并验证网络策略 | `test_model_network.py`19项，真实异步提供方请求到达认证阶段；连接绑定公共IP、保留TLS域名、禁止重定向/代理；未验证成功模型输出 |
| 004 | 已修复并验证 | `test_mysql_order_concurrency.py:test_ai_account_actual_grants_allow_aggregates_and_deny_writes`真实列级SELECT账号验证；生产权限启动检查；未操作用户现有真实账号 |
| 005 | 已修复并验证 | 真实MySQL50/100并发、20重复事件、状态竞争、锁超时重试、回滚、历史迁移；没有使用MAX+1 |
| 006 | 已修复并验证 | `test_kb_incremental.py`真实Chroma/FakeEmbedding：尾部修改替换、失败不报ok、最后源文件删除、重复Chunk去重；重建先验证源目录 |
| 007 | 已修复并验证 | Filter/version/配置隔离、深拷贝与进行中失效测试；API词面源文件变化自动重载；跨操作Chroma原子快照未实现 |
| 008 | 已修复并验证 | 与002共享AST，JOIN恒真/OR/单来源条件拒绝；真实MySQL聚合/参考SQL执行及执行时限测试；不宣称完整成本估计 |
| 009 | 数据链路修复并验证，模型口径待验收 | 实际日期+共享口径+实时BI，创建/退款/取消后三方人工SQL对照；进行中缓存重建竞争已修复；真实模型401，尚无法验证模型始终遵循口径 |
| 010 | 已修复并验证 | 默认日志只记长度/类别；反馈不存问题答案；JSONL保留结构并轮转，单类约5MiB；被拦截问题不进入下轮模型history |
| 011 | 框架与A实测完成，B/C未验收 | 冻结30题；A独立集56.67%/53.07%；真实B请求认证失败，C配对回放无有效响应，不报告虚假能力指标 |
| 012 | 真实对照完成，拒答仍有限 | 词面/BGE/Hybrid统一18题；Hybrid未优于基线且无答案拒答0%，不启用；Reranker未运行，API/UI仍不同 |
| 013 | 真实MySQL评测基础完成，模型未验收 | 冻结20题/36行fixture/SELECT账号/真实oracle/结果比较；17题模型请求认证失败，候选能力指标null |
| 014 | 硬边界修复并验证，生成语义仍有风险 | 无来源硬拒答、SQL失败硬停止、被拦截历史去敏、真实来源附加；检索误命中与自然语言注入不能靠提示词证明完全消除 |
| 015 | 部分完成 | Ruff/编译/Bandit中高危0；后端与dev依赖扫描0；Chroma告警保留；历史疑似凭据待核对；完整Docker栈/多worker故障未验收 |
| 016 | 已交付 | README、三份技术报告、架构/边界/复现命令；技术更新通过独立分支发布，部署验收另行进行 |

## 补充发现

- **TASK-015a / P1 / CONFIRMED**：PyJWT 2.13.0依赖扫描报告14条已知漏洞，项目每条漏洞的可利用性并未逐一复现。升级2.15.1并重新生成锁文件后，后端生产与dev完整锁复扫均无已知漏洞；增加JWT长度限制与异常保护。依据：[维护者发布记录](https://github.com/jpadilla/pyjwt/releases/tag/2.15.0)。
- **TASK-015b / P1 / CONFIRMED告警、SUSPECTED项目可利用性**：Chroma 1.5.9扫描5条记录、4个不同PYSEC ID。当前使用嵌入式PersistentClient，未启动HTTP服务器；公开的服务端集合API利用路径不直接对应本部署，这是基于源码的适用性判断，不是宣布库无漏洞。显式禁用trust_remote_code，不关闭扫描、不随意切换技术栈。依据：[公开漏洞记录](https://github.com/pypa/advisory-database/blob/main/vulns/chromadb/PYSEC-2026-3814.yaml)。
- **TASK-015c / P1 / CONFIRMED**：观测Compose两处使用固定ENCRYPTION_KEY。当前源码改为显式必填，保留默认关闭的trace与原观测功能；历史记录未改写，已有加密数据需按轮换流程处理。
- **TASK-015d / P1 / SUSPECTED**：Gitleaks全历史发现旧`copyright_materials/`材料3条疑似凭据，是否真实/仍有效未确认。只交付文件、行号与提交元数据，不输出原值；需要所有者核对，确为凭据则轮换并另行安排历史治理。
- **TASK-015e / P2 / NOT_APPLICABLE**：另外2条历史Gitleaks记录对应WebSocket测试的公开样例nonce，不是登录/模型凭据；未为此放宽整个扫描规则。全历史总计仍7条告警，不能宣称密钥扫描全绿。
- **TASK-015f / P1 / CONFIRMED**：事件失效与进行中analytics/RFM重建竞争可使旧计算重新缓存。缓存标签世代进入Key，RFM保留正在使用的锁并拒绝旧世代回填；`test_cache_event_race.py`复现并验证修复。Redis故障期间跨进程失效、RFM多worker快照仍需进一步验证。

初始判断中未强行修改的部分：新建orders DDL与ORM本来已有AUTO_INCREMENT，问题是业务MAX+1与存量升级遗漏；SQL失败本来就不让模型预测结果，保留并补充回归；已有MySQL执行时限测试能证明数据库终止失控查询，保留该机制。原RFM前端cache Key已包含每用户最新时间，未据笼统怀疑重写评分模块或移动bi_style.py。
