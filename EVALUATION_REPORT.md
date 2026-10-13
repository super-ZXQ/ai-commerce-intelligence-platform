# 实验报告（2026-10-13历史快照）

> 2026-10-14已有成功的真实模型评测与成本证据，见 PRE_MERGE_REVIEW.md；下文认证失败仅指当日运行，保留原始记录。

## 结论与来源

本次真实运行证据是 `docs/evaluation/audit_routing_run.json`、`audit_rag_run.json`、`audit_sql_run.json`。没有把Mock或数据库测试包装成模型能力。模型接口使用配置的 `deepseek-v4-flash` / `api.deepseek.com`，真实请求返回AuthenticationError，未得到模型输出，**路由B/C与Text-to-SQL能力指标不可得**。配置存在不代表凭据可用，错误响应延迟不代表模型生成延迟，未返回Token不代表免费。

环境：Windows 11、Python 3.12.6、MySQL 8.0.45；LangGraph 1.2.11、LangChain OpenAI 1.3.2、SQLGlot 30.17.0、Chroma 1.5.9、sentence-transformers 5.6.0。模型接口实验在PyJWT升级前运行，安全最终回归使用2.15.1；JWT不参与模型评测。BGE使用实际缓存的 `BAAI/bge-small-zh-v1.5` 权重与真实Chroma，6份原文构建60个Chunk。最终检索报告包含依赖版本、源码SHA256与数据集SHA256。

新测试集先提交冻结于 `ee21386`：路由30题、RAG18题、SQL20题；没有在看完输出后调整标签或生产路由。新集由本次审计者编写，独立于原文件和调参过程，但未做人类双盲标注，也不代表生产流量。原curated、robustness与heldout均保留；源码显示原heldout部分失败模式在过去安全加固中已被使用，不能再当完全独立测试集。

## 路由A/B/C

- A：生产确定性 `classify_intent`。
- B：确定性安全策略拦截高风险后，调用真实LLM分类。
- C：规则优先；被规则归为兜底data且缺少明确数据标记时调用LLM。与B进行配对回放，复用同一次模型响应，不声称独立重复调用了C。

| 数据集 | 样本数 | A Accuracy | A Macro-F1 | A平均耗时ms | A模型Token |
|---|---:|---:|---:|---:|---:|
| Curated | 100 | 100.0% | 100.0% | 0.043 | 0 |
| Robustness / dev | 25 | 96.0% | 95.05% | 0.024 | 0 |
| 旧Heldout（有污染风险） | 10 | 90.0% | 56.0% | 0.020 | 0 |
| 新独立审计集 | 30 | 56.67% | 53.07% | 0.024 | 0 |

Macro-F1固定平均五类，即使小集合缺某类仍计入，不能只报Accuracy掩盖类别覆盖。B实际发起141次逻辑分类调用，均失败于认证；C配对所需37条模型响应同样不可得。JSON保留逐题失败类别与观测请求延迟，但能力Accuracy/Macro-F1/Token/成本设为null。没有根据这些失败把生产切到LLM或Hybrid路由。

初次诊断运行完整记录了认证失败；评测器已增加401后停止后续请求，并在失败时返回非零退出码，避免反复消耗无效请求。配置有效凭据后必须重新运行才能补齐B/C。成本只在提供真实Token与明确的输入/输出单价时估算，不把报价或估算当账单。

## RAG真实对照

同一冻结18题：12条有答案、6条无答案。按冻结的文件名+章节身份判定相关来源，Recall@3与MRR只对有答案题统计；拒答准确率是无答案题返回空检索结果的比例。它衡量检索器，不是LLM自然语言答案质量。

| 检索方案 | Recall@3 | MRR | 无答案拒答准确率 | 平均检索ms |
|---|---:|---:|---:|---:|
| 当前词面Markdown | 66.67% | 0.5278 | 66.67% | 3.369 |
| BGE + Chroma，阈值0.4 | 50.0% | 0.3611 | 0.0% | 37.212 |
| Hybrid RRF | 66.67% | 0.5139 | 0.0% | 40.580 |
| Reranker | 未运行 | 未运行 | 未运行 | 未运行 |

Hybrid融合两路已检索Top3，RRF常数60，同一路相同章节只计一次；延迟为配对两路实测检索延迟之和，未单独计入极小的融合CPU开销，不包含模型加载/建库/生成回答。BGE指标是真实权重运行，单元测试中的FakeEmbedding不参与这些质量数字。

结果不支持启用Hybrid：Recall没有超过词面基线，MRR略低，成本与拒答更差。未引入Reranker。API仍使用词面检索、UI仍使用BGE；这份实验明确暴露入口差异，没有声称两者完全一致。BGE当前距离归一化+阈值对无答案过于宽松，需要另建校准dev集、确定阈值与新独立验证集；本次未按这18题调参以制造提升。无来源硬拒答已实现，但错误命中来源的幻觉风险仍在。

## Text-to-SQL

冻结20题：16条可执行查询（统计、过滤、分组、Top-K、时间、同比环比、用户行为、退款），3条意图/安全拒绝题，1条不存在业务字段的无效问题。真实MySQL临时schema有36条确定性合成订单，覆盖跨年、退款、取消与复购；fixture SHA256见JSON。该数据不是102,287条生产样本的性能基准，不包含真实用户个人数据。

评测器为候选创建独立SELECT-only账号，在同一连接事务中执行参考/候选，比较真实结果集，保留列顺序和有序题排名，数值容忍0.005舍入误差；不会逐行把列排序后把互换的列判对。候选与生产共用AST与数据库时限。生成/校验/执行失败最多纠错一次；不把语义错误的结果通过修改gold修正。

| 指标 | 本次结果 |
|---|---|
| MySQL参考查询 | 16条已实际执行 |
| 真实模型SQL请求 | 17题、34次逻辑尝试，均AuthenticationError |
| 候选执行成功率 / 结果正确率 | N/A，模型未返回候选 |
| 重试成功率 | N/A，认证失败不能衡量纠错能力 |
| 平均Token / 模型生成P50/P95 | N/A，无有效模型响应 |
| 错误分类 | 17题最终为AuthenticationError；逐题请求时间保留在JSON |

新建订单、退款、取消后，API指标、经过安全校验的人工SQL和BI数据函数一致，已由真实MySQL测试验证；这证明数据链路，不证明LLM一定生成相同口径。

## 验证类型与复现

| 类型 | 已运行范围 | 不能推断的能力 |
|---|---|---|
| 单元 / Mock | SQL55例、网络策略、LangGraph分支/重试/拒答、缓存竞争、统计器、反馈隐私 | 真实模型语义能力 |
| Chroma集成 + FakeEmbedding | 增量更新、删除、去重、缓存版本 | BGE检索质量 |
| 真实MySQL集成 | 50/100并发、20重复事件、状态竞争、超时重试/回滚、迁移、实际授权、指标对照 | 生产峰值吞吐、多机容灾 |
| Docker集成 | Nginx auth_request + 真实认证路由 + 页面HTTP桩；配置语法 | 完整Docker业务栈、公网HTTPS、真实浏览器端到端体验 |
| 真实Embedding | 上述18题BGE/Hybrid对照 | 回答生成质量 |
| 真实模型接口 | 认证失败已记录 | B/C路由、SQL生成与回答能力，尚未完成验收 |

```powershell
# 不调用外部模型，规则和词面基线
.venv\Scripts\python.exe -m agent_core.eval.audit_experiments --section routing --output output/routing-offline.json

# 真实BGE，使用临时向量目录，不改生产库
.venv\Scripts\python.exe -m agent_core.eval.audit_experiments --section rag --real-bge --output output/rag-real.json

# 有效模型凭据通过backend/.env或环境变量提供，不在命令行或Git中填写真实Key
.venv\Scripts\python.exe -m agent_core.eval.audit_experiments --section routing --live-model --output output/routing-live.json
.venv\Scripts\python.exe -m agent_core.eval.audit_experiments --section sql --live-model --output output/sql-live.json
```

SQL实验需可创建独立临时schema和测试账号的隔离管理凭据，结束清理自己创建的资源。不得对生产库写测试数据。API历史50题GLM报告保留为历史实验，与本次新集/新口径不能直接混合汇总。
