"""一次性 CLI 在真实 stderr 日志通道记录阶段，业务输出保持原约定。"""

from io import StringIO
import json
from pathlib import Path

import pytest

from camctl import cli
from camctl.bootstrap import application
from camctl.logging_runtime.performance import record_performance


def _config(tmp_path, level):
    paths = {"state_db": str(tmp_path / "state" / "state.db"),
             "log_file": str(tmp_path / "ordinary-log" / "camctl.log"),
             **{name: str(tmp_path / name) for name in ("staging", "ready", "processing")}}
    config = tmp_path / "config.toml"
    config.write_text("[paths]\n" + "\n".join(
        f"{name} = {json.dumps(path)}" for name, path in paths.items())
        + f'\n[log]\nlevel = "{level}"\n', encoding="utf-8")
    return config, paths


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING"])
def test_describe_stages_and_timing_obey_level_without_runtime_files(tmp_path, monkeypatch, level):
    config, paths = _config(tmp_path, level)
    original = application.describe

    def describe(config, catalog):
        record_performance("describe.detail", 3.5)
        return original(config, catalog)

    monkeypatch.setattr(application, "describe", describe)
    out, err = StringIO(), StringIO()
    assert cli.main(["describe", "--config", str(config)], stdout=out, stderr=err) == 0
    assert json.loads(out.getvalue()) == {"devices": []}
    records = [json.loads(line) for line in err.getvalue().splitlines()]
    if level == "WARNING":
        assert records == []
    else:
        stages = [record for record in records if record["stage"] == "describe"]
        assert [record["event"] for record in stages] == ["started", "returned"]
        assert all(record["level"] == "INFO" and record["command_name"] == "describe"
                   and "pid" in record and "logged_at_utc" in record for record in stages)
        assert any(record["stage"] == "describe.detail" for record in records) == (level == "DEBUG")
    assert not any(Path(path).exists() for path in paths.values())
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "ordinary-log").exists()


def test_init_info_preserves_empty_stdout_and_reports_actual_outcome(tmp_path):
    config, paths = _config(tmp_path, "INFO")
    for expected in ("created", "verified_existing"):
        out, err = StringIO(), StringIO()
        assert cli.main(["init", "--config", str(config)], stdout=out, stderr=err) == 0
        assert out.getvalue() == ""
        records = [json.loads(line) for line in err.getvalue().splitlines()]
        stages = [record for record in records if record["stage"] == "initialization"]
        assert [record["event"] for record in stages] == ["started", "returned", "result"]
        assert stages[-1]["outcome"] == expected
        assert all(record["level"] == "INFO" for record in stages)
    assert Path(paths["state_db"]).is_file()
    assert not Path(paths["log_file"]).exists()


def test_describe_error_has_one_complete_json_diagnostic(tmp_path, monkeypatch):
    config, _paths = _config(tmp_path, "INFO")
    original = OSError(5, "capability read failed", "/source")

    def fail(config, catalog):
        raise original

    monkeypatch.setattr(application, "describe", fail)
    out, err = StringIO(), StringIO()
    assert cli.main(["describe", "--config", str(config)], stdout=out, stderr=err) == 1
    assert out.getvalue() == ""
    records = [json.loads(line) for line in err.getvalue().splitlines()]
    assert [record["event"] for record in records if record["level"] == "INFO"] == ["started", "raised"]
    errors = [record for record in records if record["level"] == "ERROR"]
    assert len(errors) == 1
    assert errors[0]["exception"]["errno"] == 5
    assert errors[0]["exception"]["filename"] == "/source"


@pytest.mark.parametrize("command", ["describe", "init"])
def test_stderr_failure_preserves_successful_business_result(tmp_path, command):
    config, _paths = _config(tmp_path, "INFO")

    class FailedStderr:
        def write(self, value):
            raise OSError("stderr unavailable")

        def flush(self):
            raise OSError("stderr unavailable")

    out = StringIO()
    assert cli.main([command, "--config", str(config)], stdout=out, stderr=FailedStderr()) == 0
    assert (json.loads(out.getvalue()) == {"devices": []}) if command == "describe" else out.getvalue() == ""
