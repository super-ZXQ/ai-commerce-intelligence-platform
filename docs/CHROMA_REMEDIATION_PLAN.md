# Chroma阻塞处置方案（待范围确认）

日期：2026-10-14。用户已授权推送并合并；本方案不将该授权解释为允许关闭安全扫描或忽略真实漏洞。

## 已重新核实的事实

- 17d7ed2的CI、Docker Smoke、Release Docker Images全部成功。安全工作流仅AI依赖扫描失败；Gitleaks、Bandit、后端依赖通过。
- PyPI与本机包索引最新仍为chromadb 1.5.9；PyPA记录四个不同漏洞，当前没有已发布修复版本。
- 上游RBAC修复PR #7602仍Open，仅对应部分漏洞，不能当作四个问题已修复；不采用未经合并、未经完整回归的第三方分支冒充安全版本。
- 不降级到受其他漏洞影响的旧版，不伪造版本号，不用--no-deps、排除包、改扫描范围或ignore漏洞使门禁变绿。

来源：[PyPI](https://pypi.org/project/chromadb/)、[上游PR7602](https://github.com/chroma-core/chroma/pull/7602)、[预认证代码注入](https://raw.githubusercontent.com/pypa/advisory-database/main/vulns/chromadb/PYSEC-2026-311.yaml)、[RBAC告警](https://raw.githubusercontent.com/pypa/advisory-database/main/vulns/chromadb/PYSEC-2026-3815.yaml)。

## 路径A：保留Chroma，等待可验证上游修复

不改变技术栈与用户数据。已完成的技术修复继续保留在PR #10；pip-audit保持失败，暂不合并。修复版本发布后重新生成锁、执行全部回归、真实检索对照与Docker验收，再合并。这是改动最少的路径，但无法保证完成日期。

## 路径B：替换本地向量存储，保留BGE与RAG接口

适用于希望现在解决依赖阻塞、并同意改变Chroma技术栈约束的情况。当前知识库只有60个chunk，可评估标准库SQLite持久化加已有NumPy精确距离检索；不增加新的向量数据库或外部服务。

最小边界：

1. 保留VectorStore的add_documents/search/delete_documents/count/get_all_ids/delete_all/reset接口，Retriever缓存、knowledge.version、执行轨迹和指标继续使用；保留BGE、LangGraph、业务页面。
2. SQLite按collection/doc_id保存正文、JSON metadata、固定维度数值向量，使用参数化SQL与事务upsert。NumPy按原L2距离及1/(1+distance)计算分数，保留top-k、阈值与已有metadata过滤语义，不使用pickle或动态模型代码。
3. 先盘点用户现有知识与向量。新存储使用独立文件；原Chroma目录和源Markdown保留，不删除、不覆盖。可由原来源重建；若有仅存在于旧库的内容，须先导出核对完整性，不能静默丢失。
4. 从正常运行依赖移除chromadb/langchain-chroma并重新生成锁；同步Docker、文档及相关评测名称。历史Chroma评测快照仍保留原实现名称，不能冒充新实现结果。
5. 修改主要限于rag/vector_store.py、直接依赖/锁、相关存储测试、迁移说明与运行文档；需要核对完整调用和过滤语义后才能承诺最终diff规模，不借机改路由或答案逻辑。

验证：原增量构建、删除、失败回滚、过滤、跨进程缓存失效均回归；新旧真实BGE top-k/分数对照；重启持久化和并发读写；实际源文档/用户内容无丢失；依赖扫描不忽略任何真实漏洞；本地全量、远端CI/全栈/镜像通过后，按用户已有授权合并。

代价：精确检索为O(Nd)，适合当前小知识库，不能据此宣称大规模检索性能；存储替换本身仍需实现、数据核验与回归。本方案还没有实施，不能将它报告为已修复或门禁已通过。

## 模型Key与合并

聊天中暴露的真实模型Key只能由提供方控制台或具备相应管理权限的工具撤销，现有工具没有该能力。用户需在DeepSeek控制台撤销暴露Key，创建仅保存在本机的新Key并更新root/backend配置，不在聊天或PR发送原值。已完成真实评测，无需为存储修复重复付费跑所有模型题。

合并授权已经收到，无须重复询问是否可以合并。但在此前“不要大规模重构”和保留技术栈的约束下，实施路径B需要明确存储替换范围；路径A则保留安全阻塞。当前不强制合并红色安全检查。
