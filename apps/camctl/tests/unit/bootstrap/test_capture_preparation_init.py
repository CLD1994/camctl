"""拍摄准备升级通过显式 init 接入，普通初始化保持验证语义。"""

from io import StringIO
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl import cli
from camctl.bootstrap import application
from camctl.persistence import capture_preparation_upgrade, initialization, software_recovery_upgrade
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection, StateDatabaseError
from camctl.session.locks import SessionLease


def test_init_parses_capture_preparation_upgrade_as_explicit_option():
    assert cli.parse_command(["init", "--upgrade-capture-preparation"]) == cli.Command(
        cli.CommandKind.INIT, None, None, upgrade_capture_preparation=True,
    )
    assert not cli.parse_command(["init"]).upgrade_capture_preparation


def test_two_upgrade_options_are_mutually_exclusive():
    with pytest.raises(cli.CommandLineError):
        cli.parse_command(["init", "--upgrade-software-recovery", "--upgrade-capture-preparation"])


def test_init_cli_forwards_explicit_option_and_keeps_output_contract(monkeypatch):
    config, state_db = object(), Path("/state/state.db")
    adapter = create_autospec(application.ConfigAdapter, instance=True, spec_set=True)
    adapter.load.return_value = config
    adapter.state_db_path.return_value = state_db
    monkeypatch.setattr(application, "ConfigAdapter", create_autospec(application.ConfigAdapter, return_value=adapter))
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: cls("/unused")))
    initialize = create_autospec(initialization.initialize_state, return_value=initialization.InitResult(
        initialization.InitOutcome.VERIFIED_EXISTING, "验证通过",
    ))
    monkeypatch.setattr(initialization, "initialize_state", initialize)
    out, err = StringIO(), StringIO()
    assert cli._run_init(cli.parse_command(["init", "--upgrade-capture-preparation"]), out, err) == 0
    initialize.assert_called_once_with(config, state_db,
        upgrade_software_recovery=False, upgrade_capture_preparation=True)
    assert out.getvalue() == "" and err.getvalue() == ""


def test_programmatic_init_rejects_combined_upgrades_before_access(monkeypatch):
    runtime = create_autospec(initialization.ensure_runtime_library)
    monkeypatch.setattr(initialization, "ensure_runtime_library", runtime)
    result = initialization.initialize_state(object(), Path("/state/state.db"),
        upgrade_software_recovery=True, upgrade_capture_preparation=True)
    assert result.outcome is initialization.InitOutcome.FAILED
    runtime.assert_not_called()


def test_initialize_keeps_upgrade_inside_owned_lease_and_forwards_it(monkeypatch):
    config = SimpleNamespace(paths=SimpleNamespace(staging="/staging", ready="/ready", processing="/processing"))
    lease = create_autospec(SessionLease, instance=True, spec_set=True)
    monkeypatch.setattr(initialization, "ensure_runtime_library", create_autospec(initialization.ensure_runtime_library))
    monkeypatch.setattr(initialization, "check_distinct_roots", create_autospec(initialization.check_distinct_roots, return_value=None))
    monkeypatch.setattr(initialization, "check_atomic_move_support", create_autospec(initialization.check_atomic_move_support, return_value=None))
    monkeypatch.setattr(Path, "resolve", create_autospec(Path.resolve, side_effect=lambda path: path))
    monkeypatch.setattr(Path, "mkdir", create_autospec(Path.mkdir))
    monkeypatch.setattr(initialization, "lock_file_paths", create_autospec(initialization.lock_file_paths, return_value=(Path("/lock"), Path("/admission"))))
    monkeypatch.setattr(initialization, "acquire_session", create_autospec(initialization.acquire_session, return_value=lease))

    def under_lease(*args, **kwargs):
        lease.close.assert_not_called()
        assert kwargs == {"upgrade_software_recovery": False, "upgrade_capture_preparation": True}
        return initialization.InitResult(initialization.InitOutcome.VERIFIED_EXISTING, "验证通过")

    monkeypatch.setattr(initialization, "_initialize_under_lease",
        create_autospec(initialization._initialize_under_lease, side_effect=under_lease))
    result = initialization.initialize_state(config, Path("/state/state.db"), upgrade_capture_preparation=True)
    assert result.outcome is initialization.InitOutcome.VERIFIED_EXISTING
    lease.close.assert_called_once()


@pytest.fixture
def existing(monkeypatch):
    metadata = DatabaseMetadata("camctl", "a" * 32, 3, "/staging", "/ready", "/processing")
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True), metadata)
    roots = (Path("/staging"), Path("/ready"), Path("/processing"))
    monkeypatch.setattr(initialization, "open_existing", create_autospec(initialization.open_existing, return_value=owned))
    monkeypatch.setattr(initialization, "_prepare_directories", create_autospec(initialization._prepare_directories))
    capture = create_autospec(capture_preparation_upgrade.upgrade_capture_preparation)
    recovery = create_autospec(software_recovery_upgrade.upgrade_software_recovery)
    monkeypatch.setattr(capture_preparation_upgrade, "upgrade_capture_preparation", capture)
    monkeypatch.setattr(software_recovery_upgrade, "upgrade_software_recovery", recovery)
    return owned, roots, capture, recovery


@pytest.mark.parametrize("selected", [None, "capture", "recovery"])
def test_existing_init_uses_only_the_selected_upgrade(existing, selected):
    owned, roots, capture, recovery = existing
    result = initialization._verify_existing(Path("/state/state.db"), roots, tuple(map(str, roots)),
        upgrade_capture_preparation=selected == "capture", upgrade_software_recovery=selected == "recovery")
    assert result.outcome is initialization.InitOutcome.VERIFIED_EXISTING
    assert capture.call_count == (1 if selected == "capture" else 0)
    assert recovery.call_count == (1 if selected == "recovery" else 0)
    owned.connection.close.assert_called_once()


def test_directory_switch_cannot_be_combined_with_capture_upgrade(existing, monkeypatch):
    owned, roots, capture, _ = existing
    monkeypatch.setattr(initialization, "verify_directory_binding", create_autospec(
        initialization.verify_directory_binding, side_effect=StateDatabaseError("绑定不同")))
    switch = create_autospec(initialization._attempt_directory_switch)
    monkeypatch.setattr(initialization, "_attempt_directory_switch", switch)
    result = initialization._verify_existing(Path("/state/state.db"), roots, tuple(map(str, roots)),
        upgrade_capture_preparation=True)
    assert result.outcome is initialization.InitOutcome.FAILED
    capture.assert_not_called()
    switch.assert_not_called()
    owned.connection.close.assert_called_once()


def test_failed_upgrade_returns_initialization_failure_and_closes_owned_connection(existing):
    owned, roots, capture, _ = existing
    capture.side_effect = StateDatabaseError("备份核验失败")
    result = initialization._verify_existing(Path("/state/state.db"), roots, tuple(map(str, roots)),
        upgrade_capture_preparation=True)
    assert result.outcome is initialization.InitOutcome.FAILED
    assert result.error is capture.side_effect
    owned.connection.close.assert_called_once()


def test_missing_database_creates_current_schema_without_running_upgrade(monkeypatch):
    roots = (Path("/staging"), Path("/ready"), Path("/processing"))
    monkeypatch.setattr(Path, "exists", create_autospec(Path.exists, return_value=False))
    monkeypatch.setattr(Path, "is_symlink", create_autospec(Path.is_symlink, return_value=False))
    monkeypatch.setattr(Path, "unlink", create_autospec(Path.unlink))
    monkeypatch.setattr(initialization, "_prepare_directories", create_autospec(initialization._prepare_directories))
    build = create_autospec(initialization._build_initial_database)
    monkeypatch.setattr(initialization, "_build_initial_database", build)
    monkeypatch.setattr(initialization.os, "link", create_autospec(initialization.os.link))
    monkeypatch.setattr(initialization, "_fsync_directory", create_autospec(initialization._fsync_directory))
    upgrade = create_autospec(capture_preparation_upgrade.upgrade_capture_preparation)
    monkeypatch.setattr(capture_preparation_upgrade, "upgrade_capture_preparation", upgrade)
    result = initialization._initialize_under_lease(Path("/state/state.db"), roots,
        upgrade_capture_preparation=True)
    assert result.outcome is initialization.InitOutcome.CREATED
    build.assert_called_once()
    upgrade.assert_not_called()


def test_initial_publish_race_preserves_the_capture_upgrade_request(monkeypatch):
    roots = (Path("/staging"), Path("/ready"), Path("/processing"))
    binding = tuple(map(str, roots))
    state_db = Path("/state/state.db")
    monkeypatch.setattr(Path, "unlink", create_autospec(Path.unlink))
    monkeypatch.setattr(initialization, "_build_initial_database", create_autospec(initialization._build_initial_database))
    monkeypatch.setattr(initialization.os, "link", create_autospec(initialization.os.link, side_effect=FileExistsError))
    verify = create_autospec(initialization._verify_existing, return_value=initialization.InitResult(
        initialization.InitOutcome.VERIFIED_EXISTING, "验证已有库"))
    monkeypatch.setattr(initialization, "_verify_existing", verify)
    result = initialization._create_new(state_db, roots, binding, upgrade_capture_preparation=True)
    assert result.outcome is initialization.InitOutcome.VERIFIED_EXISTING
    verify.assert_called_once_with(state_db, roots, binding,
        upgrade_software_recovery=False, upgrade_capture_preparation=True)
