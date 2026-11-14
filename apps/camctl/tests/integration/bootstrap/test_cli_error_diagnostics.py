"""CLI 初始化前错误保留完整异常图，stdout 保持原命令协议。"""

from io import StringIO

from camctl import cli
from camctl.bootstrap import application
from camctl.bootstrap.config import ConfigDefaults, ConfigError, load_config
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


def test_init_failed_result_preserves_primary_and_secondary_exceptions(monkeypatch):
    error = _chained_error()
    secondary = OSError("初始化锁释放失败")
    result = initialization.InitResult(initialization.InitOutcome.FAILED,
        "数据库初始化失败", error=error, secondary_errors=(secondary,))
    monkeypatch.setattr(application.ConfigAdapter, "load", lambda *args: load_config({}, ConfigDefaults()))
    monkeypatch.setattr(application.ConfigAdapter, "state_db_path", lambda *args: cli.Path("/state.sqlite"))
    monkeypatch.setattr(initialization, "initialize_state", lambda *args, **kwargs: result)
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: cls("/unused")))
    out, err = StringIO(), StringIO()

    assert cli.main(["init"], out, err) == 1

    assert not out.getvalue()
    for fact in ("配置原始读取失败", "初始化锁释放失败", "OSError", "traceback"):
        assert fact in err.getvalue()
