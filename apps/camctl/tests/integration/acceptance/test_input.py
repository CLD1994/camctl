"""A1 完整输入读取的组件集成测试：真实文件与读取端口组合。

不存在文件、编码错误与解析失败分别产生带阶段诊断；诊断经受理
仓储保存（事务组合见 test_acceptance），不注册计划。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from camctl.acceptance.input import (
    InputDiagnostic,
    InputStage,
    ParsedInput,
    parse_input,
    read_input,
)

pytestmark = pytest.mark.asyncio


class RealFileReader:
    def read(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()


class TestRealFileInput:
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
