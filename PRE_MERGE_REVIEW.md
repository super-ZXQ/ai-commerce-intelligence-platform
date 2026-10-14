# 合并前验收报告

日期：2026-10-14。分支 `codex/technical-security-updates`，起点 `58a63d1`。目标：[草稿 PR #10 → main](https://github.com/super-ZXQ/ai-commerce-intelligence-platform/pull/10)。没有合并 main。

**结论：暂不建议合并。** 技术修复、真实模型与Docker验收已有证据，但Chroma依赖门禁仍失败，聊天中暴露的真实模型Key尚需所有者轮换，当前HEAD的全部远端检查也须核对。授权撤销不等于已经撤销；单次小样本高分不代表生产泛化。

## 确认的缺陷与修复

| 缺陷 | 最小修复与证据 |
|---|---|
| 权限验证错误接受整个orders表SELECT | 必须明确列集合；继续拒绝未许可列、写权限、跨表/库、角色及授权权限。真实隔离MySQL验证列级允许、表级拒绝、额外private_note拒绝、UPDATE拒绝；聚合允许，实际写SQL被数据库拒绝 |
| 列级账号反射执行SHOW CREATE TABLE报1142 | 修复前真实MySQL复现；MySQL改为参数化读取账号可见information_schema字段，其他数据库保留原反射；不扩大授权 |
| 迁移成功但CI要求旧head | 校验0002与数据库版本，隔离MySQL实际CI入口和远端迁移通过 |
| CI离线路由缺运行依赖、后端cwd不一致 | 安装既有锁文件，从仓库根跑backend/tests；远端后续CI通过 |
| 监控鉴权测试依赖未跟踪rag_stats.json | 每个测试创建独立临时快照，专项4/4；远端从失败恢复，不修改生产缺快照503语义 |
| Docker host构建网络触发Buildx权限拒绝 | 移除非必要host网络与entitlement；本地全栈及远端空卷Smoke通过 |
| Windows CRLF使挂载shell脚本失败 | .gitattributes限定deploy/mysql/*.sh为LF；本地bootstrap、迁移及grant完成 |
| 真实评测漏计失败SQL/重试Token | 已返回响应在异常分支后也计入；回归验证两次失败仍计24输入/6输出；快照以原始HTTP usage修正并保留旧统计，未重跑刷分 |

保留LangGraph、RFM、BI与观测功能。生产路由仅更正文档说明，分类逻辑和检索阈值未改变。

## 本地与GitHub验证

本地最终全量与最后一次远端提交状态在报告末尾补充。真实MySQL权限/并发/迁移、Nginx认证均已运行。

| 已核实运行 | 状态 |
|---|---|
| 7055882 Docker Smoke / 38485906468 | PASS，真实空卷冷启动 |
| a11a95b CI / 38486407606 | PASS，后端、AI Python 3.12/3.13、Ruff、离线路由、编译 |
| a11a95b Docker Smoke / 38486407621 | PASS |
| a11a95b Security / 38486407665 | FAIL：完整历史Gitleaks、AI依赖；后端依赖/Bandit通过 |
| 58a63d1 Release / 38402191988 | PASS；后续提交的镜像结果需分别核对 |

[CI](https://github.com/super-ZXQ/ai-commerce-intelligence-platform/actions/workflows/ci.yml)、[Docker Smoke](https://github.com/super-ZXQ/ai-commerce-intelligence-platform/actions/workflows/docker-smoke.yml)、[Security](https://github.com/super-ZXQ/ai-commerce-intelligence-platform/actions/workflows/security.yml)、[Release](https://github.com/super-ZXQ/ai-commerce-intelligence-platform/actions/workflows/release.yml)。前一提交成功不自动代表后续提交成功。

## 产物兼容与数据复现

README截图仍在docs/screenshots/被跟踪；运行入口不依赖.impeccable/。原主线27个设计工具/分析输出恢复本地并继续忽略，没有删除用户产物或重新全部入库。

Notebook 03/04补齐output目录创建，04修复损坏的日期字段。五份Notebook在独立临时目录执行全部非空代码单元通过，生成11个CSV/PNG，未覆盖原数据或用户输出。证据：docs/evaluation/premerge_notebooks.json。

历史负载报告的output路径为本地未入库证据，不能当作GitHub下载附件；保留报告、原产物与复现命令，不编造丢失证据。

## 真实模型验收

旧root/backend配置一致、无多余空白，官方模型发现/最小聊天均401；用户授权的临时凭据两者200，定位为旧配置认证问题。成功评测只在进程使用新凭据，本机原env未修改，未提交真实Key。

请求模型deepseek-v4-flash，实际响应deepseek-flash；提供方将旧名作为兼容别名，不宣称仍在调用退役旧模型。[官方模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)

| 冻结任务 | 实测结果 | 限制 |
|---|---|---|
| 新30题A规则 | 17/30，56.67%；五类Macro-F1 53.07% | 未按审计题调参 |
| 同题B安全拦截后LLM | 30/30，100%；Macro-F1 100% | 非纯LLM安全能力测量 |
| 同题C配对混合回放 | 29/30，96.67%；Macro-F1 96.64% | 复用B响应，没有再次调用C |
| MySQL20题、36行fixture | 可执行16题15正确，93.75%；全部20题17符合预期，85% | 严格列级账号、真实oracle；17题调用模型，共18次 |
| 历史50题综合 | 41/50，82%；SQL24/30，80%；知识17/20，85%；引用20/20 | SQLite静态2025与旧业务口径，不替代MySQL新口径 |
| 安全工作流12题 | 12/12拒绝、无SQL执行 | 路由短路，无模型调用，不是模型抗注入证明 |

错误保留：A有4个knowledge→data、1个hybrid→knowledge、6个clarification→data、2个blocked→data；C仍漏r13双任务意图。MySQL的s09重复购买人数SQL被策略拒绝，s17模糊问题未澄清，s20不存在星座字段请求生成说明文本SELECT而未语义拒绝。

历史SQL的sql-004/018/022/023/024/028被安全门拒绝：有用户明细及内部标识投影与当前隐私契约冲突，也有退款计算失败，不能把全部失败解释为安全策略正确。知识answer-001/002/005关键词覆盖不足；关键词与引用是自动代理指标，未经人工逐句事实核查，不等同于全部答案正确。

逐题证据在docs/evaluation/premerge_routing_live.json、premerge_sql_live.json、premerge_answers_live.json、premerge_safety_live.json；冻结文件哈希和提供方usage在premerge_model_usage.json。

### Token、延迟与调用费用

共215个实际HTTP请求均200；C不重复计费，安全12题未调用模型。

| 实际调用组 | 请求 | 输入Token（命中/未命中） | 输出Token | HTTP P50/P95 ms | 用量费用CNY |
|---|---:|---:|---:|---:|---:|
| 路由B，100+25+10+30题 | 141 | 18048 / 19856 | 10481 | 2728 / 6028 | 0.06214096 |
| MySQL SQL | 18 | 6656 / 5746 | 6499 | 3245 / 7042 | 0.03187512 |
| 历史50题SQL+答案 | 56 | 6272 / 19282 | 30005 | 3299 / 12599 | 0.13942744 |
| 合计 | 215 | 30976 / 44884 | 46985 | 分组报告 | **0.23344352** |

总Token122845。历史50题端到端P50/P95为5295/13552ms；MySQL20题6323/12124ms；新路由B2833/4192ms，C配对回放1488/4192ms。端到端含策略、检索及重试，与单次HTTP延迟不可混用。

费用按响应usage及当次空闲时段价格计算：缓存命中输入0.02元/百万、未命中1元/百万、输出4元/百万，含失败候选/重试。是实际用量的费用计算，不是控制台扣费凭证；不含前一日中断调用与认证探针。旧评测器漏计部分已从独立HTTP记录恢复，原统计保留供核对。

## 开发/校准与改进建议

先提交数据（7055882），再运行25题五类均衡路由开发集与16题真实BGE校准集。与旧题无规范化精确重复；由同一审计者编写且受已知问题类别启发，是开发数据，不能称盲测或统计独立。协议在agent_core/eval/PREMERGE_CALIBRATION.md，结果在docs/evaluation/premerge_calibration.json。

路由开发集6/25（24%）：5道data及1道blocked正确，其余均落入data。问题集中于知识/双任务语义、模糊续问和安全变体。建议在开发集研究缺实体先澄清、低置信语义分类、双任务识别与安全优先，再由独立人员出全新盲测题。不能把原审计题逐字加进正则后宣称泛化提升；本轮不替换生产规则。

BGE无答案top1分数约0.450–0.556，可回答约0.538–0.670，存在重叠；1/(1+distance)不是正确概率，0.4无法有效拒答。预先声明的阈值扫描：

| 开发阈值 | Recall@3（8有答案） | 无答案拒答（8无答案） |
|---|---:|---:|
| 0.40 | 50% | 0% |
| 0.50 | 50% | 62.5% |
| 0.55 | 37.5% | 87.5% |
| 0.60 | 25% | 100% |
| ≥0.70 | 0% | 100% |

0.50仅为后续验证候选，不改生产默认值；单提阈值损害召回，接近业务但无知识证据的问题仍会误命中。应补近域无答案开发样本、验证证据覆盖/拒答策略，再用新盲测验收。未启用Hybrid或Reranker，未用原18题扫阈值宣称提升。

README已统一旧10题历史60%、当前回归90%、新30题56.67%的口径。旧heldout标签虽冻结，其失败模式已用于后续安全加固；2026-09-04/10-13报告保留历史数字并加时效说明。

## Docker运行结果

隔离项目acip-premerge、随机测试凭据、独立卷，入口只绑定127.0.0.1:18097，不使用业务库或真实模型Key。LF修复后bootstrap/迁移/列级grant完成，MySQL/Redis/后端/两端Streamlit健康，Nginx启动。

102287行数据、迁移0002；匿名BI/AI均401，登录后均200；两端授权WebSocket握手通过；health/nginx-health/docs正常。证据docs/evaluation/premerge_docker.json。远端Docker Smoke独立验证空卷构建。没有执行公网TLS、浏览器逐项交互、Langfuse界面或多worker/Redis故障容量验收。

## 安全风险与所有者操作

- 原Gitleaks action只扫事件提交，完整checkout不代表全历史扫描。现新增显式HEAD祖先扫描，保留失败。基线112提交发现4条：1条公开WebSocket nonce误报、3条旧材料access_token。原全部本地refs七条属于不同范围，不混用；后续提交只增加扫描提交数。
- 旧三条token进一步核实为**截断JWT示例误报**：0390d620旧材料636/692/710行均处于/api/auth/login示例，值含字面量省略号，有4个片段；PyJWT在本地禁用签名验证也抛DecodeError，不是可用的三段JWT。证据premerge_credentials_review.json，没有输出原值或用其请求服务，因此无需撤销这些示例。
- 对上述3条截断示例和1条RFC公开WebSocket nonce，仅在.gitleaksignore逐个加入历史精确指纹，附理由。没有按规则、文件路径或包名广泛豁免，也没有豁免真实漏洞；完整历史和事件扫描保留。初始“疑似凭据、需核实”结论已被进一步静态证据更新，未进行远端历史改写。
- 本次聊天中出现的模型Key也应在控制台撤销并替换，仅更新本机root/backend env。临时评测文件在收尾删除；200不是旧Key已撤销的证据。
- Chroma1.5.9仍有5条记录/4个ID：PYSEC-2026-311、3813、3814、3815。2026-10-14包索引最新仍1.5.9，扫描无修复版本。嵌入式PersistentClient、无HTTP服务、禁用trust_remote_code降低部分攻击路径适用性，不代表漏洞消除。保留门禁失败，不降级绕过或新增ignore。[PyPA代码注入告警](https://raw.githubusercontent.com/pypa/advisory-database/main/vulns/chromadb/PYSEC-2026-3814.yaml)。等待可验证上游修复并做兼容回归；正式风险例外需所有者/安全负责人按期限审批，本次没有设置例外。

RAG跨操作原子快照、RFM多worker缓存、模型注入/事实核查与生产TLS仍有边界，本次不做大规模重构。

## 合并条件

当前HEAD的CI/镜像/Docker全部核实，安全门禁风险有可验证处置，所有者确认暴露模型Key处理结果，并明确接受模型/RAG剩余限制后，再判断合并。保持草稿；未经用户确认不合并main。

## 最终本地检查与复现入口

本轮最终全量：**422 passed，2项第三方WebSocket弃用警告，53.01秒**，显式启用真实Docker认证测试，无跳过。Ruff与compileall通过。执行输出保存在忽略的output/premerge-final-tests.txt。

Bandit 1.9.4中高危为0；保留已有两处仅用于临时测试socket的定向nosec，不增加抑制。真实模型CLI提供方标签由实际base URL主机生成，修复原先硬编码为智谱的问题。

```powershell
$env:RUN_DOCKER_AUTH_TESTS='1'
.venv\Scripts\python.exe -m pytest --tb=short -q
.venv\Scripts\python.exe -m ruff check agent_core backend ai-ecommerce-assistant scripts streamlit_app.py
.venv\Scripts\python.exe -m agent_core.eval.premerge_calibration
# 真实模型命令会调用外部API；须仅在本机配置有效凭据，不将Key写进命令或提交。
.venv\Scripts\python.exe -m agent_core.eval.audit_experiments --section routing --live-model --output output/premerge-routing-repeat.json
.venv\Scripts\python.exe -m agent_core.eval.audit_experiments --section sql --live-model --output output/premerge-sql-repeat.json
.venv\Scripts\python.exe -m agent_core.live_evaluation --allow-network --output output/premerge-answers-repeat.json
```

模型命令须由环境提供匹配的LLM_API_KEY、LLM_BASE_URL与LLM_MODEL；旧env未更新仍会401。历史50题CLI默认提供方/模型并非本次DeepSeek，必须显式设置这两个环境变量。本次费用由独立HTTP记录计算，常规CLI不能自动重建提供方缓存命中明细，不应以其缺失字段宣称零费用。
