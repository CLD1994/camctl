"""一次完整读取及解析。

每次输入仅一次完整读取尝试；读取或解析失败时不从片段取得请求
身份或 ACK。诊断中的不可编码输入以转义文本表示。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from camctl.contracts.json_values import JsonParseError, JsonValue, parse_exact_json

__all__ = [
    "InputDiagnostic",
    "InputFileReader",
    "FileInputReader",
    "InputReadFailure",
    "InputRead",
    "InputStage",
    "ParsedInput",
    "parse_input",
    "read_input",
]


class InputStage(Enum):
    """一次输入尝试失败的阶段。"""

    OPEN = "open"
    READ = "read"
    DECODE = "decode"
    PARSE = "parse"


class InputReadFailure(OSError):
    """适配器知道实际失败步骤；不携带部分读取内容。"""

    def __init__(self, stage: InputStage, cause: OSError):
        if stage not in {InputStage.OPEN, InputStage.READ}:
            raise ValueError("文件读取失败步骤必须为 OPEN 或 READ")
        super().__init__(str(cause))
        self.stage = stage
        self.cause = cause


class InputFileReader(Protocol):
    """一次调用返回完整字节；文件失败抛带真实步骤的 InputReadFailure。"""

    def read(self, path: str) -> bytes: ...


@dataclass(frozen=True)
class InputRead:
    """一次完整读取尝试的结果：原始字节或带阶段的失败。"""

    path: str
    payload: bytes | None
    stage: InputStage | None
    detail: str | None

    @property
    def failed(self) -> bool:
        return self.payload is None


@dataclass(frozen=True)
class ParsedInput:
    """完整解析成功后的输入；字段提取由后续规则承接。"""

    path: str
    document: JsonValue


@dataclass(frozen=True)
class InputDiagnostic:
    """读取或解析失败的诊断；不携带请求身份或 ACK。"""

    path: str
    stage: InputStage
    detail: str
    request_id: None = None


async def read_input(path: str, reader: InputFileReader) -> InputRead:
    """组织一次打开及完整读取；失败保留阶段与实际错误。"""
    try:
        payload = reader.read(path)
    except InputReadFailure as error:
        return InputRead(
            path=path, payload=None, stage=error.stage, detail=str(error)
        )
    return InputRead(path=path, payload=payload, stage=None, detail=None)


def parse_input(read: InputRead) -> ParsedInput | InputDiagnostic:
    """把完整读取结果解析为输入或诊断。

    编码检查先于 JSON 解析；重复键、非法 Unicode 转义及非 JSON
    数值常量都按解析失败处理，不返回部分文档。
    """
    if read.payload is None:
        assert read.stage is not None and read.detail is not None
        return InputDiagnostic(path=read.path, stage=read.stage, detail=read.detail)
    try:
        text = read.payload.decode("utf-8")
    except UnicodeDecodeError as error:
        return InputDiagnostic(
            path=read.path,
            stage=InputStage.DECODE,
            detail=f"输入不是有效的 UTF-8 文本: {error}",
        )
    try:
        document = parse_exact_json(text)
    except JsonParseError as error:
        return InputDiagnostic(
            path=read.path,
            stage=InputStage.PARSE,
            detail=str(error),
        )
    return ParsedInput(path=read.path, document=document)


class FileInputReader:
    """真实文件读取适配器：打开和完整读取各自报告实际失败步骤。"""

    def read(self, path: str) -> bytes:
        try:
            handle = open(path, "rb")
        except OSError as error:
            raise InputReadFailure(InputStage.OPEN, error) from error
        try:
            with handle:
                return handle.read()
        except OSError as error:
            raise InputReadFailure(InputStage.READ, error) from error
