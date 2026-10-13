# 观测体系：Prometheus（业务指标）× Langfuse（LLM trace）

2026-10-13 安全审计更新：启动自托管观测栈前，必须在根目录 `.env` 配置独立的 `LANGFUSE_ENCRYPTION_KEY`（64位十六进制随机值，可用 `python -c "import secrets; print(secrets.token_hex(32))"` 生成）。已移除 Compose 中的固定默认值。已有观测数据库需先备份并按 Langfuse 的密钥轮换流程处理；不要直接换值后假设旧加密数据仍可读。此调整不改变默认关闭的 trace 功能、Prometheus、JSONL 或 UI 执行轨迹。

## 为什么有两套

两套系统回答的是**不同问题**，互不替代：

| | Prometheus | Langfuse |
|---|---|---|
| 回答的问题 | 「系统整体健康吗？」 | 「这一条查询为什么慢/为什么错/花了多少钱？」 |
| 数据粒度 | 聚合指标（QPS、失败率、token 总量、P95） | 单次请求的完整执行轨迹 |
| 典型用法 | Grafana 看板、告警规则、容量规划 | 点开一条 trace，逐节点回放：路由→生成 SQL→执行→RAG→合成 |
| 保留时长 | 自定义（本地 TSDB） | 自托管数据库（trace 明细更占空间，按需清理） |
| 数据内容 | 只有数字，不含 prompt | 含 prompt、SQL、节点耗时、token、成本 |

一句话：**Prometheus 看森林，Langfuse 看树木**。面试时被问「为什么不用一套」，标准答案是：聚合指标无法回答单请求问题（「上周三那条慢查询慢在哪」），而逐请求 trace 无法直接支撑告警和趋势（每分钟扫几千条 trace 算 P95 既不实时也太重）。

## 架构

```
用户提问
   │
   ▼
AgentRuntime（LangGraph 状态图）
   ├─ input_safety → route → retrieve/load_schema → generate_sql
   │     → validate_sql → execute_sql → synthesize → finalize
   │
   ├─[已有，不动]─→ Prometheus 指标（backend/utils/metrics + agent_core/operational_metrics）
   ├─[已有，不动]─→ 事件 JSONL / UI 执行轨迹 / token 元数据
   └─[新增]───────→ Langfuse trace（每个节点一个 span、每次 LLM 调用一个 generation）
                          │
                          ▼
                   docker-compose.observability.yml（自托管栈）
                   web:3000 / worker / clickhouse / postgres / redis
```

集成方式是 LangChain/LangGraph 标准的 **CallbackHandler**：`AgentRuntime.invoke/astream`
在执行时把 handler 挂进 LangGraph config（`agent_core/runtime.py`），LangGraph 自动把
每个节点作为 chain span 上报，模型调用自动成为带 token/成本/延迟的 generation。
查询结束后再向同一 trace 追加一条 `agent-finalize` 汇总事件（意图、token、延迟、步骤链）。

## 启用（默认关闭，未配置时全部静默跳过）

1. 启动自托管栈：

```powershell
docker compose -f docker-compose.observability.yml up -d
```

2. 浏览器打开 http://localhost:3000 注册账号，创建项目后复制 Public Key / Secret Key。

3. 在根目录 `.env` 追加（后端与 AI 助手共用同一份 .env）：

```
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=http://localhost:3000
# 可选：代码侧成本估算单价（美元/百万 token）；不配则只依赖 Langfuse UI 的 model price map
LANGFUSE_PRICE_INPUT_PER_MTOK=0.6
LANGFUSE_PRICE_OUTPUT_PER_MTOK=0.6
# 可选：每次查询结束等待上报完成（默认 false，后台批量上报）
LANGFUSE_FLUSH_EACH_QUERY=false
```

4. 重启后端与 AI 助手容器。未配置 `LANGFUSE_ENABLED=true` 或 Key 缺失时，
   所有 trace 调用静默降级，主流程零影响（有测试覆盖，见
   `backend/tests/test_observability.py`）。

## 一条 trace 里有什么

以「最近30天各平台销售额」为例，在 Langfuse UI 点开 trace 可见：

- **trace 元数据**：`owner`（提问用户）、`thread_id`、`query_length`、标签 `agent-query`
- **节点 span**（LangGraph 每个节点自动上报）：`input_safety`、`route`、`load_schema`、
  `generate_sql`、`validate_sql`、`execute_sql`、`synthesize`、`finalize`，
  每个 span 带耗时与状态
- **generation**（LLM 调用）：完整 prompt、模型名、input/output token、
  Langfuse UI 按 model price map 计算的成本、首 token 延迟与总延迟
- **agent-finalize 事件**：意图分类、token 合计、端到端延迟、步骤链、SQL 错误（如有）、
  配置单价时的估算成本

trace_id 与请求的 `request_id` 一一对应（`uuid.UUID(request_id).hex`），
日志里看到 request_id 即可反查 trace。

## 统计脚本

```powershell
python scripts/observability_stats.py --days 7
```

输出按天的：查询量、总/平均 token、P50/P95 延迟、成本。脚本走 Langfuse Public API，
**未配置 Key 时明确报错退出**（exit 2），不输出任何臆造数字。成本口径：
优先取 Langfuse UI 的 costDetails（需在 UI 配 model price map），
否则用 `LANGFUSE_PRICE_*` 环境变量按 token 线性估算（报告中有口径说明）。

## 隐私与部署注意

- trace 含完整 prompt 与 SQL：自托管部署保证数据不出内网；不要指向公网 SaaS。
- 监控端点本身免 JWT 的设计未变；生产环境可通过 `MONITOR_AUTH_REQUIRED=true` 统一加 JWT
  （见 README 部署节），Langfuse Web UI 的访问控制由其自身账号体系负责。
- 生产部署 HTTPS、域名、备份回滚见 `docs/deploy-vps.md`。

## 已知限制（本机待人工验证项）

- 本机 Docker 不可用，Langfuse 自托管栈（`docker-compose.observability.yml`）
  与 `scripts/observability_stats.py` 的真实链路**尚未端到端跑通**：
  SDK 导入、CallbackHandler 构造、`create_event` 上报 API 已在本地用假模块
  与 16 项单元测试验证，但「浏览器看到 trace」需要在你本机启动栈后确认。
- LangGraph 节点 span 的上报依赖 LangChain callback 传播机制，理论可用，
  待真实环境确认 span 树与文档描述一致（尤其是 `astream` 流式路径）。
