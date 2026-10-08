"""从仓库权威来源生成 camctl 包资源。

权威来源只在仓库根的 ``protocol`` 与 ``docs/camctl`` 维护；
``src/camctl/_resources`` 是构建生成的副本，禁止手工修改。
本模块同时被 hatch 构建钩子调用，保证构建产物总是携带最新权威输入。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# 包内资源名（相对 camctl/_resources，使用 "/" 分隔）-> 仓库权威来源（相对仓库根）。
RESOURCE_SOURCES: dict[str, str] = {
    "protocol/plan.schema.json": "protocol/schemas/plan.schema.json",
    "protocol/status-report.schema.json": "protocol/schemas/status-report.schema.json",
    "protocol/capabilities.schema.json": "protocol/schemas/capabilities.schema.json",
    "protocol/host-notification.schema.json": "protocol/schemas/host-notification.schema.json",
    "protocol/workflow-codes.json": "protocol/errors/workflow-codes.json",
    "sql/core.sql": "docs/camctl/database/schema/core.sql",
    "sql/workflows.sql": "docs/camctl/database/schema/workflows.sql",
    "sql/files.sql": "docs/camctl/database/schema/files.sql",
    "sql/operations.sql": "docs/camctl/database/schema/operations.sql",
    "sql/reports.sql": "docs/camctl/database/schema/reports.sql",
    "sql/history.sql": "docs/camctl/database/schema/history.sql",
    "registry/enum-registry.json": "docs/camctl/database/enum-registry.json",
    "registry/event-transitions.json": "docs/camctl/database/event-transitions.json",
    "registry/report-dependencies.json": "docs/camctl/database/report-dependencies.json",
    "runtime/sqlite-runtime.json": "docs/camctl/sqlite-runtime.json",
}


def repo_root_of(project_dir: Path) -> Path:
    """apps/camctl 项目目录对应的仓库根。"""
    return project_dir.parents[1]


def resource_root(project_dir: Path) -> Path:
    return project_dir / "src" / "camctl" / "_resources"


def sync_package_resources(project_dir: Path, *, check: bool = False, root: Path | None = None) -> list[str]:
    """把权威来源同步到包资源目录。

    check 为 True 时不写入，只返回漂移描述；返回空列表表示完全一致。
    同步前先清空目标目录，保证被移除的权威来源不会在包内残留。
    """
    repo_root = root if root is not None else repo_root_of(project_dir)
    target_root = resource_root(project_dir)
    problems: list[str] = []

    existing: set[Path] = set()
    if target_root.is_dir():
        existing = {p.relative_to(target_root).as_posix() for p in target_root.rglob("*") if p.is_file()}
    expected = set(RESOURCE_SOURCES)

    for name, source_rel in sorted(RESOURCE_SOURCES.items()):
        source = repo_root / source_rel
        target = target_root / Path(*name.split("/"))
        if not source.is_file():
            problems.append(f"权威来源缺失: {source_rel}")
            continue
        if check:
            if not target.is_file():
                problems.append(f"包资源缺失: {name}")
            elif target.read_bytes() != source.read_bytes():
                problems.append(f"包资源与权威来源不一致: {name}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    if not check and target_root.is_dir():
        stale = existing - expected
        for name in stale:
            (target_root / Path(*name.split("/"))).unlink()
        for directory in sorted(
            (d for d in target_root.rglob("*") if d.is_dir()),
            key=lambda d: len(d.parts),
            reverse=True,
        ):
            if not any(directory.iterdir()):
                directory.rmdir()

    extra = existing - expected
    for name in sorted(extra):
        problems.append(f"包内存在未登记资源: {name}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只核对不写入，漂移时以非零退出")
    parser.add_argument("--root", type=Path, default=None, help="仓库根目录（默认按脚本位置推断）")
    args = parser.parse_args(argv)

    project_dir = Path(__file__).resolve().parents[1]
    problems = sync_package_resources(project_dir, check=args.check, root=args.root)
    if problems:
        for line in problems:
            print(line, file=sys.stderr)
        return 1
    if not args.check:
        print(f"已同步 {len(RESOURCE_SOURCES)} 项包资源")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
