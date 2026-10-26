"""输出范围升级使用显式初始化选项和原目录绑定。"""

from io import StringIO
from itertools import combinations
from pathlib import Path
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl import cli
from camctl.bootstrap import application
from camctl.persistence import initialization, output_scope_upgrade
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection, StateDatabaseError


def test_init_parses_output_scope_as_an_explicit_option():
    assert cli.parse_command(["init", "--upgrade-output-scope"]) == cli.Command(
        cli.CommandKind.INIT, None, None, upgrade_output_scope=True,
    )
    assert not cli.parse_command(["init"]).upgrade_output_scope


@pytest.mark.parametrize("previous", ["software-recovery", "capture-preparation"])
def test_output_scope_option_is_exclusive_with_other_upgrades(previous):
    with pytest.raises(cli.CommandLineError):
        cli.parse_command(["init", f"--upgrade-{previous}", "--upgrade-output-scope"])


@pytest.mark.parametrize("selected", list(combinations((
    "upgrade_software_recovery", "upgrade_capture_preparation", "upgrade_output_scope",
), 2)))
def test_programmatic_init_rejects_combined_upgrades_before_access(monkeypatch, selected):
    runtime = create_autospec(initialization.ensure_runtime_library)
    monkeypatch.setattr(initialization, "ensure_runtime_library", runtime)
    result = initialization.initialize_state(object(), Path("/state/state.db"),
        **dict.fromkeys(selected, True))
    assert result.outcome is initialization.InitOutcome.FAILED
    runtime.assert_not_called()


def test_init_cli_forwards_output_scope_option_and_keeps_stdout_empty(monkeypatch):
    config, state_db = object(), Path("/state/state.db")
    adapter = create_autospec(application.ConfigAdapter, instance=True, spec_set=True)
    adapter.load.return_value = config
    adapter.state_db_path.return_value = state_db
    monkeypatch.setattr(application, "ConfigAdapter", create_autospec(
        application.ConfigAdapter, return_value=adapter,
    ))
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: cls("/unused")))
    initialize = create_autospec(initialization.initialize_state,
        return_value=initialization.InitResult(initialization.InitOutcome.VERIFIED_EXISTING, "验证通过"))
    monkeypatch.setattr(initialization, "initialize_state", initialize)
    out, err = StringIO(), StringIO()
    assert cli._run_init(cli.parse_command(["init", "--upgrade-output-scope"]), out, err) == 0
    initialize.assert_called_once_with(config, state_db, upgrade_software_recovery=False,
        upgrade_capture_preparation=False, upgrade_output_scope=True)
    assert out.getvalue() == "" and err.getvalue() == ""


@pytest.mark.parametrize("selected, binding_matches, failure", [
    (False, True, False), (True, True, False), (True, False, False), (True, True, True),
])
def test_existing_init_preserves_binding_and_reports_actual_upgrade_failure(
    monkeypatch, selected, binding_matches, failure,
):
    roots = (Path("/staging"), Path("/ready"), Path("/processing"))
    metadata = DatabaseMetadata("camctl", "a" * 32, 5, *map(str, roots))
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True), metadata)
    monkeypatch.setattr(initialization, "open_existing", create_autospec(
        initialization.open_existing, return_value=owned,
    ))
    monkeypatch.setattr(initialization, "_prepare_directories", create_autospec(initialization._prepare_directories))
    if not binding_matches:
        monkeypatch.setattr(initialization, "verify_directory_binding", create_autospec(
            initialization.verify_directory_binding, side_effect=StateDatabaseError("绑定不同"),
        ))
    upgrade = create_autospec(output_scope_upgrade.upgrade_output_scope,
        side_effect=StateDatabaseError("备份核验失败") if failure else None)
    monkeypatch.setattr(output_scope_upgrade, "upgrade_output_scope", upgrade)
    switch = create_autospec(initialization._attempt_directory_switch)
    monkeypatch.setattr(initialization, "_attempt_directory_switch", switch)
    result = initialization._verify_existing(Path("/state/state.db"), roots, tuple(map(str, roots)),
        upgrade_output_scope=selected)
    expected = initialization.InitOutcome.FAILED if failure or not binding_matches else initialization.InitOutcome.VERIFIED_EXISTING
    assert result.outcome is expected
    assert upgrade.call_count == int(selected and binding_matches)
    if failure:
        assert result.error is upgrade.side_effect
    switch.assert_not_called()
    owned.connection.close.assert_called_once()


def test_initial_publish_race_preserves_the_output_scope_upgrade_request(monkeypatch):
    roots = (Path("/staging"), Path("/ready"), Path("/processing"))
    binding, state_db = tuple(map(str, roots)), Path("/state/state.db")
    monkeypatch.setattr(Path, "unlink", create_autospec(Path.unlink))
    monkeypatch.setattr(initialization, "_build_initial_database", create_autospec(initialization._build_initial_database))
    monkeypatch.setattr(initialization.os, "link", create_autospec(initialization.os.link, side_effect=FileExistsError))
    verify = create_autospec(initialization._verify_existing,
        return_value=initialization.InitResult(initialization.InitOutcome.VERIFIED_EXISTING, "验证已有库"))
    monkeypatch.setattr(initialization, "_verify_existing", verify)
    result = initialization._create_new(state_db, roots, binding, upgrade_output_scope=True)
    assert result.outcome is initialization.InitOutcome.VERIFIED_EXISTING
    verify.assert_called_once_with(state_db, roots, binding, upgrade_software_recovery=False,
        upgrade_capture_preparation=False, upgrade_output_scope=True)
