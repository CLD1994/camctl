"""A1 完整输入读取的组件集成测试：真实文件与读取端口组合。

不存在文件、编码错误与解析失败分别产生带阶段诊断；诊断经受理
仓储保存（事务组合见 test_acceptance），不注册计划。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from camctl.acceptance.input import (
    FileInputReader,
    InputDiagnostic,
    InputStage,
    ParsedInput,
    parse_input,
    read_input,
)

pytestmark = pytest.mark.asyncio


RealFileReader = FileInputReader

class TestRealFileInput:
    async def test_opened_file_read_failure_discards_partial_content(self, tmp_path, monkeypatch):
        import builtins
        real_open = builtins.open
        target = tmp_path / "interrupted.json"
        target.write_bytes(b'{"request_id":"42","last_report_id":"20"}')
        opened = []

        class InterruptedFile:
            def __init__(self, handle):
                self.handle = handle
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.handle.close()
            def read(self):
                assert self.handle.read(20)
                raise OSError("文件已经打开，读取尚未完成")

        def open_with_read_fault(path, mode):
            handle = real_open(path, mode)
            opened.append(handle)
            return InterruptedFile(handle)

        monkeypatch.setattr(builtins, "open", open_with_read_fault)
        read = await read_input(str(target), FileInputReader())
        diagnostic = parse_input(read)
        assert read.payload is None
        assert diagnostic.stage is InputStage.READ and diagnostic.request_id is None
        assert diagnostic.path == str(target)
        assert len(opened) == 1 and opened[0].closed

    async def test_missing_file_is_open_diagnostic(self, tmp_path: Path) -> None:
        read = await read_input(str(tmp_path / "nope.json"), RealFileReader())
        diagnostic = parse_input(read)
        assert isinstance(diagnostic, InputDiagnostic)
        assert diagnostic.stage is InputStage.OPEN

    async def test_invalid_utf8_file_is_decode_diagnostic(self, tmp_path: Path) -> None:
        target = tmp_path / "bad.json"
        target.write_bytes(b'{"a": "\xff"}')
        read = await read_input(str(target), RealFileReader())
        diagnostic = parse_input(read)
        assert isinstance(diagnostic, InputDiagnostic)
        assert diagnostic.stage is InputStage.DECODE

    async def test_truncated_json_is_parse_diagnostic(self, tmp_path: Path) -> None:
        target = tmp_path / "cut.json"
        target.write_bytes(b'{"request_id": "42", "actions": [')
        read = await read_input(str(target), RealFileReader())
        diagnostic = parse_input(read)
        assert isinstance(diagnostic, InputDiagnostic)
        assert diagnostic.stage is InputStage.PARSE
        assert diagnostic.request_id is None

    async def test_valid_file_parses_with_precise_numbers(self, tmp_path: Path) -> None:
        from decimal import Decimal

        target = tmp_path / "ok.json"
        target.write_bytes(b'{"request_id": "42", "n": 1.10}')
        read = await read_input(str(target), RealFileReader())
        parsed = parse_input(read)
        assert isinstance(parsed, ParsedInput)
        assert parsed.document["n"] == Decimal("1.10")


@pytest.mark.parametrize("stage,code,public_stage", [("open","plan_file_read_failed","input_read"),("read","plan_file_read_failed","input_read"),("decode","invalid_encoding","input_parse"),("parse","invalid_json","input_parse")])
async def test_input_diagnostic_matches_public_schema(environment, stage, code, public_stage):
    from camctl.acceptance.service import accept_input, AckDisposition
    from camctl.contracts.values import new_operation_key
    from camctl.contracts.public_projection import ProjectionInput, project_public
    from camctl.contracts.schemas import create_validator, validation_errors
    from .test_acceptance import _owned
    connection, context = environment
    source = InputDiagnostic("/plans/broken.json", InputStage(stage), "完整输入不可用")
    result = await accept_input(source, context, new_operation_key(), _owned(environment))
    assert result.ack_disposition is AckDisposition.NOT_PROCESSED
    cursor = connection.execute("SELECT * FROM plan_file_diagnostics")
    row = dict(zip([d[0] for d in cursor.description], cursor.fetchone()))
    fragment = project_public(ProjectionInput(entity="diagnostic", root_id=result.diagnostic_id, tables={"plan_file_diagnostics":{result.diagnostic_id:row}}))
    assert fragment["errors"][0]["stage"] == public_stage
    assert fragment["errors"][0]["code"] == code
    assert "request_id" not in fragment
    schema = {"$schema":"https://json-schema.org/draft/2020-12/schema", "$ref":"status-report.schema.json#/$defs/diagnostic"}
    assert not validation_errors(create_validator(schema), fragment)
    if stage in {"open","read"}:
        assert fragment["errors"][0]["details"]["operation"] == stage


from .test_acceptance import environment
