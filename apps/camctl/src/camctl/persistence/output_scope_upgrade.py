"""显式 init --upgrade-output-scope 的完整副本格式升级。"""

from pathlib import Path

from camctl.persistence.capture_preparation_upgrade import _upgrade_full_copy
from camctl.persistence.runtime import OwnedConnection, _FORMAT_VERSIONS


def upgrade_output_scope(owned: OwnedConnection, state_db: Path) -> Path | None:
    """在初始化的稳定会话锁下，将 V5 原事实保全并升级到当前格式。"""
    return _upgrade_full_copy(
        owned, state_db, source_versions=(int(_FORMAT_VERSIONS.V5),),
        upgrade_name="output-scope", description="输出范围", minimum="V5",
    )
