"""供部署更新使用的状态库检查与副本迁移入口。"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path


# 起点到下一格式的登记。每个迁移负责校验其历史和进度语义。
MIGRATIONS = {}


def migrate_copy(config_path: Path, state_db: Path) -> dict:
    """在独立副本上应用已登记迁移，返回核验后的数据库身份。"""
    from camctl.bootstrap.application import ConfigAdapter
    from camctl.persistence.runtime import (
        CURRENT_FORMAT_VERSION, _FORMAT_VERSIONS, DbConfig, DbOpenMode,
        open_existing, verify_directory_binding,
    )

    if not config_path.is_file() or not state_db.is_file():
        raise ValueError('配置或数据库副本不存在')
    config = ConfigAdapter(Path.home()).load(config_path)
    if state_db.samefile(Path(config.paths.state_db)):
        raise ValueError('迁移目标必须是独立数据库副本')
    options = DbConfig(busy_timeout_ms=config.database.busy_timeout_ms)
    owned = open_existing(state_db, DbOpenMode.EXISTING_RO, options)
    try:
        verify_directory_binding(owned.metadata, config.paths.staging, config.paths.ready,
                                 config.paths.processing)
        identity = asdict(owned.metadata)
    finally:
        owned.connection.close()

    version = identity['format_version']
    baseline = int(_FORMAT_VERSIONS.V9)
    if version < baseline or version > CURRENT_FORMAT_VERSION:
        raise ValueError(f'数据库格式 {version} 超出更新维护范围')
    path = []
    cursor = version
    while cursor != CURRENT_FORMAT_VERSION:
        step = MIGRATIONS.get(cursor)
        if step is None:
            raise ValueError(f'数据库格式 {cursor} 没有登记迁移路径')
        target, migrate = step
        if not cursor < target <= CURRENT_FORMAT_VERSION:
            raise ValueError(f'数据库格式 {cursor} 的迁移登记无效')
        path.append((target, migrate))
        cursor = target

    for target, migrate in path:
        migrate(state_db, identity)
        owned = open_existing(state_db, DbOpenMode.EXISTING_RO, options)
        try:
            verify_directory_binding(owned.metadata, config.paths.staging, config.paths.ready,
                                     config.paths.processing)
            actual = asdict(owned.metadata)
        finally:
            owned.connection.close()
        expected = dict(identity, format_version=target)
        if actual != expected:
            raise ValueError('迁移后的数据库身份或目录绑定发生变化')
        identity = actual
    return identity


def inspect_deployment(config_path: Path) -> dict:
    from camctl.bootstrap.application import ConfigAdapter
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing, verify_directory_binding

    if not config_path.is_file():
        raise ValueError(f'配置文件不存在或不是普通文件: {config_path}')
    config = ConfigAdapter(Path.home()).load(config_path)
    state_db = Path(config.paths.state_db).resolve()
    if not state_db.is_file():
        raise ValueError(f'原状态库不存在或不是普通文件: {state_db}')
    owned = open_existing(state_db, DbOpenMode.EXISTING_RO,
                          DbConfig(busy_timeout_ms=config.database.busy_timeout_ms))
    try:
        verify_directory_binding(owned.metadata, config.paths.staging, config.paths.ready,
                                 config.paths.processing)
        return {'state_db': str(state_db), 'metadata': asdict(owned.metadata)}
    finally:
        owned.connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--migrate-copy', type=Path, help='在独立状态库副本上执行已登记迁移')
    args = parser.parse_args()
    try:
        result = (migrate_copy(args.config, args.migrate_copy) if args.migrate_copy is not None
                  else inspect_deployment(args.config))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except Exception as error:
        from camctl.operations.error_diagnostics import exception_diagnostic
        parser.exit(1, json.dumps(exception_diagnostic(error), ensure_ascii=False, default=repr) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
