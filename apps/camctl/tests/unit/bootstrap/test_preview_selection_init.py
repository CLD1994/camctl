"""预览选择的显式升级、旧格式拒绝及可靠副本入口。"""

from pathlib import Path
from unittest.mock import create_autospec
import sqlite3

import pytest

from camctl import cli
from camctl.persistence import initialization, preview_selection_upgrade, preview_selection_structure
from camctl.persistence.runtime import DatabaseUnsupportedFormatError, require_preview_selection_format


def test_cli_parses_explicit_preview_selection_upgrade():
    assert cli.parse_command(["init", "--upgrade-preview-selection"]).upgrade_preview_selection
    assert not cli.parse_command(["init"]).upgrade_preview_selection


@pytest.mark.parametrize("previous", ["software-recovery", "capture-preparation", "output-scope", "photo-source"])
def test_preview_upgrade_cannot_mix_with_another_upgrade(previous):
    with pytest.raises(cli.CommandLineError):
        cli.parse_command(["init", "--upgrade-preview-selection", f"--upgrade-{previous}"])


def test_combined_programmatic_upgrades_are_rejected_before_access(monkeypatch):
    runtime = create_autospec(initialization.ensure_runtime_library)
    monkeypatch.setattr(initialization, "ensure_runtime_library", runtime)
    result = initialization.initialize_state(object(), Path("/state/state.db"),
        upgrade_preview_selection=True, upgrade_photo_source=True)
    assert result.outcome is initialization.InitOutcome.FAILED
    runtime.assert_not_called()


@pytest.mark.parametrize("version", [None, True, 1, 6, 7, 9])
def test_new_preview_selection_rejects_unsupported_format_with_upgrade_instruction(version):
    with pytest.raises(DatabaseUnsupportedFormatError, match="init --upgrade-preview-selection"):
        require_preview_selection_format(version)


def test_new_preview_selection_accepts_v8():
    require_preview_selection_format(8)


def test_upgrade_preserves_all_facts_through_the_existing_full_copy(monkeypatch):
    upgrade = create_autospec(preview_selection_upgrade._upgrade_full_copy, return_value=Path("/backup"))
    monkeypatch.setattr(preview_selection_upgrade, "_upgrade_full_copy", upgrade)
    owned, path = object(), Path("/state/state.db")
    assert preview_selection_upgrade.upgrade_preview_selection(owned, path) == Path("/backup")
    upgrade.assert_called_once_with(owned, path, source_versions=(7,),
        upgrade_name="preview-selection", description="预览选择", minimum="V7")


@pytest.mark.parametrize("actual", [None, "CREATE TABLE obtain_items (error_code INTEGER CHECK(error_code IN (8))) STRICT"])
def test_v8_open_rejects_old_or_missing_preview_selection_table(monkeypatch, actual):
    authority = b"CREATE TABLE obtain_items (error_code INTEGER CHECK(error_code IN (8,9))) STRICT"
    resource = create_autospec(preview_selection_structure.resource_bytes, return_value=authority)
    monkeypatch.setattr(preview_selection_structure, "resource_bytes", resource)
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    connection.execute.return_value.fetchone.return_value = None if actual is None else (actual,)
    with pytest.raises(ValueError):
        preview_selection_structure.verify_preview_selection_structure(connection)


def test_v8_open_accepts_authority_table_despite_formatting(monkeypatch):
    authority = b"CREATE TABLE obtain_items (error_code INTEGER CHECK(error_code IN (8,9))) STRICT"
    monkeypatch.setattr(preview_selection_structure, "resource_bytes",
        create_autospec(preview_selection_structure.resource_bytes, return_value=authority))
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    connection.execute.return_value.fetchone.return_value = (authority.decode().replace("CREATE TABLE", "create   table"),)
    preview_selection_structure.verify_preview_selection_structure(connection)
