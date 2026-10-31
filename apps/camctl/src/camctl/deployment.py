"""供部署更新使用的只读配置与既有状态库检查入口。"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path


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
    args = parser.parse_args()
    try:
        print(json.dumps(inspect_deployment(args.config), ensure_ascii=False, sort_keys=True))
    except Exception as error:
        parser.exit(1, f'部署只读检查失败: {type(error).__name__}: {error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
