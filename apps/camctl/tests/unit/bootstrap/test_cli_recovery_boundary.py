"""正式 run 入口传递部署调用方保证的旧本地收场前提。"""

from io import StringIO
from unittest.mock import create_autospec

import pytest

from camctl import cli
from camctl.bootstrap import application, lifecycle
from camctl.capture.recovery import RecoveryBoundary
from camctl.session.outcome import SessionOutcome


@pytest.mark.parametrize("kind, expected", [
    (cli.CommandKind.RUN, RecoveryBoundary.HOST_LOCAL_SETTLED),
    (cli.CommandKind.SUBMIT, RecoveryBoundary.UNCONFIRMED),
])
def test_cli_uses_deployment_run_boundary_only_for_execution(monkeypatch, kind, expected):
    config = object()
    build = create_autospec(lifecycle.build_runtime)
    execute = create_autospec(lifecycle.execute_command)
    execute.return_value = SessionOutcome(succeeded=True, needs_run=True if kind is cli.CommandKind.SUBMIT else None)
    monkeypatch.setattr(application.ConfigAdapter, "load", lambda *args: config)
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: cls("/unused")))
    monkeypatch.setattr(lifecycle, "build_runtime", build)
    monkeypatch.setattr(lifecycle, "execute_command", execute)
    monkeypatch.setattr(lifecycle, "close_runtime", create_autospec(lifecycle.close_runtime))

    result = cli._execute_session_command(cli.Command(kind, None, None), StringIO(), StringIO(), None)

    assert result == 0
    assert build.call_args.kwargs["recovery_boundary"] is expected
