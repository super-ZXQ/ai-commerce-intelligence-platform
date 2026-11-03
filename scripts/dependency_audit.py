"""依赖漏洞门禁：审计直接 pin 的依赖，拦截已有修复版本的公告。

为什么用 `--no-deps --disable-pip`：
- 只审计 requirements 里显式 pin 的版本，不解析传递依赖，CI 不需要下载
  torch（约 900MB）等大包，几秒内完成；
- 传递依赖的漏洞由上游约束决定（pillow 被 fastembed 约束在 <13.0），
  已在 docs/OPERATIONS.md 9.1 记录并定期复核，不在这里重复报红。

行为：
- 直接依赖存在"有 fix 版本"的公告 → 退出码 1，必须升级 pin；
- 直接依赖的公告暂无 fix 版本（如 chromadb 1.5.9）→ 打印 accepted，退出码 0。

用法：python scripts/dependency_audit.py（需要同一环境安装 pip-audit）
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = [
    ROOT / "backend/requirements.txt",
    ROOT / "requirements.streamlit.txt",
    ROOT / "ai-ecommerce-assistant/requirements.txt",
]


def audit(path: Path) -> dict:
    # 用当前解释器的模块入口，避免依赖 PATH 中 pip-audit 的位置（本地/CI 一致）。
    proc = subprocess.run(  # noqa: S603 - 参数固定，仅 requirements 路径来自仓库常量
        [
            sys.executable,
            "-m",
            "pip_audit",
            "-r",
            str(path),
            "--no-deps",
            "--disable-pip",
            "--format",
            "json",
            "--progress-spinner",
            "off",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # pip-audit 用退出码 1 表示"发现漏洞"；2 及以上代表网络/解析错误，必须先修管道。
    if proc.returncode not in (0, 1):
        print(proc.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"pip-audit 执行失败（exit {proc.returncode}）：{path}")
    if not proc.stdout.strip():
        # 解析器崩溃时 pip-audit 可能只写 stderr，不能把空输出当成"无漏洞"。
        print(proc.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"pip-audit 未返回 JSON 报告：{path}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"pip-audit 输出不是合法 JSON：{path}: {exc}") from exc


def main() -> int:
    blocking: list[str] = []
    accepted: list[str] = []

    for path in REQUIREMENTS:
        report = audit(path)
        for dep in report.get("dependencies", []):
            name = dep.get("name", "?")
            version = dep.get("version", "?")
            for vuln in dep.get("vulns", []):
                fixes = vuln.get("fix_versions") or []
                entry = (
                    f"{path.relative_to(ROOT)}: {name} {version} "
                    f"{vuln.get('id', '?')} fix={','.join(fixes) or '-'}"
                )
                # --no-deps 下每条依赖都是 requirements 里显式 pin 的，
                # 有 fix 就意味着可以直接升级修复。
                (blocking if fixes else accepted).append(entry)

    for entry in accepted:
        print(f"[accepted] {entry}")
    for entry in blocking:
        print(f"[blocking] {entry}", file=sys.stderr)

    if blocking:
        print(
            "\n存在可直接升级修复的依赖漏洞；请升级对应 pin 后重新运行。",
            file=sys.stderr,
        )
        return 1

    print(f"\n依赖审计通过：{len(accepted)} 条公告暂无修复版本，已记录待复核。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
