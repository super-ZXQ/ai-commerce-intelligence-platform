# ADR 0001：双轨知识检索的取舍与收敛计划

- 状态：已接受（Accepted）
- 日期：2026-09-05
- 决策人：项目维护者

## 背景

项目中存在两套知识检索实现，能力重叠但技术路线不同：

| | `ai-ecommerce-assistant/rag/` | `agent_core/rag/` |
|---|---|---|
| 检索方式 | BGE-small-zh-v1.5 向量检索（Chroma 持久化） | 词面 bigram 重叠评分（MarkdownKnowledgeRetriever） |
| 依赖 | sentence-transformers + langchain-huggingface + chromadb（含 ~93MB 模型下载） | 零第三方依赖（纯 Python 标准库） |
| 消费方 | Streamlit AI 助手（app.py 的懒加载检索器） | agent_core 离线评测（`agent_core/evaluation.py`）与 Agent Runtime 的可注入检索器 |
| 评估 | `eval/run_eval.py`（RAG 命中率） | `agent_core/evaluation.py`（recall@3 / MRR，无需模型额度） |

此外，生产 FastAPI 后端的 `ai_service`（Legacy 接口）使用的是自己的词面片段匹配，均未接入上述两套检索器。

这带来了一个合理的质疑：为什么同一能力有两套实现？为什么"更先进"的向量检索反而没有被生产链路消费？

## 决策

**保留两套实现，各自服务明确场景，暂不合并；在 README 与本 ADR 中明确记录取舍。**

理由：

1. **两者的服务目标不同。** 向量检索服务于真实用户查询（语义泛化："咋算复购"能命中"复购率"文档）；词面基线服务于**离线评测与无依赖环境**——`agent_core/evaluation.py` 的设计目标是不付模型/模型下载成本即可回归检索质量，向量方案无法满足"零依赖、秒级启动"的评测约束。
2. **生产后端（FastAPI）尚未消费任何一套**，是已知差距而非已做决策。把向量检索接入 backend 意味着 backend 镜像需引入 sentence-transformers/torch（镜像体积增加 1GB+，内存占用增加约 300MB），对当前以 SQL 聚合为主的 legacy 接口收益有限。
3. **强行收敛有真实成本。** 合并为"一套可插拔检索器"需要统一 chunking（H2/H3 结构切分 vs 按行切分）、统一接口签名与评估集，是一次涉及构建管线 + 两个消费方的重构；在 legacy `ai_service` 预定下线的背景下优先级不高。

## 后果

**正面：**
- 评测可在 CI / 无 GPU 环境零依赖运行，检索质量回归不被模型下载卡住。
- Streamlit 侧保留语义检索能力，懒加载设计保证首屏零 RAG 代价。
- 职责边界在文档中可查，技术协作场景不再需要口头解释"为什么两套"。

**负面 / 已知代价：**
- 两套 chunking 逻辑（`build_knowledge_base.py` H2/H3 切分 vs `MarkdownKnowledgeRetriever` 按行切分）意味着同一份知识库在两边的"文档"粒度不同，评估结论不能直接互换。
- 新增知识文档需要同时确认两套检索的表现。

## 收敛触发条件（满足其一即启动合并重构）

1. 生产 backend 需要知识检索能力（例如 legacy `ai_service` 下线、新接口统一走 agent_core），届时以"可插拔 Retriever 接口 + 按部署形态选择实现"收敛：有嵌入依赖的环境用向量检索，否则自动降级词面基线。
2. 词面基线在评测集上的 recall@3 显著落后（< 80% 阈值线），证明基线不再足以支撑离线回归的可信度。
3. 知识库文档规模超过词面检索的合理工作集（当前 6 份文档、数百 chunk），检索质量出现可比下降。

## 相关材料

- 路由 A/B 对照：`agent_core/eval/ab_routing_report.md`
- RAG 评估：`ai-ecommerce-assistant/eval/run_eval.py`、`agent_core/evaluation.py`
- Text-to-SQL 评估：`ai-ecommerce-assistant/eval/run_sql_eval.py`
