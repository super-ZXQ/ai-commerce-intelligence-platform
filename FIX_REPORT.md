# 修复记录（2026-10-13）

## P0

- TASK-001：网关 `/BI/`、`/ai/` 增加auth_request；登录签发HttpOnly、SameSite=Strict Cookie，生产Secure；Streamlit每次执行调用后端校验会话，失败即stop。业务API仍使用Bearer。服务端Key不进入浏览器输入框，自定义URL必须使用用户Key。生产需在入口终止HTTPS；本次ASGI测试7项通过（含原监控认证回归）。
- TASK-002/008：SQL按scope解析物理表和别名、用schema验证字段；拒绝通配输出、跨库、未知函数/语法、注释/递归、未绑定参数、负LIMIT、大OFFSET和笛卡尔JOIN。标识字段只允许去重计数及内部用户行为分组；原始提示schema只展示业务字段。新增53条安全用例。正常聚合、CTE、UNION、子查询继续可用。
- TASK-003：HTTPS精确主机白名单（默认DeepSeek/OpenAI）、检查所有DNS答案为公共IP，传输连接绑定已验证IP并保留原域名TLS身份；拒绝重定向/环境代理，明确连接/读取/写入/池等待超时。17项网络策略单元测试通过，尚需真实提供方调用验证TLS路径。
- TASK-004：生产拒绝缺失/复用主账号；开发回退须同时DEBUG与AI_DB_ALLOW_MAIN_FALLBACK。助手使用AI_DB_*。bootstrap撤销AI历史授权，迁移后仅授予orders业务列SELECT。增加db-permissions部署阶段，修正迁移密码fallback。实际部署账号权限尚需独立验证。
- TASK-005：创建事件使用AUTO_INCREMENT；新增0002迁移保留历史ID并修复旧列。事件连接使用READ COMMITTED，已存在订单继续FOR UPDATE；避免不存在订单间隙锁导致并发插入死锁。保留显式ID的内部辅助参数用于历史构造测试，业务写入不传ID。

P0合并回归：`python -m pytest backend/tests/test_mysql_order_concurrency.py backend/tests/test_sql_access_policy.py backend/tests/test_agent_runtime_core.py backend/tests/test_security_hardening.py backend/tests/test_page_auth.py backend/tests/test_model_network.py backend/tests/test_alembic_migrations.py -q --tb=short` → **149 passed，16.47s**。包含真实MySQL独立随机schema中的50/100不同订单并发、20次相同event_id、同订单退款/取消竞争、实际行锁超时重试、故障回滚和历史自增迁移；schema在fixture结束清理。

测试契约调整：原测试认可SELECT *、隐式主账号回退、固定baseline head；因需求明确改变这些行为，调整为业务列查询、显式开发开关与0002 head，保留原断言目的并增加拒绝/迁移验收。未删除失败测试。

## 待验证与后续

本节以下记录最终结果；未列为已验证的项目均不宣称完成。

## P1修复

| 修改文件 | 用途与验证 |
|---|---|
| `build_knowledge_base.py`、`rag/vector_store.py` | 完整规范化SHA256、构建锁、去重、先写后删、空源删除、失败传播；真实Chroma配合FakeEmbedding回归，不冒充BGE质量 |
| `rag/retriever.py`、`agent_core/rag/retriever.py` | Filter/版本Key、返回值隔离、失效世代；API词面检索按全内容指纹刷新，尾部修改与删除后可见 |
| `agent_core/business_data.py`、`backend/services/ai_service.py`、`ai-ecommerce-assistant/app.py` | 共享指标定义与实际日期范围；删除固定2025截止日；移除不含会话/数据版本的最终答案缓存，历史展示保留 |
| `streamlit_app.py`、`analytics_service.py`、`rfm_service.py` | 数据库BI与明确静态提示；统一有效订单、退款/取消与退款率分母；真实事件后API、SQL、BI对照一致 |
| `backend/utils/cache.py`、`rfm_service.py` | 标签版本进入Key；旧重建不能污染新世代；不回收正在等待/持有的RFM锁；竞争回归通过 |
| `agent_core/privacy.py`、`backend/routes/ai.py`、`backend/routes/order_events.py`、`rag/metrics.py`、`app.py` | 问题/答案/异常默认仅记长度或类别，反馈与JSONL有界轮转；保留Prometheus、事件结构和UI轨迹 |
| `runtime.py`、`model_adapter.py`、`llm_router.py` | 无证据硬拒答、SQL失败停止、检索资料不可信、来源服务端附加、阻断问题不再透过history外送；模型HTTP客户端在所属循环释放 |
| `agent_core/eval/audit_*.jsonl`、`audit_experiments.py` | 冻结新路由/RAG/SQL集合；真实结果集比较、正确保留列与排名、统计真实Token和错误；提供方失败不计成模型0分/零成本，401后停止后续请求 |

## P2与兼容性

- 新增 `agent_core/db_permissions.py`，生产API/助手/BI校验实际SHOW GRANTS；拒绝root、写权限、全库SELECT、角色或授权权限。列级只读账号在真实MySQL中允许聚合、拒绝写入和账本payload读取。
- PyJWT升级2.15.1；用uv重新生成三份既有锁文件，未顺手升级其他依赖版本。加入BI使用的SQLAlchemy/PyMySQL/httpx直接依赖和知识构建FileLock直接声明（既有锁已有该传递依赖）。
- `docker-compose.yml`新增迁移后权限阶段，BI使用数据库；`deploy/nginx.conf`页面认证带真实客户端IP，签名校验独立限流额度适配静态资源。
- `.github/workflows/ci.yml`显式启用测试开发回退；`docker-smoke.yml`更新迁移head、匿名401、登录Cookie和授权WebSocket验收，并等待助手完成启动。未声称远端Actions或完整本地全栈smoke已经通过。
- `docker-compose.observability.yml`移除固定加密Key，`deploy/.env.example`及观测文档说明必填与存量数据轮换；未修改真实env、未轮换用户真实密钥、未重写Git历史。
- 页面新增认证是有意的兼容性变化。生产需HTTPS、bcrypt密码和专用账号；本地开发才可显式DEBUG和AI_DB_ALLOW_MAIN_FALLBACK；SQLite演示须AI_DATA_MODE=demo。旧主键先迁移，旧AI授权需重新bootstrap/permissions。
- 默认有效销售额排除历史退款与取消；退款指标独立统计。这会改变历史展示数字，不能把它宣称为“销售增长”。静态BI按退款标志排除，数据库BI按完整订单状态排除。
- `SELECT *`、个人标识明细、未知字段/函数与复杂不支持语法会被拒绝；复购查询内部按用户分组只输出次数，不能投影用户名。保留LangGraph、RAG、RFM、BI、订单事件和原观测设施，未移动bi_style.py。

## 最终验证

最终全量命令：`RUN_DOCKER_AUTH_TESTS=1 python -m pytest -q --tb=short` → **417 passed，2条第三方WebSocket弃用警告，无跳过**。测试使用Python3.12.6/MySQL8.0.45，Docker网关fixture会创建并清理自己的容器与网络，MySQLfixture会清理自己的随机schema/测试账号。原测试仍保留；只针对需求明确改变的契约调整测试：通配/账号回退/head/隐私日志，以及修正原SQL测试fixture中不存在的platform/user_id字段；未改冻结gold来掩盖模型失败。冷启动检查改用独立解释器，避免新增Chroma集成测试加载Rust扩展污染导入检测。

- Ruff：通过；compileall：通过；原Agent离线评测命令通过。
- Bandit 1.9.4：中/高危0；Docker测试监听临时宿主端口的B104有两处精确例外，测试只挂载真实认证路由与无数据HTTP桩，finally关闭，不是生产接口例外。
- pip-audit 2.10.1：后端生产/dev完整锁复扫无已知漏洞；BI直接依赖复扫无已知漏洞，不能推断BI全部传递依赖已审计；AI完整锁Chroma告警保留。
- Docker：实际Nginx语法检查、统一认证集成通过；主Compose使用独立假配置 `config --quiet` 通过。现有根env缺少完整部署变量，未冒用或修改它来启动全栈。
- Gitleaks全Git历史：7条告警；位置元数据见`audit_secret_findings.json`，真实敏感值不入报告；不能声称全历史扫描无泄漏。
- 真实Embedding：完成18题词面/BGE/Hybrid对照；真实LLM接口401，能力指标N/A，见EVALUATION_REPORT.md。

## 已知未完成项

有效模型凭据下B/C、SQL生成、Token/生成延迟与注入回答评测未完成；Chroma无答案拒答/上游漏洞与API/UI检索一致性未全面解决；历史疑似凭据待所有者核对；完整Docker业务栈、公网HTTPS、Python3.13、Redis跨worker故障和生产负载未运行。RFM进程内完整快照仍按单worker部署；Chroma多步更新不是原子快照；提示词不能形式化保证模型永不被文档指令影响。没有编造性能提升或宣称全部16项验收完结。
