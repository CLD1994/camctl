"""对已核验的 hb_cli 标准输出进行严格、无副作用的解析。"""

from dataclasses import dataclass
import re
import unicodedata
from pathlib import PurePosixPath


class CliParseError(ValueError):
    """hb_cli 输出不符合已核验的响应格式。"""


@dataclass(frozen=True, slots=True)
class UsbCamera:
    vendor_id: int
    product_id: int
    bus: int
    address: int


@dataclass(frozen=True, slots=True)
class CapturedFile:
    path: str
    original_name: str
    size: int


@dataclass(frozen=True, slots=True)
class CaptureResponse:
    original: CapturedFile
    preview: CapturedFile


_DEVICE_LINE = re.compile(
    r"vid=0x([0-9a-fA-F]{4}) pid=0x([0-9a-fA-F]{4})[ \t]+"
    r"bus=[ \t]*([0-9]+) addr=[ \t]*([0-9]+)"
)
_INFO_LINE = re.compile(
    r"(?P<key>[^:\s][^:\r\n]*?)[ \t]+:[ \t]+(?P<value>\S[^\r\n]*)"
)
_SAVED_LINE = re.compile(r"saved (.+) \(([0-9]+) bytes\)")
_NON_LF_LINE_SEPARATORS = frozenset("\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def _decode(output: bytes, command: str) -> str:
    try:
        return output.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CliParseError(f"{command} 输出不是有效 UTF-8") from exc


def _lines(text: str) -> list[str]:
    # 只接受 LF，并最多去除一个末尾 LF；保留额外空行供调用方拒绝。
    if _NON_LF_LINE_SEPARATORS.intersection(text):
        raise CliParseError("输出包含未经核验的非 LF 行分隔符")
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n")


def _decimal_int(value: str, field: str) -> int:
    try:
        return int(value, 10)
    except ValueError as exc:
        raise CliParseError(f"{field} 不是可处理的十进制整数") from exc


def parse_devices(output: bytes) -> tuple[UsbCamera, ...]:
    """解析 `hb_cli list` 的设备行或已知的空设备提示。"""
    lines = _lines(_decode(output, "list"))
    if lines == ["(no Hasselblad USB devices found)"]:
        return ()
    if not lines or any(not line for line in lines):
        raise CliParseError("list 输出缺少设备行或包含空行")

    devices: list[UsbCamera] = []
    for line in lines:
        match = _DEVICE_LINE.fullmatch(line)
        if match is None:
            raise CliParseError("list 输出包含无法识别的设备行")
        vendor_id, product_id, bus, address = match.groups()
        devices.append(UsbCamera(
            vendor_id=int(vendor_id, 16),
            product_id=int(product_id, 16),
            bus=_decimal_int(bus, "bus"),
            address=_decimal_int(address, "address"),
        ))
    return tuple(devices)


def parse_serial(output: bytes) -> str:
    """从 `hb_cli info` 输出读取唯一且非空的 serial 文本值。"""
    lines = _lines(_decode(output, "info"))
    values: list[str] = []
    for line in lines:
        match = _INFO_LINE.fullmatch(line)
        if match is None:
            raise CliParseError("info 输出包含无法识别的参数行")
        if match.group("key") == "serial":
            value = match.group("value")
            if any(unicodedata.category(character) == "Cc" for character in value):
                raise CliParseError("serial 值包含控制字符")
            values.append(value)
    if len(values) != 1 or not values[0].strip():
        raise CliParseError("info 输出必须包含唯一且非空的 serial 值")
    return values[0]


def _canonical_absolute_directory(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value:
        raise CliParseError("output_directory 必须是有效的绝对 POSIX 目录")
    if not value.startswith("/"):
        raise CliParseError("output_directory 必须是绝对 POSIX 路径")
    raw_parts = value.split("/")[1:]
    if any(part in (".", "..") for part in raw_parts):
        raise CliParseError("output_directory 不得包含 . 或 .. 路径段")
    path = PurePosixPath(value)
    if str(path) != value.rstrip("/") and value != "/":
        raise CliParseError("output_directory 必须使用规范 POSIX 路径")
    return path


def _captured_file(line: str, output_directory: PurePosixPath) -> CapturedFile:
    match = _SAVED_LINE.fullmatch(line)
    if match is None:
        raise CliParseError("capture 输出包含无法识别的文件行")
    raw_path, raw_size = match.groups()
    if "\x00" in raw_path or "\n" in raw_path or "\r" in raw_path:
        raise CliParseError("capture 文件路径包含禁止字符")
    if not raw_path.startswith("/"):
        raise CliParseError("capture 文件路径必须是绝对 POSIX 路径")
    raw_parts = raw_path.split("/")[1:]
    if any(part in ("", ".", "..") for part in raw_parts):
        raise CliParseError("capture 文件路径包含非法路径段")

    path = PurePosixPath(raw_path)
    if path.parent != output_directory or str(path) != raw_path:
        raise CliParseError("capture 文件必须是指定目录中的直接文件")
    size = _decimal_int(raw_size, "size")
    if size <= 0:
        raise CliParseError("capture 文件长度必须为正数")
    return CapturedFile(path=raw_path, original_name=path.name, size=size)


def parse_capture(output: bytes, *, output_directory: str) -> CaptureResponse:
    """解析成功的 RAW 拍摄响应及其一对主机文件信息。"""
    directory = _canonical_absolute_directory(output_directory)
    lines = _lines(_decode(output, "capture"))
    if len(lines) != 3 or lines[0] != "capture: OK":
        raise CliParseError("capture 输出不是完整的成功响应")

    first = _captured_file(lines[1], directory)
    second = _captured_file(lines[2], directory)
    if first == second:
        raise CliParseError("capture 输出重复引用同一文件")

    if first.original_name.endswith(".3FR"):
        original, preview = first, second
    elif second.original_name.endswith(".3FR"):
        original, preview = second, first
    else:
        raise CliParseError("capture 输出缺少 3FR RAW 原图")

    if preview.original_name != f"{original.original_name}.thumbnail.jpg":
        raise CliParseError("capture 预览名称未与 RAW 原图配对")
    return CaptureResponse(original=original, preview=preview)
