"""显式升级预览选择格式，保留状态库全部事实及原副本。"""

from pathlib import Path

from camctl.persistence.capture_preparation_upgrade import _upgrade_full_copy
from camctl.persistence.runtime import OwnedConnection, _FORMAT_VERSIONS


def upgrade_preview_selection(owned: OwnedConnection, state_db: Path) -> Path | None:
    return _upgrade_full_copy(
        owned, state_db, source_versions=(int(_FORMAT_VERSIONS.V7),),
        upgrade_name="preview-selection", description="预览选择", minimum="V7")
