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

P1/P2进行中。基线301项通过，不意味着所有新验收已完成。最终全量结果、模型实验、部署检查与剩余风险将在本文件补充；未列为已验证的项目均不宣称完成。
