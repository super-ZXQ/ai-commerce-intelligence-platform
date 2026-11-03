"""依赖锁定契约：版本必须精确 pin，且同一个包在整个仓库里只能有一个版本。

镜像和本地环境此前分别按各自的文件安装，同一个 pandas 可能装出不同版本；
构建可复现的前提是「声明即所装」，这两条守卫把它变成 CI 硬约束。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_FILES = (
    "requirements.txt",
    "requirements.streamlit.txt",
    "backend/requirements.txt",
    "backend/requirements-dev.txt",
    "ai-ecommerce-assistant/requirements.txt",
)
# 只接受 name==version（可带 extras 与环境标记）；>=、~=、<= 与裸包名都算未锁定。
_PINNED = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*"
    r"(?P<version>[A-Za-z0-9._+!-]+)(?:\s*;.*)?$"
)


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _iter_lines(relative: str) -> list[tuple[str, int, str]]:
    path = _ROOT / relative
    assert path.is_file(), f"依赖清单 {relative} 不存在，守卫会空转"
    entries: list[tuple[str, int, str]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        # 空行与 pip 选项/子依赖引用（-r、--flags）不参与版本校验
        if not line or line.startswith("-"):
            continue
        entries.append((relative, lineno, line))
    return entries


def test_all_requirement_files_are_discovered() -> None:
    """新增 requirements 文件必须同时登记进来，否则守卫会静默漏掉它。"""
    found = {
        str(path.relative_to(_ROOT))
        for path in _ROOT.rglob("requirements*.txt")
        if ".venv" not in path.parts
    }

    assert found == set(_FILES), f"依赖清单未全部纳入守卫: {sorted(found ^ set(_FILES))}"


def test_every_dependency_is_exactly_pinned() -> None:
    unpinned = [
        f"{origin}:{lineno}: {line}"
        for relative in _FILES
        for origin, lineno, line in _iter_lines(relative)
        if not _PINNED.match(line)
    ]

    assert not unpinned, "以下依赖未精确锁定版本:\n" + "\n".join(unpinned)


def test_one_package_can_only_have_one_version() -> None:
    declared: dict[str, dict[str, list[str]]] = {}
    for relative in _FILES:
        for origin, lineno, line in _iter_lines(relative):
            match = _PINNED.match(line)
            assert match, f"{origin}:{lineno} 未锁定，先过上一条守卫"
            name = _canonical(match.group("name"))
            declared.setdefault(name, {}).setdefault(match.group("version"), []).append(
                f"{origin}:{lineno}"
            )

    conflicts = [
        f"{name}: " + "; ".join(f"{version} <- {', '.join(sites)}" for version, sites in sorted(places.items()))
        for name, places in sorted(declared.items())
        if len(places) > 1
    ]

    assert not conflicts, "同一包在不同清单里版本不一致:\n" + "\n".join(conflicts)


@pytest.mark.skipif(
    os.environ.get("CHECK_VENV_DRIFT") != "1",
    reason="仅在本地显式设置 CHECK_VENV_DRIFT=1 时检查当前虚拟环境与声明版本的偏差",
)
def test_local_environment_matches_declared_versions() -> None:
    """重装依赖后用这条守卫确认本地 venv 已和清单对齐。"""
    from importlib.metadata import PackageNotFoundError, version

    drift: list[str] = []
    for relative in _FILES:
        for origin, lineno, line in _iter_lines(relative):
            match = _PINNED.match(line)
            assert match
            name = match.group("name")
            try:
                installed = version(_canonical(name))
            except PackageNotFoundError:
                continue  # 本文件只校验当前环境里装了的包
            if installed != match.group("version"):
                drift.append(f"{origin}:{lineno} {name} 声明 {match.group('version')}，实际 {installed}")

    assert not drift, "本地环境比清单声明的版本新/旧:\n" + "\n".join(sorted(set(drift)))
