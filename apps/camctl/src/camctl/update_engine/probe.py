"""在原环境的解释器中只读检查配置及其状态库。"""
import importlib.metadata
import json
from pathlib import Path
import sys

from camctl.bootstrap.application import ConfigAdapter
from camctl.session.locks import lock_file_paths

adapter = ConfigAdapter(Path.home())
config_path = Path(sys.argv[1]) if sys.argv[1] else adapter.default_config_path()
config = adapter.load(config_path)
state_db = Path(config.paths.state_db).resolve()
result = {
    'config': str(config_path),
    'state_db': str(state_db),
    'lock_paths': list(map(str, lock_file_paths(state_db))),
    'old_version': importlib.metadata.version('camctl'),
    'paths': {name: str(getattr(config.paths, name)) for name in ('staging', 'ready', 'processing')},
    'busy_timeout_ms': config.database.busy_timeout_ms,
}
if sys.argv[2] == 'inspect':
    from dataclasses import asdict
    from camctl.persistence.maintenance_integrity import verify_maintenance_integrity
    from camctl.persistence.runtime import (CURRENT_FORMAT_VERSION, _FORMAT_VERSIONS, DbConfig,
                                             DbOpenMode, open_existing, verify_directory_binding)
    owned = open_existing(state_db, DbOpenMode.EXISTING_RO,
                          DbConfig(busy_timeout_ms=config.database.busy_timeout_ms))
    try:
        verify_maintenance_integrity(owned.connection)
        verify_directory_binding(owned.metadata, config.paths.staging, config.paths.ready,
                                 config.paths.processing)
        if not int(_FORMAT_VERSIONS.V9) <= owned.metadata.format_version <= CURRENT_FORMAT_VERSION:
            raise ValueError('原状态库格式超出更新维护范围')
        result['deployment'] = {'state_db': str(state_db), 'metadata': asdict(owned.metadata)}
    finally:
        owned.connection.close()
print(json.dumps(result, ensure_ascii=False))
