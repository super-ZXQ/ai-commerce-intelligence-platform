# GLM 真实模型评测

本目录保存显式联网运行的模型评测快照。API Key 只通过进程环境变量传入，不写入报告、日志或仓库。

## 最新结果

> 以下是 2026-09-01 按旧比较规则生成的历史快照。旧规则忽略列位置与行顺序；
> 评测器现已修正，以下通过率不能作为新规则的结果，需重新运行真实模型评测。

- 运行时间：2026-09-01（UTC）
- 提供方：智谱 BigModel
- 模型：`glm-4-flash-250414`
- 数据：完整 `cleaned_orders.csv`，共 102,287 行
- 数据集：10 条 Text-to-SQL + 5 条知识回答

| 指标 | 结果 |
|------|------|
| 总通过率 | 93.33%（14/15） |
| 结构化 SQL 输出率 | 100% |
| SQL AST 合法率 | 100% |
| SQL 执行成功率 | 90% |
| SQL 结果正确率 | 90% |
| 知识关键词覆盖率 | 100% |
| 引用完整率 | 100% |
| 延迟 P50 / P95 | 1,508 ms / 8,941 ms |
| Token 用量 | 6,894 |

详细逐项结果见 [`glm-4-flash-250414.json`](glm-4-flash-250414.json)。

## 评测方法

1. 把完整订单 CSV 导入隔离的内存 SQLite，不连接生产数据库。
2. 模型通过 JSON Mode 生成结构化 SQL，Pydantic 校验 `sql` 字段。
3. SQL 先经过 SQLGlot AST 安全校验，再从 MySQL 方言转译到 SQLite 执行。
4. 当前评测器按列位置比较实际结果值；参考 SQL 的顶层 ORDER BY 存在时还比较行顺序，不比较 SQL 字符串。列别名不影响比较。
5. 生成、校验或执行异常时，按生产工作流使用脱敏错误纠正一次；可执行但结果错误时不重试。
6. 知识回答检查业务关键词和实际检索来源文件名，不使用模型自评作为裁判。来源由 `agent_core` 的字符 bigram 词法检索基线提供，本报告不把它包装为 BGE/Chroma 向量召回效果。

唯一失败项为“整体客单价”：模型两次生成的 SQL 都缺少 `FROM orders`，执行层拦截后按规则停止。该失败保留在报告中，没有修改参考答案或人工改写模型输出。

## 复现

真实评测不会在 CI 自动运行，必须显式允许联网：

```powershell
$env:LLM_API_KEY = "<临时 Key>"
$env:LLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
$env:LLM_MODEL = "glm-4-flash-250414"

python -m agent_core.live_evaluation `
  --allow-network `
  --output docs/evaluation/glm-4-flash-250414.json

Remove-Item Env:LLM_API_KEY
```

模型输出具有随机性，后续复跑可能产生不同结果；报告是指定时间、模型和数据集上的可审计快照，不代表所有问题的总体准确率。
