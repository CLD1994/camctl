"""按需照片来源格式只通过独立的显式升级入口接入旧库。"""

from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl import cli
from camctl.persistence import initialization, photo_source_upgrade
from camctl.persistence.runtime import DatabaseUnsupportedFormatError, require_deferred_photo_format


def test_cli_parses_explicit_photo_source_upgrade():
    command = cli.parse_command(["init", "--upgrade-photo-source"])
    assert command.upgrade_photo_source is True
    assert cli.parse_command(["init"]).upgrade_photo_source is False


@pytest.mark.parametrize("previous", ["software-recovery", "capture-preparation", "output-scope"])
def test_cli_rejects_combining_photo_source_with_another_upgrade(previous):
    with pytest.raises(cli.CommandLineError):
        cli.parse_command(["init", f"--upgrade-{previous}", "--upgrade-photo-source"])


def test_programmatic_init_rejects_combined_upgrades_before_access(monkeypatch):
    check = create_autospec(initialization.ensure_runtime_library)
    monkeypatch.setattr(initialization, "ensure_runtime_library", check)
    result = initialization.initialize_state(object(), Path("/state/state.db"),
        upgrade_output_scope=True, upgrade_photo_source=True)
    assert result.outcome is initialization.InitOutcome.FAILED
    check.assert_not_called()


def test_photo_source_upgrade_uses_the_existing_verified_copy_path(monkeypatch):
    upgrade = create_autospec(photo_source_upgrade._upgrade_full_copy, return_value=Path("/backup"))
    monkeypatch.setattr(photo_source_upgrade, "_upgrade_full_copy", upgrade)
    owned, state_db = object(), Path("/state/state.db")
    assert photo_source_upgrade.upgrade_photo_source(owned, state_db) == Path("/backup")
    upgrade.assert_called_once_with(owned, state_db, source_versions=(6,),
        upgrade_name="photo-source", description="照片来源", minimum="V6")


@pytest.mark.parametrize("version", [None, True, 1, 6, 8])
def test_deferred_photo_is_rejected_by_unsupported_state_formats(version):
    with pytest.raises(DatabaseUnsupportedFormatError, match="upgrade-photo-source"):
        require_deferred_photo_format(version)


def test_deferred_photo_accepts_the_new_state_format():
    require_deferred_photo_format(7)
