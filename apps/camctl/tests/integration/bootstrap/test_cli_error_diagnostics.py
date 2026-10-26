"""CLI 初始化前错误保留完整异常图，stdout 保持原命令协议。"""

from io import StringIO
from unittest.mock import create_autospec

import pytest

from camctl import cli
from camctl.bootstrap import application
from camctl.bootstrap.config import ConfigError
from camctl.persistence import initialization


def _chained_error():
    try:
        raise OSError("配置原始读取失败")
    except OSError as cause:
        try:
            raise ConfigError("配置装配失败") from cause
        except ConfigError as error:
            error.add_note("已知配置路径=/configured/settings.json")
            return error


@pytest.mark.parametrize("arguments", [["run"], ["submit", "plan.json"], ["init"], ["describe"]])
def test_early_configuration_failure_has_complete_exception_facts(monkeypatch, arguments):
    error = _chained_error()
    load = create_autospec(application.ConfigAdapter.load, side_effect=error)
    monkeypatch.setattr(application.ConfigAdapter, "load", load)
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: cls("/unused")))
    out, err = StringIO(), StringIO()

    result = cli.main(arguments, out, err)

    assert result == 1
    assert out.getvalue() == ""
    diagnostic = err.getvalue()
    for fact in ("ConfigError", "OSError", "配置原始读取失败", "配置装配失败", "/configured/settings.json", "traceback"):
        assert fact in diagnostic


def test_init_failed_result_preserves_primary_and_secondary_exceptions(monkeypatch):
    error = _chained_error()
    secondary = OSError("初始化锁释放失败")
    result = initialization.InitResult(initialization.InitOutcome.FAILED,
        "数据库初始化失败", error=error, secondary_errors=(secondary,))
    monkeypatch.setattr(application.ConfigAdapter, "load", lambda *args: object())
    monkeypatch.setattr(application.ConfigAdapter, "state_db_path", lambda *args: cli.Path("/state.sqlite"))
    monkeypatch.setattr(initialization, "initialize_state", lambda *args, **kwargs: result)
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: cls("/unused")))
    out, err = StringIO(), StringIO()

    assert cli.main(["init"], out, err) == 1

    assert not out.getvalue()
    for fact in ("配置原始读取失败", "初始化锁释放失败", "OSError", "traceback"):
        assert fact in err.getvalue()


def test_notification_open_failure_has_complete_exception_facts(monkeypatch):
    from camctl.motor import notification
    error = _chained_error()
    monkeypatch.setattr(notification, "open_notification_writer", lambda fd: (_ for _ in ()).throw(error))
    out, err = StringIO(), StringIO()

    assert cli.main(["run", "--host-notification-fd", "4"], out, err) == 1

    assert not out.getvalue()
    assert "配置原始读取失败" in err.getvalue()
    assert "traceback" in err.getvalue()
