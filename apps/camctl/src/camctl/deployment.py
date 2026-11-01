"""供部署更新使用的状态库检查与副本迁移入口。"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path


# 起点到下一格式的登记。每个迁移负责校验其历史和进度语义。
MIGRATIONS = {}


def inspect_configuration(config_path: Path | None) -> dict:
    """按普通入口加载配置，交付更新前核对所需的路径。"""
    from camctl.bootstrap.application import ConfigAdapter

    config = ConfigAdapter(Path.home()).load(config_path)
    return {'state_db': str(Path(config.paths.state_db).resolve()),
            'paths': {name: str(getattr(config.paths, name)) for name in ('staging', 'ready', 'processing')}}


def create_database_copy(config_path: Path | None, destination: Path) -> dict:
    """在维护事务拥有的独立位置准备新实例数据库。"""
    from camctl.bootstrap.application import ConfigAdapter
    from camctl.persistence.initialization import _build_initial_database, _fsync_directory
    from camctl.persistence.maintenance_integrity import verify_maintenance_integrity
    from camctl.persistence.runtime import (DbConfig, DbOpenMode, configured_directory_binding,
                                            open_existing, verify_directory_binding)

    if not destination.is_absolute():
        raise ValueError('新数据库准备路径必须为绝对路径')
    config = ConfigAdapter(Path.home()).load(config_path)
    target = Path(config.paths.state_db).resolve()
    if destination.resolve() == target:
        raise ValueError('新数据库必须在独立事务路径准备')
    roots = tuple(getattr(config.paths, name) for name in ('staging', 'ready', 'processing'))
    bindings = tuple(configured_directory_binding(path, name)
                     for name, path in zip(('staging', 'ready', 'processing'), roots))
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    os.close(fd)
    _build_initial_database(destination, *bindings)
    _fsync_directory(destination.parent)
    owned = open_existing(destination, DbOpenMode.EXISTING_RW,
                          DbConfig(busy_timeout_ms=config.database.busy_timeout_ms))
    try:
        verify_maintenance_integrity(owned.connection)
        verify_directory_binding(owned.metadata, *roots)
        result = {'state_db': str(target), 'metadata': asdict(owned.metadata)}
        busy, remaining, checkpointed = owned.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if busy or remaining != checkpointed:
            raise ValueError('新状态库 WAL 无法完成检查点')
    finally:
        owned.connection.close()
    descriptor = os.open(destination, os.O_RDWR | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(destination.parent)
    return result


def prepare_new_directories(config_path: Path | None) -> None:
    """由已持有原库锁的维护事务调用正式目录准备能力。"""
    from camctl.bootstrap.application import ConfigAdapter
    from camctl.persistence.initialization import _prepare_directories

    config = ConfigAdapter(Path.home()).load(config_path)
    _prepare_directories(Path(config.paths.state_db).resolve(), Path(config.paths.staging),
                         Path(config.paths.ready), Path(config.paths.processing))


def migrate_copy(config_path: Path | None, state_db: Path) -> dict:
    """在独立副本上应用已登记迁移，返回核验后的数据库身份。"""
    from camctl.bootstrap.application import ConfigAdapter
    from camctl.persistence.maintenance_integrity import verify_maintenance_integrity
    from camctl.persistence.runtime import (
        CURRENT_FORMAT_VERSION, _FORMAT_VERSIONS, DbConfig, DbOpenMode,
        open_existing, verify_directory_binding,
    )

    if not state_db.is_file():
        raise ValueError('数据库副本不存在')
    config = ConfigAdapter(Path.home()).load(config_path)
    if state_db.samefile(Path(config.paths.state_db)):
        raise ValueError('迁移目标必须是独立数据库副本')
    options = DbConfig(busy_timeout_ms=config.database.busy_timeout_ms)
    owned = open_existing(state_db, DbOpenMode.EXISTING_RO, options)
    try:
        verify_maintenance_integrity(owned.connection)
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
            verify_maintenance_integrity(owned.connection)
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


def inspect_deployment(config_path: Path | None) -> dict:
    from camctl.bootstrap.application import ConfigAdapter
    from camctl.persistence.maintenance_integrity import verify_maintenance_integrity
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing, verify_directory_binding

    config = ConfigAdapter(Path.home()).load(config_path)
    state_db = Path(config.paths.state_db).resolve()
    if not state_db.is_file():
        raise ValueError(f'原状态库不存在或不是普通文件: {state_db}')
    owned = open_existing(state_db, DbOpenMode.EXISTING_RO,
                          DbConfig(busy_timeout_ms=config.database.busy_timeout_ms))
    try:
        verify_maintenance_integrity(owned.connection)
        verify_directory_binding(owned.metadata, config.paths.staging, config.paths.ready,
                                 config.paths.processing)
        return {'state_db': str(state_db), 'metadata': asdict(owned.metadata)}
    finally:
        owned.connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check-config', action='store_true', help='加载配置并输出更新路径')
    mode.add_argument('--prepare-directories', action='store_true', help='准备新实例目录')
    mode.add_argument('--migrate-copy', type=Path, help='在独立状态库副本上执行已登记迁移')
    mode.add_argument('--create-copy', type=Path, help='在独立事务位置准备新的空状态库')
    args = parser.parse_args()
    try:
        result = (prepare_new_directories(args.config) if args.prepare_directories
                  else inspect_configuration(args.config) if args.check_config
                  else migrate_copy(args.config, args.migrate_copy) if args.migrate_copy is not None
                  else create_database_copy(args.config, args.create_copy) if args.create_copy is not None
                  else inspect_deployment(args.config))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except Exception as error:
        from camctl.operations.error_diagnostics import exception_diagnostic
        parser.exit(1, json.dumps(exception_diagnostic(error), ensure_ascii=False, default=repr) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
