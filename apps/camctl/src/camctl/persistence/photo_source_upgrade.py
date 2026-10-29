"""显式 init --upgrade-photo-source 的完整副本格式升级。"""

from pathlib import Path

from camctl.persistence.capture_preparation_upgrade import _upgrade_full_copy
from camctl.persistence.runtime import OwnedConnection, _FORMAT_VERSIONS


def upgrade_photo_source(owned: OwnedConnection, state_db: Path) -> Path | None:
    """在原会话锁下，保全 V6 全部事实并升级到当前格式。"""
    return _upgrade_full_copy(
        owned, state_db, source_versions=(int(_FORMAT_VERSIONS.V6),),
        upgrade_name="photo-source", description="照片来源", minimum="V6")
