"""A1 一次完整读取及解析的单元测试。

期望独立来自输入契约：每次输入仅一次完整读取尝试；失败或 JSON
不完整时不从片段取得请求身份或 ACK。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from camctl.acceptance.input import (
    InputDiagnostic,
    InputFileReader,
    InputStage,
    parse_input,
    read_input,
)

pytestmark = pytest.mark.asyncio


class FakeReader:
    """受读取端口约束的替身：记录打开次数，按脚本返回或失败。"""

    def __init__(self, *, payload: bytes | None = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.open_count = 0

    def read(self, path: str) -> bytes:
        self.open_count += 1
        if self.error is not None:
            raise self.error
        assert self.payload is not None
        return self.payload


async def _diagnostic_for(payload: bytes) -> InputDiagnostic:
    read = await read_input("/plans/p.json", FakeReader(payload=payload))
    parsed = parse_input(read)
    assert isinstance(parsed, InputDiagnostic)
    return parsed


class TestReadInput:
    async def test_partial_read_has_no_identity(self) -> None:
        reader = FakeReader(error=OSError("读取在后半段中断"))
        read = await read_input("/plans/p.json", reader)
        diagnostic = parse_input(read)
        assert isinstance(diagnostic, InputDiagnostic)
        assert diagnostic.request_id is None
        assert diagnostic.stage is InputStage.OPEN
        # 只尝试一次打开，不重读。
        assert reader.open_count == 1

    async def test_success_carries_full_document(self) -> None:
        payload = b'{"request_id": "42"}'
        read = await read_input("/plans/p.json", FakeReader(payload=payload))
        parsed = parse_input(read)
        assert not isinstance(parsed, InputDiagnostic)
        assert parsed.document == {"request_id": "42"}

    async def test_open_error_keeps_path_and_detail(self) -> None:
        read = await read_input(
            "/plans/missing.json", FakeReader(error=FileNotFoundError("no such file"))
        )
        diagnostic = parse_input(read)
        assert diagnostic.path == "/plans/missing.json"
        assert diagnostic.stage is InputStage.OPEN
        assert "no such file" in diagnostic.detail


class TestParseStages:
    async def test_invalid_utf8_is_decode_failure_with_escaped_detail(self) -> None:
        diagnostic = await _diagnostic_for(b'{"a": "\xff\xfe"}')
        assert diagnostic.stage is InputStage.DECODE
        # 不可编码输入以转义表示进入诊断。
        assert "ff" in diagnostic.detail

    async def test_duplicate_keys_are_parse_failure(self) -> None:
        diagnostic = await _diagnostic_for(b'{"request_id": "1", "request_id": "2"}')
        assert diagnostic.stage is InputStage.PARSE
        assert diagnostic.request_id is None

    async def test_non_json_constants_are_parse_failure(self) -> None:
        diagnostic = await _diagnostic_for(b'{"value": NaN}')
        assert diagnostic.stage is InputStage.PARSE

    async def test_lone_surrogate_escape_is_parse_failure(self) -> None:
        diagnostic = await _diagnostic_for('{"name": "\\ud800"}'.encode("utf-8"))
        assert diagnostic.stage is InputStage.PARSE

    async def test_trailing_garbage_is_parse_failure(self) -> None:
        diagnostic = await _diagnostic_for(b'{"request_id": "1"} extra')
        assert diagnostic.stage is InputStage.PARSE

    async def test_precise_numbers_preserved(self) -> None:
        read = await read_input(
            "/plans/p.json", FakeReader(payload=b'{"n": 1.10, "i": 2}')
        )
        parsed = parse_input(read)
        assert not isinstance(parsed, InputDiagnostic)
        assert parsed.document["n"] == Decimal("1.10")
        assert parsed.document["i"] == 2


class TestDiagnosticIdentity:
    async def test_truncated_body_after_identity_still_has_no_identity(self) -> None:
        # 前段含 request_id 和 ACK、后段不完整：不得从片段取得身份。
        diagnostic = await _diagnostic_for(b'{"request_id": "42", "ack": {"report_id":')
        assert diagnostic.request_id is None
        assert diagnostic.stage is InputStage.PARSE
