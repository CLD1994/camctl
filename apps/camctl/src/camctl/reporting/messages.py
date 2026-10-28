"""报告控制消息：主进程与报告子进程之间的字节消息契约。

所有控制消息是 UTF-8 JSON 对象，带统一版本号，总长度不超过固定
容量；字段集按消息种类封闭，额外字段、未知版本或非法取值一律拒
绝。任务身份区分同一报告的不同生成调用；结果携带数据库身份、
文件身份、长度与摘要，或明确的错误分类。错误分类保留报告失败与
状态库错误的区别。
"""

from __future__ import annotations

import enum
import json
import re
import secrets
from dataclasses import dataclass

from camctl.operations.error_diagnostics import ExceptionDiagnosticGraph, ExceptionDiagnosticReference

__all__ = [
    "MAX_MESSAGE_BYTES",
    "ControlMessage",
    "ErrorKind",
    "JobMessage",
    "MessageKind",
    "MessageCapacityError",
    "MessageProtocolError",
    "ReadyMessage",
    "ResultFailureMessage",
    "ResultSuccessMessage",
    "ShutdownMessage",
    "StartupFailedMessage",
    "StartupPhase",
    "decode_message",
    "encode_message",
    "new_job_id",
]

#: 单条控制消息的最大字节数（UTF-8 JSON 载荷）。
MAX_MESSAGE_BYTES = 64 * 1024
#: 控制消息协议版本。
MESSAGE_VERSION = 1

_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_INSTANCE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_PATH_CHARS = 2048
_MAX_CODE_CHARS = 128
_MAX_TEXT_CHARS = 1024


class MessageProtocolError(Exception):
    """控制消息违反通信契约（版本、字段、取值或容量）。"""


class MessageCapacityError(MessageProtocolError):
    """完整控制消息超过固定容量；发送方可改用明确的诊断通道。"""


class MessageKind(enum.Enum):
    READY = "ready"
    STARTUP_FAILED = "startup_failed"
    JOB = "job"
    RESULT = "result"
    SHUTDOWN = "shutdown"


class StartupPhase(enum.Enum):
    """子进程启动失败的阶段：保护、工作锁与运行库检查。"""

    PARENT_GUARD = "parent_guard"
    WORK_LOCK = "work_lock"
    RUNTIME_CHECK = "runtime_check"


class ErrorKind(enum.Enum):
    """结果错误的分类：普通报告失败与状态库或历史解释错误。"""

    REPORT = "report"
    STATE = "state"


def _require_int(value, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MessageProtocolError(f"{name} 必须是不小于 {minimum} 的整数: {value!r}")
    return value


def _require_text(value, name: str, *, maximum: int) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or "\x00" in value):
        raise MessageProtocolError(
            f"{name} 必须是不超过 {maximum} 字符且不含空字节的非空文本")
    return value


def _require_pattern(value, name: str, pattern: re.Pattern) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise MessageProtocolError(f"{name} 不符合固定格式: {value!r}")
    return value


def _require_diagnostic(diagnostic, fallback, size_bytes) -> None:
    if diagnostic is not None and not isinstance(diagnostic, dict):
        raise MessageProtocolError("diagnostic 必须是 JSON 对象或 None")
    if fallback is None:
        if size_bytes is not None:
            raise MessageProtocolError("diagnostic_size_bytes 必须与降级通道同时提供")
        return
    if fallback != "stderr":
        raise MessageProtocolError("diagnostic_fallback 仅支持 stderr")
    if diagnostic is not None:
        raise MessageProtocolError("完整诊断与降级通道不能同时提供")
    _require_int(size_bytes, "diagnostic_size_bytes", minimum=1)


@dataclass(frozen=True)
class ReadyMessage:
    """子进程就绪通知：可以接收生成任务。"""


@dataclass(frozen=True)
class StartupFailedMessage:
    """子进程启动失败：失败阶段与原因，不含任务数据。"""

    phase: StartupPhase
    reason: str
    diagnostic: dict | None = None
    diagnostic_fallback: str | None = None
    diagnostic_size_bytes: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.phase, StartupPhase):
            raise MessageProtocolError(f"启动失败阶段必须使用 StartupPhase: {self.phase!r}")
        _require_text(self.reason, "reason", maximum=256)
        _require_diagnostic(
            self.diagnostic, self.diagnostic_fallback, self.diagnostic_size_bytes)


@dataclass(frozen=True)
class JobMessage:
    """一次生成任务：报告身份、冻结依据、位置与执行参数。"""

    job_id: str
    report_id: int
    from_wm: int
    to_wm: int
    frozen_event_id: int
    instance_id: str
    db_path: str
    staging_path: str
    staging_root: str
    ready_root: str
    processing_root: str
    entity_batch_size: int
    event_batch_size: int
    busy_timeout_ms: int

    def __post_init__(self) -> None:
        _require_pattern(self.job_id, "job_id", _JOB_ID_PATTERN)
        _require_int(self.report_id, "report_id", minimum=1)
        _require_int(self.from_wm, "from_wm", minimum=0)
        _require_int(self.to_wm, "to_wm", minimum=0)
        _require_int(self.frozen_event_id, "frozen_event_id", minimum=0)
        if self.to_wm < self.from_wm:
            raise MessageProtocolError("to_wm 不能小于 from_wm")
        _require_pattern(self.instance_id, "instance_id", _INSTANCE_ID_PATTERN)
        _require_text(self.db_path, "db_path", maximum=_MAX_PATH_CHARS)
        _require_text(self.staging_path, "staging_path", maximum=_MAX_PATH_CHARS)
        for name in ("staging_root", "ready_root", "processing_root"):
            _require_text(getattr(self, name), name, maximum=_MAX_PATH_CHARS)
        _require_int(self.entity_batch_size, "entity_batch_size", minimum=1)
        _require_int(self.event_batch_size, "event_batch_size", minimum=1)
        _require_int(self.busy_timeout_ms, "busy_timeout_ms", minimum=1)


@dataclass(frozen=True)
class ResultSuccessMessage:
    """生成成功：任务身份、数据库身份、文件位置、长度与摘要。"""

    job_id: str
    instance_id: str
    path: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        _require_pattern(self.job_id, "job_id", _JOB_ID_PATTERN)
        _require_pattern(self.instance_id, "instance_id", _INSTANCE_ID_PATTERN)
        _require_text(self.path, "path", maximum=_MAX_PATH_CHARS)
        _require_int(self.size_bytes, "size_bytes", minimum=0)
        _require_pattern(self.sha256, "sha256", _SHA256_PATTERN)


@dataclass(frozen=True)
class ResultFailureMessage:
    """生成失败：任务身份、数据库身份与明确的错误分类。"""

    job_id: str
    instance_id: str
    error_kind: ErrorKind
    error_code: str
    error_message: str
    diagnostic: dict | None = None
    diagnostic_fallback: str | None = None
    diagnostic_size_bytes: int | None = None

    def __post_init__(self) -> None:
        _require_pattern(self.job_id, "job_id", _JOB_ID_PATTERN)
        _require_pattern(self.instance_id, "instance_id", _INSTANCE_ID_PATTERN)
        if not isinstance(self.error_kind, ErrorKind):
            raise MessageProtocolError(f"错误分类必须使用 ErrorKind: {self.error_kind!r}")
        _require_text(self.error_code, "error_code", maximum=_MAX_CODE_CHARS)
        _require_text(self.error_message, "error_message", maximum=_MAX_TEXT_CHARS)
        _require_diagnostic(
            self.diagnostic, self.diagnostic_fallback, self.diagnostic_size_bytes)


@dataclass(frozen=True)
class ShutdownMessage:
    """正常退出指令：子进程收到后结束自身。"""


ControlMessage = (
    ReadyMessage
    | StartupFailedMessage
    | JobMessage
    | ResultSuccessMessage
    | ResultFailureMessage
    | ShutdownMessage
)

# 各消息在线上的封闭字段集（不含 version 与 kind）。
_RESULT_SUCCESS_FIELDS = {"status"}
_RESULT_FAILURE_FIELDS = {"status"}
_DIAGNOSTIC_FIELDS = {"diagnostic", "diagnostic_fallback", "diagnostic_size_bytes"}
_DIAGNOSTIC_WIRE_FIELDS = _DIAGNOSTIC_FIELDS | {"diagnostic_graphs"}


def _diagnostic_fields(message: StartupFailedMessage | ResultFailureMessage) -> dict:
    return {name: getattr(message, name) for name in _DIAGNOSTIC_FIELDS
            if getattr(message, name) is not None}


def encode_message(message: ControlMessage) -> bytes:
    """把已校验的控制消息编码为不超过容量的 UTF-8 JSON 字节。"""
    payload: dict[str, object] = {"version": MESSAGE_VERSION}
    if isinstance(message, ReadyMessage):
        payload["kind"] = MessageKind.READY.value
    elif isinstance(message, StartupFailedMessage):
        payload.update(kind=MessageKind.STARTUP_FAILED.value,
                       phase=message.phase.value, reason=message.reason,
                       **_diagnostic_fields(message))
    elif isinstance(message, JobMessage):
        payload.update(kind=MessageKind.JOB.value, **vars(message))
    elif isinstance(message, ResultSuccessMessage):
        payload.update(kind=MessageKind.RESULT.value, status="success", **vars(message))
    elif isinstance(message, ResultFailureMessage):
        payload.update(kind=MessageKind.RESULT.value, status="failure",
                       error_kind=message.error_kind.value,
                       error_code=message.error_code,
                       error_message=message.error_message,
                       job_id=message.job_id, instance_id=message.instance_id,
                       **_diagnostic_fields(message))
    elif isinstance(message, ShutdownMessage):
        payload["kind"] = MessageKind.SHUTDOWN.value
    else:
        raise MessageProtocolError(f"未知控制消息类型: {type(message).__name__}")
    if isinstance(message, (StartupFailedMessage, ResultFailureMessage)) and message.diagnostic is not None:
        try:
            metadata = _diagnostic_graph_metadata(message.diagnostic)
        except (TypeError, ValueError) as error:
            raise MessageProtocolError("诊断图标记不能编码") from error
        if metadata is not None:
            payload["diagnostic_graphs"] = metadata
    try:
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8", "backslashreplace")
    except (TypeError, ValueError) as error:
        raise MessageProtocolError("控制消息不能编码为 JSON") from error
    if len(data) > MAX_MESSAGE_BYTES:
        raise MessageCapacityError(
            f"控制消息超过容量 {MAX_MESSAGE_BYTES} 字节: {len(data)}")
    return data


def decode_message(data: bytes) -> ControlMessage:
    """按封闭字段集与固定取值范围解码一条控制消息。"""
    if not isinstance(data, (bytes, bytearray)):
        raise MessageProtocolError("控制消息必须是字节串")
    if len(data) > MAX_MESSAGE_BYTES:
        raise MessageCapacityError(
            f"控制消息超过容量 {MAX_MESSAGE_BYTES} 字节: {len(data)}")
    try:
        payload = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise MessageProtocolError("控制消息不是有效的 UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise MessageProtocolError("控制消息必须是 JSON 对象")
    version = payload.get("version")
    if version != MESSAGE_VERSION or isinstance(version, bool):
        raise MessageProtocolError(f"控制消息版本不受支持: {version!r}")
    kind = payload.get("kind")
    try:
        message_kind = MessageKind(kind)
    except ValueError as error:
        raise MessageProtocolError(f"未知控制消息种类: {kind!r}") from error
    fields = set(payload) - {"version", "kind"}
    if message_kind is MessageKind.READY:
        _require_fields(fields, set())
        return ReadyMessage()
    if message_kind is MessageKind.SHUTDOWN:
        _require_fields(fields, set())
        return ShutdownMessage()
    if message_kind is MessageKind.STARTUP_FAILED:
        _require_fields(fields - _DIAGNOSTIC_WIRE_FIELDS, {"phase", "reason"})
        _restore_wire_diagnostic(payload)
        try:
            phase = StartupPhase(payload["phase"])
        except ValueError as error:
            raise MessageProtocolError(
                f"未知启动失败阶段: {payload['phase']!r}") from error
        return StartupFailedMessage(
            phase=phase, reason=payload["reason"],
            **{name: payload.get(name) for name in _DIAGNOSTIC_FIELDS})
    if message_kind is MessageKind.JOB:
        _require_fields(fields, {
            "job_id", "report_id", "from_wm", "to_wm", "frozen_event_id",
            "instance_id", "db_path", "staging_path", "staging_root", "ready_root", "processing_root",
            "entity_batch_size", "event_batch_size", "busy_timeout_ms"})
        return JobMessage(
            job_id=payload["job_id"], report_id=payload["report_id"],
            from_wm=payload["from_wm"], to_wm=payload["to_wm"],
            frozen_event_id=payload["frozen_event_id"],
            instance_id=payload["instance_id"], db_path=payload["db_path"],
            staging_path=payload["staging_path"],
            staging_root=payload["staging_root"], ready_root=payload["ready_root"],
            processing_root=payload["processing_root"],
            entity_batch_size=payload["entity_batch_size"],
            event_batch_size=payload["event_batch_size"],
            busy_timeout_ms=payload["busy_timeout_ms"])
    # MessageKind.RESULT：成功与失败的字段集互斥。
    status = payload.get("status")
    if status == "success":
        _require_fields(fields, _RESULT_SUCCESS_FIELDS | {
            "job_id", "instance_id", "path", "size_bytes", "sha256"})
        return ResultSuccessMessage(
            job_id=payload["job_id"], instance_id=payload["instance_id"],
            path=payload["path"], size_bytes=payload["size_bytes"],
            sha256=payload["sha256"])
    if status == "failure":
        _require_fields(fields - _DIAGNOSTIC_WIRE_FIELDS, _RESULT_FAILURE_FIELDS | {
            "job_id", "instance_id", "error_kind", "error_code", "error_message"})
        _restore_wire_diagnostic(payload)
        try:
            error_kind = ErrorKind(payload["error_kind"])
        except ValueError as error:
            raise MessageProtocolError(
                f"未知错误分类: {payload['error_kind']!r}") from error
        return ResultFailureMessage(
            job_id=payload["job_id"], instance_id=payload["instance_id"],
            error_kind=error_kind, error_code=payload["error_code"],
            error_message=payload["error_message"],
            **{name: payload.get(name) for name in _DIAGNOSTIC_FIELDS})
    raise MessageProtocolError(f"结果消息缺少合法 status: {status!r}")


def _pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _pointer_parts(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or (value != "#" and not value.startswith("#/")):
        raise ValueError("诊断图路径不是 JSON pointer")
    if value == "#":
        return ()
    parts = value[2:].split("/")
    if any(re.search(r"~(?![01])", part) for part in parts):
        raise ValueError("诊断图路径转义无效")
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in parts)


def _json_key(key) -> str:
    if isinstance(key, str):
        return key
    if key is None:
        return "null"
    if isinstance(key, bool):
        return "true" if key else "false"
    if isinstance(key, (int, float)):
        return json.dumps(key)
    raise TypeError("诊断对象键不能编码为 JSON")


def _diagnostic_graph_metadata(diagnostic: dict) -> dict | None:
    """仅传递编码器实际标记的图和引用；普通业务映射保持原义。"""
    graphs, references, active = [], [], set()

    def visit(value, path, ancestors):
        if isinstance(value, ExceptionDiagnosticGraph):
            graphs.append({"path": path, "root": value.root_path})
            ancestors = (*ancestors, (path, value._scope))
        elif isinstance(value, ExceptionDiagnosticReference):
            owners = [item for item in ancestors if item[1] is value._scope]
            if value._scope is not None and not owners:
                raise ValueError("诊断引用缺少所属图")
            if not ancestors:
                raise ValueError("诊断引用缺少所属图")
            owner = owners[-1] if owners else ancestors[-1]
            references.append({"path": path, "graph": owner[0]})
            return
        if isinstance(value, (dict, list, tuple)):
            identity = id(value)
            if identity in active:
                raise ValueError("诊断对象包含循环容器")
            active.add(identity)
            try:
                if isinstance(value, dict):
                    for key, child in value.items():
                        visit(child, path + "/" + _pointer_part(_json_key(key)), ancestors)
                else:
                    for index, child in enumerate(value):
                        visit(child, path + f"/{index}", ancestors)
            finally:
                active.remove(identity)

    visit(diagnostic, "#", ())
    if not graphs:
        return None
    return {"version": 1, "graphs": graphs, "references": references}


def _pointer_get(root, parts):
    node = root
    for part in parts:
        if isinstance(node, dict):
            node = node[part]
        elif isinstance(node, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
            node = node[int(part)]
        else:
            raise ValueError("诊断图路径不指向容器成员")
    return node


def _pointer_replace(root, parts, value):
    if not parts:
        return value
    parent = _pointer_get(root, parts[:-1])
    part = parts[-1]
    if isinstance(parent, dict):
        parent[part] = value
    elif isinstance(parent, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
        parent[int(part)] = value
    else:
        raise ValueError("诊断图路径不指向容器成员")
    return root


def _restore_wire_diagnostic(payload: dict) -> None:
    if "diagnostic_graphs" not in payload:
        return
    diagnostic, metadata = payload.get("diagnostic"), payload["diagnostic_graphs"]
    try:
        if (not isinstance(diagnostic, dict) or not isinstance(metadata, dict)
                or set(metadata) != {"version", "graphs", "references"}
                or type(metadata["version"]) is not int or metadata["version"] != 1
                or not isinstance(metadata["graphs"], list) or not metadata["graphs"]
                or not isinstance(metadata["references"], list)):
            raise ValueError("诊断图标记字段无效")
        graphs = {}
        for entry in metadata["graphs"]:
            if not isinstance(entry, dict) or set(entry) != {"path", "root"}:
                raise ValueError("诊断图边界无效")
            path, root = _pointer_parts(entry["path"]), _pointer_parts(entry["root"])
            if path in graphs:
                raise ValueError("诊断图边界重复")
            node = _pointer_get(diagnostic, path)
            if (not isinstance(node, dict) or not {"type", "traceback", "cause", "context", "suppress_context"} <= node.keys()):
                raise ValueError("诊断图边界不是异常图")
            graphs[path] = (entry["root"], root)
        references = {}
        for entry in metadata["references"]:
            if not isinstance(entry, dict) or set(entry) != {"path", "graph"}:
                raise ValueError("诊断图引用字段无效")
            path, owner = _pointer_parts(entry["path"]), _pointer_parts(entry["graph"])
            if path in references or path in graphs:
                raise ValueError("诊断图引用重复或覆盖边界")
            if owner not in graphs or len(owner) >= len(path) or path[:len(owner)] != owner:
                raise ValueError("诊断图引用没有所属图")
            node = _pointer_get(diagnostic, path)
            if not isinstance(node, dict) or set(node) != {"$ref"}:
                raise ValueError("诊断图引用节点无效")
            target = _pointer_parts(node["$ref"])
            source = graphs[owner][1]
            if target[:len(source)] != source:
                raise ValueError("诊断图引用超出原图")
            _pointer_get(_pointer_get(diagnostic, owner), target[len(source):])
            references[path] = owner
        graph_scopes = {}
        for path, (root, _) in sorted(graphs.items(), key=lambda item: len(item[0])):
            node = _pointer_get(diagnostic, path)
            graph = ExceptionDiagnosticGraph(node, root_path=root)
            graph_scopes[path] = graph._scope
            diagnostic = _pointer_replace(diagnostic, path, graph)
        for path, owner in references.items():
            node = _pointer_get(diagnostic, path)
            diagnostic = _pointer_replace(diagnostic, path,
                ExceptionDiagnosticReference(node, _scope=graph_scopes[owner]))
    except (IndexError, KeyError, TypeError, ValueError) as error:
        raise MessageProtocolError("诊断图标记不合法") from error
    payload["diagnostic"] = diagnostic


def _require_fields(actual: set, expected: set) -> None:
    if actual != expected:
        missing = expected - actual
        extra = actual - expected
        raise MessageProtocolError(
            f"字段集不符: 缺少 {sorted(missing)} 多余 {sorted(extra)}")


def new_job_id() -> str:
    """分配新的生成任务身份；同一报告的每次生成都使用新身份。"""
    return secrets.token_hex(16)
