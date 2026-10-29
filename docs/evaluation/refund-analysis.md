# 退款率多步分析评测

## 已执行的离线评测

- 时间：2026-10-29（UTC），人工构造订单数据、隔离内存 SQLite。
- 模式：预设工具选择和占位回答；不调用真实模型。
- 结果：6/6 项自动检查通过。逐项证据见 [离线快照](refund-analysis-offline.json)。
- 检查内容：平台、商品、组合拆解的工具选择与聚合计数；未上升和无数据时停止；数据库失败时返回错误；最多三次查询。
- 局限：六个小型合成场景只验证流程，不能证明实际模型的选择、回答质量或真实业务数据泛化能力。

复现：`python -m agent_core.refund_evaluation --output output/refund-offline.json`。

## 真实模型评测

配置当前进程的 `LLM_API_KEY`，并按实际提供方设置 `LLM_BASE_URL` 和 `LLM_MODEL`；密钥不要写进报告。然后运行：

```bash
python -m agent_core.refund_evaluation --mode live --allow-network --output output/refund-live.json
```

也可将模型配置保存在已被 Git 忽略的 `backend/.env`，并添加 `--env-file backend/.env`。进程环境变量优先于文件内容。请将对应提供商的 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 一起配置，避免使用不匹配的默认模型。

脚本使用同一组合成数据，真实调用模型选择下一步和生成回答；模型请求可能产生费用。自动检查不等同于最终回答正确率。对每条模型回答人工检查：

1. 是否正确描述两个时间窗口和退款率的分子、分母；
2. 是否满足题目指定的平台／商品拆解，结论是否有对应数据支持；
3. 是否把相关性错误地描述为已证实的退款原因，或在证据不足时编造原因；
4. 是否解释商品结果最多 50 行的局限。

未配置密钥或未显式允许联网时，脚本不会调用真实模型。

2026-10-29 使用 DeepSeek V4.1 Flash（API 模型名 `deepseek-flash`）对这六条合成案例进行了三次运行，记录如下：

| 运行 | 自动检查 | 说明 |
|---|---:|---|
| [首次运行](refund-live-deepseek-initial.json) | 6/6 | 使用原始提示词和旧自动检查；旧检查未将回答合成失败单独列为失败项。 |
| [补充检查后](refund-live-deepseek-before.json) | 4/6 | 未修改规划提示词；模型在两例用户要求商品或平台拆解时选择提前结束。 |
| [调整提示词后](refund-live-deepseek-after.json) | 6/6 | 明确要求遵循用户指定的拆解维度，即使样本小也先查询并说明局限。 |

每次仅三个上升案例调用模型；其他三个案例由工作流直接处理。调整后的模型调用案例分别消耗 3,767、3,729、4,662 token，耗时约 7.8、6.9、10.3 秒。三个生成的回答均给出了对应时间窗口、订单和退款数量，也说明了样本量小及不能证明退款原因；但未展示通用的商品结果最多 50 行的限制。

六条用例规模很小，且第三次运行的提示词根据前次失败调整，因此它是开发集复测，不能视为独立留出集泛化成绩。模型的工具选择存在随机性；评测结果不代表真实业务数据上的整体准确率。评测只发送人工构造的订单数据，不发送本机的真实订单明细。

## 新问题留出集（2026-10-29）

先固定 [21 条留出问题和期望结果](../../agent_core/eval/refund_holdout.jsonl)，再使用现有提示词运行，运行后没有依该集合修改提示词。数据集 SHA-256：`39152aff88f5d9572866c0c7b5656a8d34e01e99ffd2b03d51c3292438549a02`。逐条 SQL、结果、轨迹和回答见 [真实模型快照](refund-holdout-deepseek.json)。

- **自动检查 21/21 通过**：12 条需要模型选择平台、商品或两者，全部检查通过；另外 9 条测试未上升、无数据或首次查询失败，均由工作流处理，不调用模型。
- 12 条模型场景合计 56,431 tokens，端到端耗时中位数约 11.8 秒。逐条结果包含耗时与可用 token 计数。
- 人工核对这 12 条回答：整体窗口与聚合数值一致，均提到了样本很小以及不能证明具体退款原因；要求商品拆解的回答没有说明商品结果最多 50 行的通用限制。
- **适用范围**：这是与开发集不同的问题和合成订单，但 12 个模型问题共享同一组上升订单，另外控制场景复用少量固定数据；题目由项目维护过程中编写，且均落在现有分支支持的问法内。它主要检查这些问法的工具选择和小样本回答，不能代表真实业务分布或完整自然语言路由的泛化率；自动通过也不等于回答正确率。

复现：

```bash
python -m agent_core.refund_evaluation --mode live --allow-network --env-file .env \
  --dataset agent_core/eval/refund_holdout.jsonl --output output/refund-holdout-deepseek.json
```

## 隔离 MySQL 验证

CI 的 MySQL 服务中启用 `TEST_ORDER_CONCURRENCY=1`，运行订单并发创建和退款分析 MySQL 方言测试。退款方言测试创建一个随机命名的独立数据库，测试结束后删除该数据库；旧库迁移测试也使用专用测试库。**这些测试需要数据库创建和删除权限，只能在专门用于测试的隔离实例运行。**

隔离实例中设置 `DB_HOST`、`DB_PORT`、`DB_USER`、`DB_PASSWORD`、`DB_NAME`，先执行 `python -m alembic -c backend/alembic.ini upgrade head`，再运行：

```bash
TEST_ORDER_CONCURRENCY=1 python -m pytest backend/tests/test_alembic_migrations.py backend/tests/test_order_event_create_pk.py backend/tests/test_refund_analysis.py -q
```

2026-10-29 在本机 Docker 的隔离 MySQL 8.0 实例中执行了上述三个测试文件，**23 项通过**；包含历史库自增迁移、订单并发创建和三个退款分析工具的 MySQL 方言查询。临时实例已停止并移除。本机项目数据库另以真实公开订单样本执行整体聚合查询成功。这些验证不包含真实模型的回答质量评测。
