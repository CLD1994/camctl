"""随原业务结果持有诊断，在必要业务收场后交给命令日志目标。"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, fields, is_dataclass
from decimal import Decimal
from enum import Enum
import json
from os import PathLike

from camctl.logging_runtime.admission import AdmissionKind
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime import service
from camctl.logging_runtime.service import LogChannel, LogRecord
from camctl.operations.error_diagnostics import exception_diagnostic
from camctl.operations.models import CallOutcome, ErrorValue
from camctl.operations.process import RawToolOutcome
from camctl.operations.result_format import error_document
from camctl.operations.tool_diagnostics import ToolDiagnostics, ToolStream, encode_diagnostics

__all__ = [
    "DiagnosticState", "DiagnosticStatus", "record_error", "record_call_outcome",
    "error_diagnostics_context", "diagnostic_scope", "flush_diagnostics", "diagnostic_state_for",
    "diagnostic_record_for", "acknowledge_diagnostic",
]


class DiagnosticStatus(Enum):
    NEW = "new"
    HELD = "held"
    ADMITTED = "admitted"
    FALLBACK = "fallback"


@dataclass(eq=False)
class DiagnosticState:
    """由原结果持有的一次诊断；终态不再登记，不保留全局身份集合。"""

    status: DiagnosticStatus = field(default=DiagnosticStatus.NEW, init=False)
    _record: LogRecord | None = field(default=None, init=False, repr=False)
    _fallback_only: bool = field(default=False, init=False, repr=False)


@dataclass
class _Owner:
    pending: deque[DiagnosticState] = field(default_factory=deque)
    closed: bool = False


@dataclass
class _Target:
    channel: LogChannel | None
    fallback: Callable[[str], None]
    root: _Owner = field(default_factory=_Owner)
    closed: bool = False


@dataclass
class _Scope:
    target: _Target
    owner: _Owner = field(default_factory=_Owner)


_target: ContextVar[_Target | None] = ContextVar("error_diagnostics_target", default=None)
_scope: ContextVar[_Scope | None] = ContextVar("error_diagnostics_scope", default=None)


@contextmanager
def error_diagnostics_context(channel: LogChannel | None, fallback: Callable[[str], None]):
    """装配命令目标；命令负责在退出本上下文前完成必要收场与 flush。"""
    target = _Target(channel, fallback)
    token = _target.set(target)
    try:
        yield
    finally:
        target.closed = True
        _target.reset(token)


@asynccontextmanager
async def diagnostic_scope():
    """正常 body 返回后交接；异常先传播，尚未交接的诊断由命令持有。"""
    target = _target.get()
    if target is None or target.closed:
        yield
        return
    scope = _Scope(target)
    token = _scope.set(scope)
    try:
        try:
            yield
        except BaseException:
            scope.owner.closed = True
            _retain_at_root(scope.owner, target)
            raise
        else:
            # 子任务继承的是同一个 scope；关闭后到达的记录直接交命令。
            scope.owner.closed = True
            try:
                await _drain(scope.owner, target)
            except BaseException:
                _retain_at_root(scope.owner, target)
                raise
    finally:
        _scope.reset(token)


async def flush_diagnostics() -> None:
    """在必要业务收场后、日志消费者关闭前交接命令持有的诊断。"""
    target = _target.get()
    if target is None or target.closed:
        return
    first_cancel = None
    while target.root.pending:
        try:
            await _drain(target.root, target)
        except asyncio.CancelledError as error:
            # 必要业务收场已经完成；命令继续交接其余记录再传播原取消。
            if first_cancel is None:
                first_cancel = error
    if first_cancel is not None:
        raise first_cancel


def record_error(
    error: object,
    *,
    stage: str,
    context: Mapping | None = None,
    state: DiagnosticState | None = None,
    details: Mapping | None = None,
) -> DiagnosticState:
    """同步冻结一次实际错误，不等待接纳，也不创建后台任务。"""
    if state is None:
        state = diagnostic_state_for(error)
    state = _record({"stage": stage, "error": error, "context": context, "details": details}, state)
    if isinstance(error, BaseException) and state.status is not DiagnosticStatus.NEW:
        error._camctl_diagnostic_state = state
    return state


def diagnostic_state_for(error: object) -> DiagnosticState | None:
    """查询原异常自身持有的诊断状态，不改变异常类型或维护全局身份。"""
    if isinstance(error, BaseException):
        state = getattr(error, "_camctl_diagnostic_state", None)
        if isinstance(state, DiagnosticState):
            return state
    return None


def diagnostic_record_for(state: DiagnosticState) -> LogRecord | None:
    """取得原不可变记录，供需要实际写入凭据的日志副本流程复用。"""
    return state._record


def acknowledge_diagnostic(state: DiagnosticState) -> None:
    """副本拥有者取得实际追加成功凭据后交接原记录，不推断写入结果。"""
    if state.status is DiagnosticStatus.HELD:
        state.status = DiagnosticStatus.ADMITTED


def record_call_outcome(
    outcome: CallOutcome,
    *,
    ticket=None,
    run_error: ErrorValue | BaseException | None = None,
    context: Mapping | None = None,
    state: DiagnosticState | None = None,
) -> DiagnosticState | None:
    """保留调用错误及伴随流程错误；正常控制分支由业务调用方分类。"""
    if state is None:
        state = diagnostic_state_for(run_error)
    if state is not None and state.status is not DiagnosticStatus.NEW:
        if isinstance(run_error, BaseException):
            run_error._camctl_diagnostic_state = state
        return state
    if outcome.error is None and run_error is None:
        return None
    error = outcome.error if outcome.error is not None else run_error
    stage = error.stage if isinstance(error, ErrorValue) else "call_outcome"
    state = _record({
        "stage": stage, "error": error, "outcome": outcome, "ticket": ticket,
        "run_error": run_error, "context": context,
    }, state)
    if isinstance(run_error, BaseException) and state.status is not DiagnosticStatus.NEW:
        run_error._camctl_diagnostic_state = state
    return state


def _record(payload: Mapping, state: DiagnosticState | None) -> DiagnosticState:
    state = state if state is not None else DiagnosticState()
    if state.status is not DiagnosticStatus.NEW:
        return state
    target = _target.get()
    if target is None or target.closed:
        return state
    try:
        document = _document(payload)
    except Exception as error:
        # 编码器拒绝原结果时，仍沿原 dataclass 字段保存安全字节事实。
        document = {"diagnostic_format_error": type(error).__name__,
                    "diagnostic": _document(payload, structured=False)}
        state._fallback_only = True
    try:
        message = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    except Exception as error:
        # 格式化失败仍持有已经排除媒体 stdout 的完整事实，不能截断。
        message = f"错误诊断格式化失败：{type(error).__name__}: {error}; diagnostic={document!r}"
        state._fallback_only = True
    state._record = LogRecord(LogLevel.ERROR, message)
    state.status = DiagnosticStatus.HELD
    scope = _scope.get()
    owner = (scope.owner if scope is not None and scope.target is target
             and not scope.owner.closed else target.root)
    owner.pending.append(state)
    return state


def _document(value, *, name: str | None = None, structured: bool = True):
    """复用原结构形成独立快照，并给已有 stderr 字节编码补可读文本。"""
    def convert(item, *, name=None):
        return _document(item, name=name, structured=structured)

    if isinstance(value, BaseException):
        return convert(exception_diagnostic(value))
    if isinstance(value, ErrorValue):
        if structured:
            return convert(error_document(value))
    if isinstance(value, ToolDiagnostics) and structured:
        result = encode_diagnostics(value)
        for call in result["calls"]:
            stdout = call["stdout"]
            encoded = stdout.pop("hex")
            stdout["byte_count"] = None if encoded is None else len(encoded) // 2
            stderr = call["stderr"]
            stderr["text"] = (None if stderr["hex"] is None else
                              bytes.fromhex(stderr["hex"]).decode("utf-8", "backslashreplace"))
        return result
    if isinstance(value, Enum):
        return convert(value.value)
    if isinstance(value, bytes):
        if name == "stdout":
            return {"byte_count": len(value)}
        if name == "stderr":
            return {"hex": value.hex(), "text": value.decode("utf-8", "backslashreplace")}
        return {"bytes_hex": value.hex()}
    if is_dataclass(value) and not isinstance(value, type):
        if name == "stdout" and isinstance(value, ToolStream):
            data = value.data
            return {"state": convert(value.state),
                    "byte_count": len(data) if isinstance(data, bytes) else None}
        result = {}
        for item in fields(value):
            field_name = item.name
            if isinstance(value, RawToolOutcome) and field_name == "output":
                field_name = "stdout"
            elif isinstance(value, ToolStream) and field_name == "data" and name == "stderr":
                field_name = "stderr"
            result[item.name] = convert(getattr(value, item.name), name=field_name)
        return result
    if isinstance(value, Mapping):
        result = {key: convert(item, name=key) for key, item in value.items()}
        for encoding in ("hex", "base64"):
            encoded = result.get(f"stderr_{encoding}")
            if isinstance(encoded, str):
                try:
                    raw = (bytes.fromhex(encoded) if encoding == "hex" else
                           base64.b64decode(encoded, validate=True))
                except ValueError:
                    continue  # 原诊断含非规范字节编码时仍保留原值。
                result["stderr_text"] = raw.decode("utf-8", "backslashreplace")
        return result
    if isinstance(value, (tuple, list)):
        return [convert(item) for item in value]
    if isinstance(value, (Decimal, PathLike)):
        return str(value)
    return value


def _retain_at_root(owner: _Owner, target: _Target) -> None:
    target.root.pending.extend(owner.pending)
    owner.pending.clear()


def _admission_result_for(error: BaseException):
    # 在日志基础接口接入前亦可导入本模块；最终装配提供实际接纳结果。
    reader = getattr(service, "admission_result_for", None)
    return None if reader is None else reader(error)


async def _drain(owner: _Owner, target: _Target) -> None:
    while owner.pending:
        state = owner.pending[0]
        if state.status is not DiagnosticStatus.HELD:
            owner.pending.popleft()
            continue
        record = state._record
        assert record is not None
        if target.channel is None or state._fallback_only:
            _fallback(state, target)
        else:
            try:
                result = await target.channel.alog(record)
            except Exception:
                _fallback(state, target)
            except BaseException as error:
                result = _admission_result_for(error)
                if result is not None and result.decision.kind is AdmissionKind.ACCEPTED:
                    state.status = DiagnosticStatus.ADMITTED
                    owner.pending.popleft()
                elif result is not None and result.decision.kind is AdmissionKind.CHANNEL_FAILED:
                    _fallback(state, target)
                    owner.pending.popleft()
                raise
            else:
                if result.decision.kind is AdmissionKind.ACCEPTED:
                    state.status = DiagnosticStatus.ADMITTED
                else:
                    _fallback(state, target)
        owner.pending.popleft()


def _fallback(state: DiagnosticState, target: _Target) -> None:
    assert state._record is not None
    try:
        target.fallback(state._record.message)
    except Exception:
        # stderr 本身已经失效，不递归诊断或反复写入，也不影响业务结果。
        pass
    state.status = DiagnosticStatus.FALLBACK
