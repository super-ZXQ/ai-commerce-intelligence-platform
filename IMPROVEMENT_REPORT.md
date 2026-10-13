# 面试竞争力导向加固 — 改进报告

- **执行日期**：2026-10-13
- **分支**：`codex/order-event-production-like`（9 个提交，未 push，等你审阅后自行推送）
- **环境**：Windows + `.venv`（Python 3.12），本地无 Docker、无 `.venv-ai`（两套测试均在 `.venv` 实跑）

## 一、变更清单（按提交）

| 提交 | 类型 | 内容 |
|------|------|------|
| `bf80ea4` | chore | 提交既有 WIP：移除 `.impeccable/` 跟踪 |
| `c242e6f` | feat | 提交既有 WIP：Swagger 宽屏居中 + 空 SQL 结果不渲染空框 |
| `33a8cbc` | chore | 新增 MIT LICENSE；`git rm -r --cached output/`（13 个本地产物解除跟踪，文件保留）；清理 `.gitignore`（删 `-w` 脏行，新增 `.impeccable/` 与 `eval/feedback.jsonl` 忽略） |
| `c91887a` | docs | 新增 `docs/data_provenance.md`（来源/字段/脱敏声明/清洗流程/面试应答要点）；AGENTS.md 记录 bi_style.py 保留根目录的决策 |
| `28567d9` | feat | 新增 `.github/workflows/security.yml`（gitleaks + pip-audit×2 + bandit 中高危门禁）；修复 6 处 B324 弱 MD5；监控端点 `MONITOR_AUTH_REQUIRED` 开关（默认关）；4 项新测试 |
| `2e6e158` | build | 三套依赖锁文件（backend 生产/开发、AI 助手，uv compile + `--python-version 3.12`）；CI 三处安装切锁；后端 Dockerfile 切锁；根 requirements.txt 钉死；`docs/reproducible-builds.md` |
| `fc0f7e1` | feat | Langfuse trace 级观测：`agent_core/observability.py`（开关/env 驱动、未配置静默跳过）、Runtime 挂 LangChain CallbackHandler、`docker-compose.observability.yml`（v3 自托管栈）、`scripts/observability_stats.py`、`docs/observability.md`；16 项新测试 |
| `00f79e5` | feat | 评测扩容 15→50 条（本地全验证）；新增 12 条安全拒答端到端评测 + `live_safety_evaluation.py`；**修复路由越权缺口**；`agent_core/eval/adversarial/` 出题模板 + 流程文档；README 数字对账 |
| `3e7fb1d` | docs | `docs/deploy-vps.md`（域名/HTTPS/防火墙/备份/回滚/更新）、`docs/demo_script.md`（2 分钟分镜）；README 在线演示节注明本地入口 |

## 二、测试前后对比（全部实跑可复现）

```powershell
# 复现命令
.\.venv\Scripts\python.exe -m pytest backend\tests\ ai-ecommerce-assistant\tests\ -q -c pytest.ini
.\.venv\Scripts\python.exe -m agent_core.evaluation        # 135 条路由 + 15 条检索离线评测
.\.venv\Scripts\python.exe -m ruff check agent_core backend ai-ecommerce-assistant scripts streamlit_app.py
```

| 指标 | 加固前（README 声称 / 实跑） | 加固后（2026-10-13 实跑） |
|------|------------------------------|---------------------------|
| pytest 全量 | README 称 252（实跑基线 265，README 过时） | **301 passed / 0 failed**（backend 213 + AI 88） |
| 路由离线评测 | 各类别 100% | 各类别 100%（**零回归**，含 10 条冻结留出） |
| 词法检索 | recall@3 100%、MRR 0.7444 | 不变（未动） |
| bandit 中高危 | 6 个高危（B324 弱 MD5） | **0** |
| ruff | 通过 | 通过 |
| 干净 venv 锁版本复验 | 未验证过 | backend 177 + AI 88 全绿（锁安装后实测） |

**说明**：README 原「2026-09-18 实测 252 项」本身已过时（订单事件分支新增 13 项后基线即 265）；本次已按 301 项实测对账更新。50 条 GLM 真实模型评测与 12 条安全拒答的**端到端新快照未跑**（需 Key，见第五节）。

## 三、修复的漏洞（按严重度）

| 严重度 | 漏洞 | 证据 | 修复 |
|--------|------|------|------|
| **高** | **路由越权缺口**：隐私宾语变体（「导出客户资料」「收货地址」）、记录行写操作（「订单表里3月份的记录行都删掉」）、字段值改写（「订单金额改成9999」）、定向插入（「往订单表里插入一条」）、泛宾语盘点（「数据库里都有什么数据」）均路由为 `data`，仅靠下游 AST 门控兜底，回答语义是「执行失败」而非安全拒绝 | 12 条安全题离线验证：修复前仅 5/12 拦截 | `routing.py` 新增五组模式（含数据字典问句豁免，防误伤「用户信息字段有哪些」）；修复后 12/12 拦截、135 条路由基线零回归、13 条正常查询零误伤；测试 `test_routing_hardening_blocks_overreach_without_false_positives` 守护 |
| 中 | 6 处 B324 弱 MD5（缓存键/ID 用途，非密码学场景） | bandit 本地预跑 | 统一 `usedforsecurity=False`；复跑中高危清零 |
| 中 | 监控端点（`/metrics`、`/api/monitor/*`、`/health/detailed`）无任何访问控制，公网部署暴露内部指标 | 源码核对 | `MONITOR_AUTH_REQUIRED` 开关（默认关，向后兼容），4 项测试覆盖两态；README 注明生产开启 |
| 中 | 无依赖锁：传递依赖随 PyPI 漂移，CI 结果不可复现 | 盘点 | 三份锁文件 + CI/Docker 切锁 + 升级流程文档 |
| 中 | README 声称 MIT 但无 LICENSE 文件 | 盘点 | MIT LICENSE（署名 super-ZXQ） |
| 中 | `output/` 13 个产物（1.6MB CSV + 5 PNG）被 git 跟踪 | 盘点 | `--cached` untrack，本地保留 |
| 低 | `.gitignore` 混入 `-w` 脏行；`feedback.jsonl`（含真实提问）有误提交风险 | 盘点 | 已清理并补忽略规则 |
| 低 | 根 `requirements.txt` 裸名依赖 | 盘点 | 全部钉死 |

**顺带修复的失真**：README「252 项」→ 301 项；「15 条 GLM 评测」→ 50 条（新快照待跑，文档已注明）。

## 四、发现但未修复 / 有意不做

| 事项 | 原因 |
|------|------|
| `bi_style.py` 保留根目录 | `streamlit_app.py` 伴生模块，移动要连带改 Docker 构建路径与 CI ruff 范围，收益不成比例；决策已写入 AGENTS.md |
| 本地无 `.venv-ai` | 环境事实（langchain 版本冲突的有意设计在你原环境）；两套测试在 `.venv` 实跑通过，报告如实记录 |
| AI 助手 Dockerfile 不用锁文件 | universal 锁含 `sys_platform == 'linux'` 激活的 nvidia/cuda 条目，与 `torch==2.13.0+cpu` CPU 镜像路径互斥；requirements.txt 直接依赖已钉死，注释已说明 |
| 原有 15 条评测中 3 条 answer 关键词不在检索 snippet | 既有设计（answer-002/003/005），非本轮引入，未改动以免破坏基线口径 |
| Swagger 居中依赖未跟踪的 `swagger.css` | 已随 WIP 提交入库，闭环 |

## 五、需你人工完成的事项

| # | 事项 | 说明 |
|---|------|------|
| 1 | **推送 9 个提交** | 未执行任何 push；分支 `codex/order-event-production-like`，你审阅后自行推送或开 PR |
| 2 | **Langfuse 端到端验证** | 本机 Docker 不可用：SDK API 已用假模块 + 真实包（3.15.0）双重验证，但「浏览器看到 trace」需你 `docker compose -f docker-compose.observability.yml up -d` 后确认；`docs/observability.md` 已标 TODO |
| 3 | **跑 50 条 GLM 评测 + 12 条安全拒答** | 需 `LLM_API_KEY`：`.\.venv\Scripts\python.exe -m agent_core.live_evaluation --allow-network` 与 `-m agent_core.live_safety_evaluation --allow-network`；跑完把快照数字给我或自行更新 README |
| 4 | **security.yml 首跑确认** | 沙箱断 pypi.org/OSV/GitHub，pip-audit/gitleaks 本地预跑不可行；bandit 已本地验证通过。CI 首跑若 pip-audit 报存量漏洞，按报告升级或加豁免 |
| 5 | **轮换 LLM_API_KEY** | 本地 `.env` 中旧值标注待轮换（未入库，安全），轮换是你的人工待办 |
| 6 | **找同学盲出敌意题** | `agent_core/eval/adversarial/` 已备好模板和流程文档（含盲出原则），目录为空未造假 |
| 7 | **公网部署** | `docs/deploy-vps.md` 全流程就绪；真实域名/证书/备份演练需你执行；完成后把演示 URL 填回 README「在线演示」节 |
| 8 | **确认 LICENSE 署名** | 当前为 GitHub 账号 `super-ZXQ`，如需真名改一行即可 |

## 六、面试叙事要点（这轮加固新增的弹药）

1. **「安全边界怎么设计的」**：路由层确定性拦截（正则 + 豁免负向断言）+ SQL AST 只读门控 + 12 条端到端安全评测，三层防线；「漏拦率从 7/12 到 0」是可讲的量化改进。
2. **「为什么不用纯 ReAct」**：确定性工作流 + 135 条路由回归（含冻结留出集）+ 50 条真实模型评测，每一步可回归。
3. **「构建可复现吗」**：三套锁文件，干净 venv 复验 265 项全绿；升级流程写进文档。
4. **「可观测性怎么分层」**：Prometheus 看森林（聚合/告警）、Langfuse 看树木（trace 回放），未配置静默降级——讲清「为什么要两套」。
5. **「数据哪来的、能公开吗」**：`docs/data_provenance.md`：脱敏扫描零命中 + 清洗流程 + 可公开理由，面试必被问。
