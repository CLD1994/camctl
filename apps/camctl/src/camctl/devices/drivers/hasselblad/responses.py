"""对 hb_cli 的独立双流进行严格、无副作用的响应解析。"""

from dataclasses import dataclass
from enum import StrEnum
import re
import unicodedata
from pathlib import PurePosixPath

from .files import CameraFileLocator, CameraLocatorStatus
from .parameters import CAPTURE_FORMATS, CaptureFormat, CaptureFormatDescription


class CaptureParseErrorCode(StrEnum):
    INVALID_RESPONSE = "invalid_capture_response"
    UNREPORTED_CAMERA_LOCATOR = "unreported_camera_locator"
    MALFORMED_CAMERA_LOCATOR = "malformed_camera_locator"
    AMBIGUOUS_CAMERA_LOCATOR = "ambiguous_camera_locator"


class CliParseError(ValueError):
    """hb_cli 输出不符合已核验的响应格式。"""

    def __init__(self, message: str, *, code: str = "invalid_response") -> None:
        self.code = str(code)
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class UsbCamera:
    vendor_id: int
    product_id: int
    bus: int
    address: int


@dataclass(frozen=True, slots=True)
class CapturedFile:
    host_path: str
    host_name: str
    size: int
    media_type: str


@dataclass(frozen=True, slots=True)
class CaptureResponse:
    original: CapturedFile
    preview: CapturedFile
    camera_locator: CameraFileLocator


@dataclass(frozen=True, slots=True)
class DeviceCaptureResponse:
    """可靠相机定位与独立解释的同步文件记录。"""

    camera_locator: CameraFileLocator
    original: CapturedFile | None
    preview: CapturedFile | None
    host_records_error: str | None
    original_error: str | None = None
    preview_error: str | None = None


_DEVICE_LINE = re.compile(
    r"vid=0x([0-9a-fA-F]{4}) pid=0x([0-9a-fA-F]{4})[ \t]+"
    r"bus=[ \t]*([0-9]+) addr=[ \t]*([0-9]+)"
)
_INFO_LINE = re.compile(
    r"(?P<key>[^:\s][^:\r\n]*?)[ \t]+:[ \t]+(?P<value>\S[^\r\n]*)"
)
_SAVED_LINE = re.compile(r"saved (.+) \(([0-9]+) bytes\)")
_CAMERA_FILE_LINE = re.compile(r"camera file: (.+)")
_UNREPORTED_CAMERA_FILE_LINE = "capture: camera filename was not reported"
_NON_LF_LINE_SEPARATORS = frozenset("\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
_DOCTOR_CHECK_LINE = re.compile(r"\[(OK|FAIL)\] [^:\s][^:\r\n]*: \S[^\r\n]*")


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


def doctor_passed(output: bytes, stderr: bytes) -> bool:
    """按已观察的结构化检查行及明确结果分类；未知内容不能确认。"""
    lines = _lines(_decode(output, "doctor"))
    _lines(_decode(stderr, "doctor stderr"))
    if stderr:
        raise CliParseError("doctor stderr 包含未知输出")
    results = []
    failed = False
    for line in lines:
        if line in ("doctor: PASS", "doctor: FAIL"):
            results.append(line)
            continue
        check = _DOCTOR_CHECK_LINE.fullmatch(line)
        if check is None or any(unicodedata.category(character) == "Cc" for character in line):
            raise CliParseError("doctor 输出包含无法识别的检查行")
        failed |= check.group(1) == "FAIL"
    if results == ["doctor: PASS"] and lines[-1] != "doctor: PASS":
        raise CliParseError("doctor PASS 之后包含未知输出")
    return not failed and results == ["doctor: PASS"]


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


@dataclass(frozen=True, slots=True)
class _SavedFile:
    host_path: str
    host_name: str
    size: int


def _captured_file(line: str, output_directory: PurePosixPath) -> _SavedFile:
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
    return _SavedFile(host_path=raw_path, host_name=path.name, size=size)


def _camera_locator(
    line: str, description: CaptureFormatDescription
) -> CameraFileLocator:
    match = _CAMERA_FILE_LINE.fullmatch(line)
    if match is None:
        raise CliParseError(
            "capture 机内文件行格式无效", code=CaptureParseErrorCode.MALFORMED_CAMERA_LOCATOR
        )
    camera_path = match.group(1)
    try:
        locator = CameraFileLocator(
            CameraLocatorStatus.PATH,
            camera_path=camera_path,
            camera_name=PurePosixPath(camera_path).name,
        )
    except (TypeError, ValueError) as error:
        raise CliParseError(
            "capture 机内文件路径不合法", code=CaptureParseErrorCode.MALFORMED_CAMERA_LOCATOR
        ) from error
    if not locator.camera_name.casefold().endswith(description.original_extension.casefold()):
        raise CliParseError(
            "capture 机内文件格式与请求格式不符", code=CaptureParseErrorCode.MALFORMED_CAMERA_LOCATOR
        )
    return locator


def parse_capture(
    output: bytes,
    stderr: bytes,
    *,
    output_complete: bool,
    capture_format: CaptureFormat,
    output_directory: str,
) -> CaptureResponse:
    """按请求格式严格解析唯一的双文件响应和互斥机内定位。"""
    if output_complete is not True or type(output) is not bytes or type(stderr) is not bytes:
        raise CliParseError("capture 必须取得完整且独立的 stdout/stderr", code=CaptureParseErrorCode.INVALID_RESPONSE)
    if not isinstance(capture_format, CaptureFormat):
        raise CliParseError("capture_format 必须是已登记的格式", code=CaptureParseErrorCode.INVALID_RESPONSE)
    description = CAPTURE_FORMATS[capture_format]
    directory = _canonical_absolute_directory(output_directory)
    decoded = _decode(output, "capture")
    try:
        lines = _lines(decoded)
    except CliParseError as error:
        marker = "camera file:"
        if marker in decoded:
            camera_row = decoded.split(marker, 1)[1].split("\n", 1)[0]
            if any(unicodedata.category(character) in {"Cc", "Zl", "Zp"}
                   for character in camera_row):
                raise CliParseError(
                    "capture 机内文件路径包含控制字符",
                    code=CaptureParseErrorCode.MALFORMED_CAMERA_LOCATOR,
                ) from error
        raise
    stdout_records = [line for line in lines
                      if line.startswith("camera file") or line == _UNREPORTED_CAMERA_FILE_LINE]
    # 其他 stderr 字节只属于诊断；仅解码已有定位语法，不借诊断建立成功。
    stderr_records = []
    for record in stderr.split(b"\n"):
        if record.startswith(b"camera file") or record == _UNREPORTED_CAMERA_FILE_LINE.encode():
            decoded_record = _decode(record, "capture stderr")
            if _NON_LF_LINE_SEPARATORS.intersection(decoded_record):
                raise CliParseError("capture 机内定位包含控制字符", code=CaptureParseErrorCode.MALFORMED_CAMERA_LOCATOR)
            stderr_records.append(decoded_record)
    camera_records = [("stdout", line) for line in stdout_records] + [("stderr", line) for line in stderr_records]
    if len(camera_records) > 1:
        raise CliParseError(
            "capture 输出包含重复或冲突的机内定位", code=CaptureParseErrorCode.AMBIGUOUS_CAMERA_LOCATOR
        )
    if lines.count("capture: OK") != 1 or not lines or lines[0] != "capture: OK":
        raise CliParseError(
            "capture 输出必须以唯一的 capture: OK 开始", code=CaptureParseErrorCode.INVALID_RESPONSE
        )
    if len(lines) not in (3, 4):
        if len(camera_records) == 1 and len(lines) > 4 and lines[3].startswith("camera file"):
            _camera_locator(lines[3], description)
        raise CliParseError(
            "capture 输出必须包含两条 saved 记录和至多一条机内定位",
            code=CaptureParseErrorCode.INVALID_RESPONSE,
        )

    first = _captured_file(lines[1], directory)
    second = _captured_file(lines[2], directory)
    if first.host_path == second.host_path:
        raise CliParseError(
            "capture 输出重复引用同一主机文件", code=CaptureParseErrorCode.INVALID_RESPONSE
        )

    candidates = []
    for original, preview in ((first, second), (second, first)):
        if (
            original.host_name.casefold().endswith(description.original_extension.casefold())
            and preview.host_name == f"{original.host_name}.thumbnail{description.preview_extension}"
        ):
            candidates.append((original, preview))
    if len(candidates) != 1:
        raise CliParseError(
            "capture 输出的原图格式或预览配对与请求格式不符",
            code=CaptureParseErrorCode.INVALID_RESPONSE,
        )
    original_saved, preview_saved = candidates[0]
    original = CapturedFile(
        original_saved.host_path,
        original_saved.host_name,
        original_saved.size,
        description.original_media_type,
    )
    preview = CapturedFile(
        preview_saved.host_path,
        preview_saved.host_name,
        preview_saved.size,
        description.preview_media_type,
    )

    if not camera_records:
        raise CliParseError(
            "capture 成功响应未报告机内路径，也没有明确缺名提示",
            code=CaptureParseErrorCode.UNREPORTED_CAMERA_LOCATOR,
        )
    if len(lines) == 4 and (not stdout_records or lines[3] != stdout_records[0]):
        raise CliParseError("capture stdout 包含无法解释的额外记录", code=CaptureParseErrorCode.INVALID_RESPONSE)
    _channel, locator_line = camera_records[0]
    if locator_line == _UNREPORTED_CAMERA_FILE_LINE:
        locator = CameraFileLocator(CameraLocatorStatus.EXPLICIT_MISSING)
    elif locator_line.startswith("camera file"):
        locator = _camera_locator(
            locator_line, description
        )
    else:
        raise CliParseError(
            "capture 输出包含无法解释的额外记录", code=CaptureParseErrorCode.INVALID_RESPONSE
        )
    return CaptureResponse(original=original, preview=preview, camera_locator=locator)


def parse_device_capture(
    output: bytes, stderr: bytes, *, output_complete: bool,
    capture_format: CaptureFormat, output_directory: str,
) -> DeviceCaptureResponse:
    """独立确认相机控制与定位；本地文件记录只决定同步候选。"""
    if output_complete is not True or type(output) is not bytes or type(stderr) is not bytes:
        raise CliParseError("capture 必须取得完整且独立的 stdout/stderr")
    if not isinstance(capture_format, CaptureFormat):
        raise CliParseError("capture_format 必须是已登记的格式")
    _canonical_absolute_directory(output_directory)
    lines = output.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if not lines or lines[0] != b"capture: OK" or sum(line == b"capture: OK" for line in lines) != 1:
        raise CliParseError("capture 输出必须以唯一的 capture: OK 开始")
    if any(line.startswith(b"capture:") and line not in
           (b"capture: OK", _UNREPORTED_CAMERA_FILE_LINE.encode()) for line in lines):
        raise CliParseError("capture 输出包含冲突的控制结果")
    locator_records = [line for line in (*lines, *stderr.split(b"\n"))
                       if line.startswith(b"camera file") or
                       line == _UNREPORTED_CAMERA_FILE_LINE.encode()]
    if len(locator_records) != 1:
        raise CliParseError("capture 机内定位缺失、重复或冲突")
    locator_line = _decode(locator_records[0], "capture camera locator")
    if locator_line == _UNREPORTED_CAMERA_FILE_LINE:
        raise CliParseError("相机正式源缺少机内定位")
    locator = _camera_locator(locator_line, CAPTURE_FORMATS[capture_format])

    local_lines = [line for line in lines[1:] if line not in locator_records]
    # 各角色分别判断；一个可安全定位的文件不能因另一角色缺失被丢弃。
    candidates: dict[str, list[_SavedFile]] = {"original": [], "preview": []}
    invalid: dict[str, str] = {}
    description = CAPTURE_FORMATS[capture_format]
    suffix = ".thumbnail" + description.preview_extension
    def role_for(name):
        if name.endswith(suffix):
            original_name = name[:-len(suffix)]
            return "preview" if original_name.casefold().endswith(
                description.original_extension.casefold()) else None
        if name.casefold().endswith(description.original_extension.casefold()):
            return "original"
        return None
    try:
        for raw in local_lines:
            line = _decode(raw, "capture host record")
            if _NON_LF_LINE_SEPARATORS.intersection(line):
                raise CliParseError("capture 主机文件记录包含非法换行")
            # 名称只用于归类错误；无效路径绝不成为后续文件操作凭据。
            match = re.fullmatch(r"saved (.+) \([^\n]*\)", line)
            role = None if match is None else role_for(PurePosixPath(match.group(1)).name)
            if role is None:
                raise CliParseError("capture 主机文件名称不符合请求格式")
            try:
                saved = _captured_file(line, _canonical_absolute_directory(output_directory))
            except CliParseError as error:
                invalid[role] = str(error)
            else:
                candidates[role].append(saved)
    except CliParseError as error:
        return DeviceCaptureResponse(locator, None, None, str(error), str(error), str(error))
    for role, values in candidates.items():
        if len(values) > 1 or values and role in invalid:
            invalid[role] = "capture 主机文件角色存在冲突"
            values.clear()
    original = candidates["original"][0] if candidates["original"] else None
    preview = candidates["preview"][0] if candidates["preview"] else None
    if original is not None and preview is not None and preview.host_name != (
            original.host_name + ".thumbnail" + description.preview_extension):
        error = "capture 主机文件配对存在冲突"
        return DeviceCaptureResponse(locator, None, None, error, error, error)
    def convert(saved, media):
        return None if saved is None else CapturedFile(saved.host_path, saved.host_name, saved.size, media)
    original_error = invalid.get("original") or (None if original is not None else "主机原图记录缺失")
    preview_error = invalid.get("preview") or (None if preview is not None else "主机预览记录缺失")
    return DeviceCaptureResponse(locator, convert(original, description.original_media_type),
        convert(preview, description.preview_media_type), original_error or preview_error,
        original_error, preview_error)
