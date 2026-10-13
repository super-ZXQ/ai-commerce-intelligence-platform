# 可复现构建：依赖锁文件说明

## 为什么需要锁文件

`requirements.txt` 只钉死**直接依赖**版本，传递依赖仍由 pip 在安装时按 PyPI 最新发布解析。这意味着：同一份 `requirements.txt`，今年 1 月装的 `langchain-core==0.3.x`，明年可能解析出 `0.4.x`——同一仓库、同一分支，CI 结果会随上游发布时间漂移。锁文件把**完整依赖闭包**（含传递依赖）钉死到具体版本，保证任何机器、任何时间安装出的环境一致。

## 工具选择：uv pip compile

选用 `uv pip compile`（而非 pip-tools）：

| 对比项 | uv pip compile | pip-tools compile |
|--------|---------------|-------------------|
| 解析速度 | 快（Rust 实现，本项目 AI 侧含 torch，秒级完成） | 慢（纯 Python，torch 闭包常需数分钟） |
| 输出格式 | 标准 `pip install -r` 兼容，带环境 marker | 相同 |
| 额外收益 | 装好后同时可用于快速建 venv 验证 | 无 |

`--universal` 输出跨平台锁（带 `sys_platform` 等 marker），Windows 开发机与 Linux CI/Docker 用同一份文件。

## 锁文件清单

| 文件 | 范围 | 用途 |
|------|------|------|
| `backend/requirements.lock` | 后端生产依赖 | Docker 镜像构建、生产部署 |
| `backend/requirements-dev.lock` | 后端 + 测试/静态工具（pytest/ruff/bandit/pip-audit/alembic） | CI 后端测试、本地开发 |
| `ai-ecommerce-assistant/requirements.lock` | AI 助手运行依赖（含 torch） | CI AI 测试、干净环境复现 |

所有锁按 `--python-version 3.12` 解析（CI 与 Docker 的 Python 版本），本地 3.13 亦兼容（锁内 marker 覆盖两个版本）。

## 日常使用

安装（CI / 新环境）：

```powershell
# 后端（含开发工具）
pip install -r backend/requirements-dev.lock

# AI 助手
pip install -r ai-ecommerce-assistant/requirements.lock
pip install pytest==9.1.0 pytest-asyncio==1.4.0   # 测试工具不进运行锁
```

升级某个依赖的正确流程（**不要直接改锁文件**）：

```powershell
# 1. 改 requirements.txt 中的直接依赖版本
# 2. 重新生成锁（三份各跑一次）
uv pip compile backend/requirements.txt --universal --python-version 3.12 -o backend/requirements.lock
uv pip compile backend/requirements-dev.txt --universal --python-version 3.12 -o backend/requirements-dev.lock
uv pip compile ai-ecommerce-assistant/requirements.txt --universal --python-version 3.12 -o ai-ecommerce-assistant/requirements.lock

# 3. 干净 venv 安装锁文件，跑全量测试确认无回归
# 4. 提交 requirements.txt + 锁文件两个改动
```

## 已知特例：AI 助手 Dockerfile 不使用锁文件

`ai-ecommerce-assistant/Dockerfile` 先从 PyTorch CPU 专属源安装 `torch==2.13.0+cpu`（避免拖入约数 GB 的 CUDA 依赖），再安装其余依赖。由于 `requirements.lock` 按 PyPI 解析，含 `sys_platform == 'linux'` 激活的 `nvidia-*` / `cuda-*` 条目，与该路径互斥。因此该镜像继续使用 `requirements.txt`——其直接依赖本身已全部 `==` 钉死，构建仍可复现。锁文件本身对 AI 侧依然有效：用于 CI 测试环境与本地干净复现。

## 验证记录（2026-10-13）

- 用本仓库锁文件在全新虚拟环境安装后实测：后端测试 177 项全绿、AI 助手测试 88 项全绿，与现有开发环境（265 项基线）一致。
- 生成锁时未发生大版本升级：直接依赖版本与 `requirements.txt` 钉死值完全一致，漂移仅出现在此前未锁定的传递依赖。
