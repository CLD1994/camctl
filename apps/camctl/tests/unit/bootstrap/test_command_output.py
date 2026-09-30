"""B3 命令解析与机器结果编码的单元测试。

期望独立来自 CLI 契约：单行 JSON 结果通道、退出码 1 的语法错误
适配、submit 成功必须携带 needs_run、describe 序列化前整体校验。
"""

from __future__ import annotations

import json

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


class TestDescribeEncoding:
    def test_empty_devices_document_validated_and_encoded(self) -> None:
        output = encode_describe_document({"devices": []})
        assert output.endswith(b"\n")
        assert output.count(b"\n") == 1
        assert json.loads(output) == {"devices": []}

    def test_invalid_document_rejected_before_encoding(self) -> None:
        with pytest.raises(Exception, match="capabilities"):
            encode_describe_document({"devices": [], "unexpected": 1})
        with pytest.raises(Exception):
            encode_describe_document({})

    def test_schema_failure_raises_validation_error_only(self) -> None:
        from camctl.contracts.schemas import SchemaValidationError

        with pytest.raises(SchemaValidationError):
            encode_describe_document({"devices": [{"device_id": ""}]})


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

    def test_run_not_assembled_exits_one_with_stderr(self, capsys) -> None:
        from camctl.cli import main

        assert main(["run"]) == 1
        assert capsys.readouterr().out == ""
