# FastAPI / MySQL 流式问答评测

2026-11-09 使用 `deepseek-flash`、隔离的 MySQL 8.0.46、FastAPI `/api/ai/stream` 运行。评测通过 HTTPX ASGI 传输进入实际路由，使用真实登录、Agent Runtime、只读 SQL 账号和模型调用。短期会话使用内存存储，情节向量关闭。

[逐例记录与首轮失败样本](ai-workflow-mysql-deepseek.json) · [冻结题集和金标](../../agent_core/eval/ai_workflow_mysql.jsonl) · [评测入口](../../backend/scripts/evaluate_ai_workflow.py)

## 测试数据与评分

- 121 条合成订单，两个窗口各 60 条有效订单，另含一笔日期更晚、金额为 9,999 元的取消订单。运行前先经销售总览 API 验证有效订单数 120、销售额 17,200 元。
- 10 个问题覆盖平台汇总、最新有效订单日期、客单价混合问答、同一会话的两轮追问、复购率知识问答、退款多步分析、澄清和写操作拒绝。
- 工具项核对意图、终态和预期工具。数据项核对结果集或退款聚合计数。回答项检查正文数值、措辞和引用。严格自动检查与人工核对分别记录。

| 运行 | 工具及路径 | 数据金标 | 回答规则 |
|---|---:|---:|---:|
| 首轮 | 9/10 | 7/7 | 8/10 |
| 修复后同集复测 | 10/10 | 6/7 | 9/10 |

首轮复购率定义被路由成查数，客单价查询正确但合成阶段误把计数派生指标假名化。复测修正了这两处。客单价回答给出 143.33 元，分子 17,200 元，分母 120 单，与本样本一致。它因 SQL 多返回两列核算值、回答用“实付金额”而触发了冻结检查的列数和措辞差异。该 SQL 还采用正支付过滤，这组全部正支付的有效订单无法验证该条件在零支付数据上的影响。

这是用于验证当前链路的小样本复测，题集参与了上述两处缺陷的诊断。报告分数描述固定输入及检查规则下的结果，模型回答会随运行波动。

## 首页回放案例

> 退款率为什么上升？按平台和商品拆开看，给出比较窗口、分子分母和商品样本范围。

| 证据 | 本期（2025-03-31 至 2025-04-29） | 上期（2025-03-01 至 2025-03-30） |
|---|---:|---:|
| 整体 | 18 / 60（30%） | 6 / 60（10%） |
| APP | 16 / 40（40%） | 4 / 40（10%） |
| Web网站 | 2 / 20（10%） | 2 / 20（10%） |
| 商品 SKU-001 | 3 / 3（100%） | 0 / 1（0%） |

复测按整体、平台和商品执行三条聚合 SQL。商品查询返回 50 行，模型回答说明了合成提示词中的 14 行样本范围，并引用了 SKU-001 的计数。该次请求耗时 36.2 秒，报告记录 7,509 tokens。首页回放保存逐节点事件和各步实际执行的 SQL。

## 复现

从仓库根目录运行。模型参数通过进程环境变量传入，配置 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 和 `LLM_PROVIDER_ALLOWLIST`。以下账号仅用于绑定到回环地址的临时 MySQL 容器。

```bash
docker run --rm -d --name ea-ai-workflow-eval -p 127.0.0.1:33316:3306 \
  --tmpfs /var/lib/mysql -e MYSQL_ROOT_HOST=% -e MYSQL_DATABASE=ai_workflow_eval \
  -e MYSQL_ROOT_PASSWORD=eval-local-root mysql:8.0 --default-time-zone=+00:00

export DB_HOST=127.0.0.1 DB_PORT=33316 DB_NAME=ai_workflow_eval DB_USER=root DB_PASSWORD=eval-local-root
export AI_DB_USER=ea_eval_ro AI_DB_PASSWORD=eval-local-read
export DEBUG=true ADMIN_USERNAME=eval-admin ADMIN_PASSWORD=eval-local-login ANALYST_PASSWORD=
export JWT_SECRET="$(openssl rand -hex 32)" LLM_MODE=managed REDIS_ENABLED=false AGENT_EPISODE_VECTOR_ENABLED=false
export AUTH_DB_USER= EVENT_DB_USER= IMPORT_DB_USER= MIGRATION_DB_USER= MIGRATION_DB_PASSWORD=

# MySQL 就绪后执行迁移和只读授权
python backend/scripts/run_alembic_upgrade.py
docker exec -i -e MYSQL_PWD=eval-local-root ea-ai-workflow-eval mysql -uroot <<'SQL'
CREATE USER 'ea_eval_ro'@'%' IDENTIFIED BY 'eval-local-read';
GRANT SELECT ON ai_workflow_eval.orders TO 'ea_eval_ro'@'%';
SQL

python -m backend.scripts.evaluate_ai_workflow --allow-network --output output/ai-workflow-mysql.json
docker stop ea-ai-workflow-eval
```

评测器在空的 `ai_workflow_eval` 库内写入合成订单和引导账号。每次复跑使用新临时库。只要完成执行，即使有用例失败也会保存 JSON；任一检查未通过时退出码为 1。

首页 JSON 可用 `python backend/scripts/build_agent_trace_snapshot.py` 从归档的评测报告重新生成。
