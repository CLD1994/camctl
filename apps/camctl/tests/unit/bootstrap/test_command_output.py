"""B3 命令解析与机器结果编码的单元测试。

期望独立来自 CLI 契约：单行 JSON 结果通道、退出码 1 的语法错误
适配、submit 成功必须携带 needs_run、describe 序列化前整体校验。
"""

from __future__ import annotations

import json
from io import StringIO

import pytest

from camctl.cli import (
    Command,
    CommandKind,
    CommandLineError,
    ResultChannelError,
    encode_describe_document,
    encode_session_result,
    parse_command,
)
from camctl.session.outcome import SessionOutcome


class TestParseCommand:
    def test_all_command_forms(self) -> None:
        assert parse_command(["init"]) == Command(CommandKind.INIT, None, None)
        assert parse_command(["init", "--config", "/x/c.toml"]) == Command(
            CommandKind.INIT, None, "/x/c.toml"
        )
        assert parse_command(["run"]) == Command(CommandKind.RUN, None, None)
        assert parse_command(["run", "/plans/p.json"]) == Command(
            CommandKind.RUN, "/plans/p.json", None
        )
        assert parse_command(["submit", "/plans/p.json", "--config", "c.toml"]) == Command(
            CommandKind.SUBMIT, "/plans/p.json", "c.toml"
        )
        assert parse_command(["describe"]) == Command(CommandKind.DESCRIBE, None, None)
        assert parse_command(["--version"]) == Command(CommandKind.VERSION, None, None)

    def test_submit_without_plan_rejected(self) -> None:
        with pytest.raises(CommandLineError):
            parse_command(["submit"])

    def test_unknown_command_rejected(self) -> None:
        with pytest.raises(CommandLineError):
            parse_command(["explode"])

    def test_init_with_plan_rejected(self) -> None:
        # init 不接受计划输入文件（argparse 层即拒绝多余位置参数）。
        with pytest.raises(CommandLineError):
            parse_command(["init", "/plans/p.json"])

    def test_missing_command_rejected(self) -> None:
        with pytest.raises(CommandLineError):
            parse_command([])


class TestSessionResultChannel:
    def test_result_channel_is_single_json(self) -> None:
        output = encode_session_result(SessionOutcome(succeeded=True))
        assert output.endswith(b"\n")
        assert output.count(b"\n") == 1
        assert json.loads(output.decode("utf-8")) == {"kind": "succeeded"}

    def test_succeeded_with_needs_run(self) -> None:
        output = encode_session_result(
            SessionOutcome(succeeded=True, needs_run=True), requires_needs_run=True
        )
        assert json.loads(output) == {"kind": "succeeded", "body": {"needs_run": True}}

    def test_submit_success_without_needs_run_rejected(self) -> None:
        with pytest.raises(ResultChannelError, match="needs_run"):
            encode_session_result(SessionOutcome(succeeded=True), requires_needs_run=True)
        with pytest.raises(ResultChannelError, match="needs_run"):
            encode_session_result(
                SessionOutcome(succeeded=True, needs_run=None), requires_needs_run=True
            )

    def test_error_result_carries_reason_and_details(self) -> None:
        output = encode_session_result(
            SessionOutcome(succeeded=False, reason="clock_invalid", details={"phase": "启动"})
        )
        assert json.loads(output) == {
            "kind": "error",
            "body": {"reason": "clock_invalid", "details": {"phase": "启动"}},
        }

    def test_special_characters_are_json_escaped(self) -> None:
        output = encode_session_result(
            SessionOutcome(succeeded=False, reason="x", details={"text": '带"引号"\n与\\斜杠'})
        )
        assert output.count(b"\n") == 1
        assert json.loads(output)["body"]["details"]["text"] == '带"引号"\n与\\斜杠'

    def test_run_success_with_body_rejected(self) -> None:
        with pytest.raises(ResultChannelError):
            encode_session_result(SessionOutcome(succeeded=True, needs_run=True))

    def test_error_without_reason_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            SessionOutcome(succeeded=False)


class TestMainSyntaxExitCode:
    def test_syntax_error_exits_one_not_two(self, capsys) -> None:
        from camctl.cli import main

        assert main(["explode"]) == 1
        captured = capsys.readouterr()
        assert captured.err != ""
        assert captured.out == ""

    def test_version_outputs_and_exits_zero(self, capsys) -> None:
        from camctl.cli import main

        assert main(["--version"]) == 0
        assert capsys.readouterr().out.endswith("\n")

    def test_run_not_assembled_exits_one_with_stderr(self, capsys, monkeypatch) -> None:
        from camctl.cli import main
        from camctl.bootstrap import application
        from camctl import cli
        import sys
        from types import ModuleType

        monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls:cls("/unused")))
        monkeypatch.setattr(application.ConfigAdapter, "load", lambda *args:object())
        def unavailable(*args):
            raise FileNotFoundError("状态库不存在")
        lifecycle = ModuleType("camctl.bootstrap.lifecycle")
        lifecycle.build_runtime = unavailable
        lifecycle.close_runtime = lambda deps:None
        lifecycle.execute_command = None
        monkeypatch.setitem(sys.modules, "camctl.bootstrap.lifecycle", lifecycle)

        assert main(["run"]) == 1
        assert capsys.readouterr().out == ""


@pytest.mark.parametrize("error_kind", ["rule", "document"])
def test_describe_schema_error_returns_diagnostic(monkeypatch, error_kind) -> None:
    from camctl import cli
    from camctl.bootstrap import application
    from camctl.contracts.schemas import SchemaRuleError, SchemaValidationError
    from camctl.devices import catalog

    failure = (SchemaRuleError if error_kind == "rule" else SchemaValidationError)("schema-case-17")
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: cls("/unused")))
    monkeypatch.setattr(application.ConfigAdapter, "load", lambda *args: object())
    monkeypatch.setattr(catalog, "build_catalog", lambda *args: object())
    monkeypatch.setattr(catalog, "default_driver_definitions", lambda: {})
    monkeypatch.setattr(application, "describe", lambda *args: {"devices": []})

    def validate(*args):
        raise failure

    monkeypatch.setattr(cli, "validate_document", validate)
    out, err = StringIO(), StringIO()
    assert cli.main(["describe"], stdout=out, stderr=err) == 1
    assert out.getvalue() == ""
    assert "schema-case-17" in err.getvalue()


def test_describe_catalog_rule_error_returns_diagnostic(monkeypatch):
    from camctl import cli
    from camctl.bootstrap import application
    from camctl.devices import catalog
    from camctl.acceptance.schema import RuleError
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls:cls("/unused")))
    monkeypatch.setattr(application.ConfigAdapter, "load", lambda *args:object())
    def fail(*args):
        raise RuleError("驱动未部署 driver-case-17")
    monkeypatch.setattr(catalog, "build_catalog", fail)
    out, err = StringIO(), StringIO()
    assert cli.main(["describe"], stdout=out, stderr=err) == 1
    assert out.getvalue() == "" and "driver-case-17" in err.getvalue()


def test_describe_encoding_preserves_validation_boundary(monkeypatch):
    from camctl import cli
    seen = []
    monkeypatch.setattr(cli, "validate_document", lambda schema, document:seen.append((schema,document)))
    document = {"devices":[]}
    assert json.loads(cli.encode_describe_document(document)) == document
    assert seen == [("protocol/capabilities.schema.json", document)]


def test_describe_encoding_propagates_validation_failure(monkeypatch):
    from camctl import cli
    from camctl.contracts.schemas import SchemaValidationError
    def reject(*args):
        raise SchemaValidationError("能力说明不可编码")
    monkeypatch.setattr(cli, "validate_document", reject)
    with pytest.raises(SchemaValidationError):
        cli.encode_describe_document({"devices":[]})
