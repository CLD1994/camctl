"""显式保全 V8 全部事实，增加同机文件批次结构。"""

from pathlib import Path

from camctl.persistence.capture_preparation_upgrade import _upgrade_full_copy
from camctl.persistence.runtime import OwnedConnection, _FORMAT_VERSIONS


def upgrade_device_file_batches(owned: OwnedConnection, state_db: Path) -> Path | None:
    return _upgrade_full_copy(owned, state_db,
        source_versions=(int(_FORMAT_VERSIONS.V8),), upgrade_name="device-file-batches",
        description="同机文件批次", minimum="V8")
