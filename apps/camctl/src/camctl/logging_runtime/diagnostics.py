"""随原业务结果持有诊断，在必要业务收场后交给命令日志目标。"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, fields, is_dataclass, replace
from decimal import Decimal
from enum import Enum
import json
from os import PathLike
import sys
import traceback

from camctl.logging_runtime.admission import AdmissionDecision, AdmissionKind
from camctl.logging_runtime.models import LogLevel, LogWriteResult
from camctl.logging_runtime.receipt import LogReceipt
from camctl.logging_runtime.service import AdmissionResult, LogChannel, LogRecord, admission_result_for
from camctl.logging_runtime.formatting import log_metadata
from camctl.operations.error_diagnostics import DiagnosticError, exception_diagnostic
from camctl.operations.models import AttemptTicket, CallOutcome, ErrorValue
from camctl.operations.process import RawToolOutcome
from camctl.operations.result_format import error_document
from camctl.operations.tool_diagnostics import ToolDiagnostics, ToolStream, encode_diagnostics

__all__ = [
    "DiagnosticState", "DiagnosticStatus", "record_error", "record_call_outcome", "record_availability_error",
    "error_diagnostics_context", "diagnostic_scope", "flush_diagnostics", "diagnostic_state_for",
    "diagnostic_record_for", "acknowledge_diagnostic", "acknowledge_diagnostic_record",
    "DiagnosticReservation", "reserve_diagnostic", "adopt_diagnostic", "release_diagnostic",
    "prepare_log_diagnostic", "submit_diagnostic", "admit_diagnostic", "diagnostic_snapshot",
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
    _target: _Target | None = field(default=None, init=False, repr=False)
    _owner: _Owner | None = field(default=None, init=False, repr=False)
    _admitting: bool = field(default=False, init=False, repr=False)
    _copy_owned: bool = field(default=False, init=False, repr=False)
    _changed: asyncio.Future | None = field(default=None, init=False, repr=False)
    _receipt: LogReceipt | None = field(default=None, init=False, repr=False)
    _submitted: bool = field(default=False, init=False, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)


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
    background: bool = False
    wake: asyncio.Event | None = None
    task: asyncio.Task | None = None
    stopping: bool = False
    finished: bool = False
    serve_error: BaseException | None = None
    error_reported: bool = False
    flushed: bool = False
    reservations: list[DiagnosticReservation] = field(default_factory=list)


class _ReservationStatus(Enum):
    RESERVED = "reserved"
    ADOPTED = "adopted"
    RELEASED = "released"


@dataclass(eq=False)
class DiagnosticReservation:
    """当前命令临时持有的副本交接，不改变原诊断状态或记录。"""

    state: DiagnosticState
    _target: _Target = field(repr=False)
    _status: _ReservationStatus = field(default=_ReservationStatus.RESERVED, repr=False)


@dataclass
class _Scope:
    target: _Target
    owner: _Owner = field(default_factory=_Owner)
    task: asyncio.Task | None = field(default_factory=asyncio.current_task)


_target: ContextVar[_Target | None] = ContextVar("error_diagnostics_target", default=None)
_scope: ContextVar[_Scope | None] = ContextVar("error_diagnostics_scope", default=None)


@contextmanager
def error_diagnostics_context(
    channel: LogChannel | None, fallback: Callable[[str], None], *, background: bool = False,
):
    """装配命令目标；命令负责在退出本上下文前完成必要收场与 flush。"""
    target = _Target(channel, fallback, background=background)
    if background:
        asyncio.get_running_loop()
        target.wake = asyncio.Event()
    token = _target.set(target)
    if background:
        target.task = asyncio.create_task(_serve(target), name="camctl-error-diagnostics")
    try:
        yield
    finally:
        target.closed = True
        _target.reset(token)
        if target.background and not target.flushed:
            target.stopping = True
            _notify(target)
            raise RuntimeError("后台诊断上下文退出前必须 await flush_diagnostics()") from target.serve_error


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
            if target.background:
                _retain_at_root(scope.owner, target)
                return
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
    first_control = None
    for reservation in tuple(target.reservations):
        try:
            release_diagnostic(reservation)
        except BaseException as error:
            first_control = _first_control(first_control, error)
    if target.background:
        try:
            await _finish_background(target)
        except BaseException as error:
            if first_control is None:
                raise
            first_control = _first_control(first_control, error)
        if first_control is not None:
            raise first_control
        return
    if first_control is not None:
        raise first_control
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


def prepare_log_diagnostic(
    record: LogRecord,
    *,
    error: BaseException | None = None,
    state: DiagnosticState | None = None,
    channel: LogChannel | None = None,
    fallback: Callable[[str], object] | None = None,
) -> DiagnosticState:
    """登记已冻结的通用日志；原异常身份与首次记录优先于新的调用。"""
    original = diagnostic_state_for(error)
    state = original if original is not None else state if state is not None else DiagnosticState()
    if state.status is DiagnosticStatus.NEW:
        target = _target.get()
        if target is None:
            selected_fallback = fallback if fallback is not None else record.fallback
            target = _Target(channel, selected_fallback if selected_fallback is not None else _stderr)
        state._record = replace(record, fallback=target.fallback)
        state._target = target
        state.status = DiagnosticStatus.HELD
        _receipt_for(state)
        if error is not None:
            error._camctl_diagnostic_state = state
        if target.closed or target.finished:
            _final_fallback(state, target)
        else:
            scope = _scope.get()
            owner = (scope.owner if scope is not None and scope.target is target
                     and not scope.owner.closed else target.root)
            _enqueue(owner, state)
            if owner is target.root:
                _notify(target)
    if error is not None:
        error._camctl_diagnostic_state = state
    return state


def submit_diagnostic(state: DiagnosticState) -> LogReceipt:
    """同步交接本条并取得原写入回执；副本拥有者继续持有原记录。"""
    if state.status is DiagnosticStatus.NEW:
        raise ValueError("诊断尚未准备，不能执行提交")
    receipt = _receipt_for(state)
    if (state.status is not DiagnosticStatus.HELD or state._admitting
            or state._copy_owned or state._submitted):
        return receipt
    target, owner = state._target, state._owner
    assert target is not None and owner is not None
    owner.pending.remove(state)
    state._owner = None
    try:
        _submit_one(state, target)
    finally:
        if state.status is DiagnosticStatus.HELD and not state._submitted:
            retained_owner = owner if not owner.closed else target.root
            _enqueue(retained_owner, state)
            if retained_owner is target.root:
                _notify(target)
    return receipt


async def admit_diagnostic(state: DiagnosticState) -> DiagnosticState:
    """仅交接本条；已有接纳者和实际副本拥有者继续持有原记录。"""
    if state.status is DiagnosticStatus.NEW:
        raise ValueError("诊断尚未准备，不能执行接纳")
    _receipt_for(state)
    first_cancel = None
    while state.status is DiagnosticStatus.HELD:
        if state._admitting or state._copy_owned:
            if state._changed is None:
                state._changed = asyncio.get_running_loop().create_future()
            try:
                # shield 已有 Future，不创建任务；取消不能撤销共有通知。
                await asyncio.shield(state._changed)
            except asyncio.CancelledError as error:
                if first_cancel is None:
                    first_cancel = error
            continue
        target, owner = state._target, state._owner
        assert target is not None and owner is not None
        owner.pending.remove(state)
        state._owner = None
        try:
            await _admit_one(state, target)
        except asyncio.CancelledError as error:
            if first_cancel is None:
                first_cancel = error
        finally:
            if state.status is DiagnosticStatus.HELD:
                retained_owner = owner if not owner.closed else target.root
                _enqueue(retained_owner, state)
                if retained_owner is target.root:
                    _notify(target)
    if first_cancel is not None:
        raise first_cancel
    return state


def diagnostic_snapshot(value, *, structured: bool = True):
    """复用诊断快照规则，保留原错误字节并排除媒体 stdout。"""
    return _document(value, structured=structured)


def _stderr(message: str) -> None:
    sys.stderr.write(message.rstrip("\n") + "\n")
    sys.stderr.flush()


def _state_changed(state: DiagnosticState) -> None:
    changed, state._changed = state._changed, None
    if changed is not None and not changed.done():
        changed.set_result(None)


def _complete(state: DiagnosticState, status: DiagnosticStatus) -> None:
    state.status = status
    _state_changed(state)


def _receipt_for(state: DiagnosticState) -> LogReceipt:
    try:
        state._loop = asyncio.get_running_loop()
    except RuntimeError:
        pass
    if state._receipt is None:
        receipt = state._receipt = LogReceipt()

        def admission_completed(result: AdmissionResult) -> None:
            loop = state._loop
            if loop is None or loop.is_closed():
                # 没有存活的事件循环时也保留原身份终态，不能触碰旧 Future。
                state.status = (DiagnosticStatus.ADMITTED if
                                result.decision.kind is AdmissionKind.ACCEPTED else
                                DiagnosticStatus.FALLBACK)
                state._admitting = False
                return
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if current is loop:
                _admission_completed(state, result)
            else:
                loop.call_soon_threadsafe(_admission_completed, state, result)

        receipt.add_admission_callback(admission_completed)
    return state._receipt


def _admission_completed(state: DiagnosticState, result: AdmissionResult) -> None:
    state._admitting = False
    if state.status is DiagnosticStatus.HELD:
        _complete(state, DiagnosticStatus.ADMITTED if
                  result.decision.kind is AdmissionKind.ACCEPTED else
                  DiagnosticStatus.FALLBACK)


def acknowledge_diagnostic(state: DiagnosticState, completion: LogWriteResult | None = None) -> None:
    """副本拥有者取得实际追加成功凭据后交接原记录，不推断写入结果。"""
    if completion is not None and not completion.appended:
        return
    if state.status is not DiagnosticStatus.NEW:
        receipt = _receipt_for(state)
        assert state._record is not None
        if completion is None:
            # 原无参接口表示调用方已经取得本级别完成凭据。
            completion = LogWriteResult(appended=True, durable=state._record.level is LogLevel.ERROR)
        result = AdmissionResult(
            AdmissionDecision(AdmissionKind.ACCEPTED, state._record.level),
            None, delivered=completion.delivered_for(state._record.level),
            fallback_written=completion.fallback_written,
            appended=completion.appended, durable=completion.durable,
        )
        receipt.complete_admission(result)
        receipt.complete_write(result)
    if state.status is DiagnosticStatus.HELD:
        _complete(state, DiagnosticStatus.ADMITTED)


def acknowledge_diagnostic_record(record: LogRecord, completion: LogWriteResult | None = None) -> None:
    """副本取得实际追加凭据后，结算当前目标持有的同一条原记录。"""
    target = _target.get()
    if target is None:
        return
    scope = _scope.get()
    owners = [target.root]
    if scope is not None and scope.target is target:
        owners.insert(0, scope.owner)
    for owner in owners:
        for state in owner.pending:
            if state._record is record:
                acknowledge_diagnostic(state, completion)
                return
    for reservation in target.reservations:
        if reservation.state._record is record:
            acknowledge_diagnostic(reservation.state, completion)
            return


def reserve_diagnostic(state: DiagnosticState | None) -> DiagnosticReservation | None:
    """从当前拥有者取出尚未开始接纳的记录，交给即将开始的副本责任。"""
    target = _target.get()
    if (state is None or target is None or target.closed or target.finished
            or state._target is not target or state.status is not DiagnosticStatus.HELD
            or state._admitting or state._owner is None):
        return None
    state._owner.pending.remove(state)
    state._owner = None
    state._copy_owned = True
    reservation = DiagnosticReservation(state, target)
    target.reservations.append(reservation)
    return reservation


def adopt_diagnostic(reservation: DiagnosticReservation) -> DiagnosticState:
    """实际副本任务在自己的 scope 内接管原记录；父等待取消不撤回接管。"""
    if reservation._status is _ReservationStatus.ADOPTED:
        return reservation.state
    if reservation._status is _ReservationStatus.RELEASED:
        raise RuntimeError("诊断已经放回命令，副本任务不能再次接管")
    target = reservation._target
    scope = _scope.get()
    if (_target.get() is not target or scope is None or scope.target is not target
            or scope.task is not asyncio.current_task()
            or scope.owner.closed or target.closed or target.finished):
        raise RuntimeError("实际副本任务必须在当前命令自己的 diagnostic_scope 内接管诊断")
    target.reservations.remove(reservation)
    reservation._status = _ReservationStatus.ADOPTED
    _enqueue(scope.owner, reservation.state)
    return reservation.state


def release_diagnostic(reservation: DiagnosticReservation | None) -> None:
    """仅在确认操作不会启动时退回；已接管的责任继续由实际副本任务持有。"""
    if reservation is None or reservation._status is not _ReservationStatus.RESERVED:
        return
    target = reservation._target
    target.reservations.remove(reservation)
    reservation._status = _ReservationStatus.RELEASED
    state = reservation.state
    state._copy_owned = False
    if state.status is not DiagnosticStatus.HELD:
        return
    if target.closed or target.finished:
        _final_fallback(state, target)
    else:
        _enqueue(target.root, state)
        _notify(target)


def record_availability_error(error, *, ticket, diagnostics, context=None, details=None, state=None):
    """交付未确认收场的检查错误及原工具字节，不生成调用结果。"""
    if not isinstance(ticket, AttemptTicket) or ticket.operation != "availability":
        raise ValueError("可用性诊断必须属于原检查票据")
    if not isinstance(diagnostics, ToolDiagnostics):
        raise TypeError("可用性诊断必须使用原工具诊断结构")
    state = _record({"stage": "availability", "error": error, "ticket": ticket,
                     "diagnostics": diagnostics, "context": context, "details": details}, state,
                    non_media_diagnostics=diagnostics)
    if isinstance(error, BaseException) and state.status is not DiagnosticStatus.NEW:
        error._camctl_diagnostic_state = state
    return state


def record_call_outcome(
    outcome: CallOutcome,
    *,
    ticket=None,
    run_error: ErrorValue | BaseException | None = None,
    context: Mapping | None = None,
    state: DiagnosticState | None = None,
    include_tool_stdout: bool = False,
) -> DiagnosticState | None:
    """保留调用错误；调用方须按原驱动契约确认可用性检查输出不是媒体。"""
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
    }, state, non_media_diagnostics=(outcome.diagnostics if include_tool_stdout
                                      and isinstance(ticket, AttemptTicket)
                                      and ticket.operation == "availability" else None))
    if isinstance(run_error, BaseException) and state.status is not DiagnosticStatus.NEW:
        run_error._camctl_diagnostic_state = state
    return state


def _record(payload: Mapping, state: DiagnosticState | None, *,
            non_media_diagnostics: ToolDiagnostics | None = None) -> DiagnosticState:
    state = state if state is not None else DiagnosticState()
    if state.status is not DiagnosticStatus.NEW:
        return state
    target = _target.get()
    if target is None or (target.closed and not target.background):
        return state
    metadata = log_metadata(LogLevel.ERROR)
    try:
        document = _document(payload, non_media_diagnostics=non_media_diagnostics)
    except Exception as error:
        # 编码器拒绝原结果时，仍沿原 dataclass 字段保存安全字节事实。
        try:
            document = {"diagnostic_format_error": _document(error, structured=False),
                        "diagnostic": _document(payload, structured=False,
                                                non_media_diagnostics=non_media_diagnostics)}
        except Exception as fallback_error:
            # 诊断准备本身失效时只保留仍能取得的类型，结束该诊断责任。
            document = {"stage": payload["stage"],
                        "error_type": _type_name(payload["error"]),
                        "diagnostic_format_error": _type_name(error),
                        "diagnostic_fallback_error": _type_name(fallback_error)}
        state._fallback_only = True
    document = {**metadata, **document}
    try:
        message = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    except Exception as error:
        # 格式化失败仍持有已经排除媒体 stdout 的完整事实，不能截断。
        message = f"错误诊断格式化失败：{type(error).__name__}: {error}; diagnostic={document!r}"
        state._fallback_only = True
    state._record = LogRecord(LogLevel.ERROR, message, fallback=target.fallback, structured=True)
    state._target = target
    state.status = DiagnosticStatus.HELD
    _receipt_for(state)
    # 最后的 stderr 交付可以抛出控制异常；原异常在这之前即持有去重身份。
    for original in (payload["error"], payload.get("run_error")):
        if isinstance(original, BaseException):
            original._camctl_diagnostic_state = state
    if target.background and (target.finished or target.closed):
        _final_fallback(state, target)
        return state
    scope = _scope.get()
    owner = (scope.owner if scope is not None and scope.target is target
             and not scope.owner.closed else target.root)
    _enqueue(owner, state)
    if owner is target.root:
        _notify(target)
    return state


def _document(value, *, name: str | None = None, structured: bool = True,
              non_media_diagnostics: ToolDiagnostics | None = None, tool_stdout: bool = False):
    """复用原结构形成独立快照，并给已有 stderr 字节编码补可读文本。"""
    def convert(item, *, name=None):
        return _document(item, name=name, structured=structured,
                         non_media_diagnostics=non_media_diagnostics, tool_stdout=tool_stdout)

    if isinstance(value, BaseException):
        if structured:
            return convert(exception_diagnostic(value))
        # 原异常无法格式化时不再调用 __str__，保留安全取得的原字段。
        result = {"type": _type_name(value), "arguments": convert(value.args),
                  "traceback": [{"filename": frame.f_code.co_filename, "lineno": lineno,
                                 "function": frame.f_code.co_name}
                                for frame, lineno in traceback.walk_tb(value.__traceback__)]}
        if isinstance(value, DiagnosticError):
            result["diagnostic_details"] = convert(value.diagnostic_details)
        if hasattr(value, "__notes__"):
            result["notes"] = convert(value.__notes__)
        return result
    if isinstance(value, ErrorValue):
        if structured:
            return convert(error_document(value))
    if isinstance(value, ToolDiagnostics) and structured:
        result = encode_diagnostics(value)
        for call in result["calls"]:
            stdout = call["stdout"]
            if value is not non_media_diagnostics:
                encoded = stdout.pop("hex")
                stdout["byte_count"] = None if encoded is None else len(encoded) // 2
            stderr = call["stderr"]
            stderr["text"] = (None if stderr["hex"] is None else
                              bytes.fromhex(stderr["hex"]).decode("utf-8", "backslashreplace"))
        return result
    if isinstance(value, ToolDiagnostics):
        tool_stdout = value is non_media_diagnostics
    if isinstance(value, Enum):
        return convert(value.value)
    if isinstance(value, bytes):
        if name == "stdout" and not tool_stdout:
            return {"byte_count": len(value)}
        if name == "stderr":
            return {"hex": value.hex(), "text": value.decode("utf-8", "backslashreplace")}
        return {"bytes_hex": value.hex()}
    if is_dataclass(value) and not isinstance(value, type):
        if name == "stdout" and isinstance(value, ToolStream) and not tool_stdout:
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
            elif isinstance(value, ToolStream) and field_name == "data" and name == "stdout":
                field_name = "stdout"
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


def _type_name(value: object) -> str:
    kind = type(value)
    return f"{kind.__module__}.{kind.__qualname__}"


def _retain_at_root(owner: _Owner, target: _Target) -> None:
    first_control = None
    while owner.pending:
        state = owner.pending[0]
        late = target.background and (target.finished or target.closed)
        if state.status is DiagnosticStatus.HELD:
            if late:
                try:
                    _final_fallback(state, target)
                except BaseException as error:
                    first_control = _first_control(first_control, error)
        _pop_pending(owner)
        if state.status is DiagnosticStatus.HELD and not late:
            _enqueue(target.root, state)
    _notify(target)
    if first_control is not None:
        raise first_control


def _first_control(first: BaseException | None, error: BaseException) -> BaseException:
    if first is None:
        return error
    if first is not error:
        first.add_note(f"诊断后续控制错误: {_type_name(error)}")
    return first


def _enqueue(owner: _Owner, state: DiagnosticState) -> None:
    state._owner = owner
    owner.pending.append(state)
    _state_changed(state)


def _pop_pending(owner: _Owner) -> DiagnosticState:
    state = owner.pending.popleft()
    state._owner = None
    state._copy_owned = False
    return state


def _notify(target: _Target) -> None:
    if target.wake is not None:
        target.wake.set()


async def _serve(target: _Target) -> None:
    assert target.wake is not None
    try:
        while True:
            try:
                if not target.root.pending:
                    if target.stopping:
                        break
                    target.wake.clear()
                    await target.wake.wait()
                await _drain(target.root, target)
            except asyncio.CancelledError as error:
                _remember_serve_error(target, error)
            except BaseException as error:
                _remember_serve_error(target, error)
                break
    finally:
        target.finished = True
        _fallback_remaining(target)


def _fallback_remaining(target: _Target) -> None:
    while target.root.pending:
        state = _pop_pending(target.root)
        if state.status is not DiagnosticStatus.HELD:
            continue
        if state._submitted:
            # 原通道已经持有此条；不能再以 stderr 冒充它的实际写入结果。
            continue
        try:
            _fallback(state, target)
        except BaseException as error:
            _remember_serve_error(target, error)
            # stderr 控制错误由 flush 外传，该诊断不再递归投递。
            _settle_fallback(state, written=False)


async def _finish_background(target: _Target) -> None:
    assert target.task is not None
    target.stopping = True
    _notify(target)
    first_cancel = None
    while True:
        try:
            await asyncio.shield(target.task)
            break
        except asyncio.CancelledError as error:
            if target.task.cancelled():
                target.finished = True
                _remember_serve_error(target, error)
                _fallback_remaining(target)
                break
            if first_cancel is None:
                first_cancel = error
    serve_error = None if target.error_reported else target.serve_error
    target.error_reported = True
    target.flushed = True
    if first_cancel is not None:
        if serve_error is not None:
            first_cancel.add_note(f"诊断任务控制错误: {_type_name(serve_error)}")
        raise first_cancel
    if serve_error is not None:
        raise serve_error


def _remember_serve_error(target: _Target, error: BaseException) -> None:
    if target.serve_error is None:
        target.serve_error = error
    elif target.serve_error is not error:
        target.serve_error.add_note(f"诊断任务后续控制错误: {_type_name(error)}")


async def _drain(owner: _Owner, target: _Target) -> None:
    while owner.pending:
        state = owner.pending[0]
        if state.status is not DiagnosticStatus.HELD:
            _pop_pending(owner)
            continue
        try:
            await _admit_one(state, target)
        except BaseException:
            if state.status is not DiagnosticStatus.HELD:
                _pop_pending(owner)
            raise
        _pop_pending(owner)


async def _admit_one(state: DiagnosticState, target: _Target) -> None:
    receipt = _receipt_for(state)
    try:
        if not state._submitted:
            _submit_one(state, target)
        first_cancel = None
        while True:
            try:
                result = await receipt.wait_admission()
                break
            except asyncio.CancelledError as error:
                # 旧诊断交接仍保护到接纳完成；通用回执的观察者可独立取消。
                if first_cancel is None:
                    first_cancel = error
        if first_cancel is not None:
            raise first_cancel
    except BaseException as error:
        result = receipt.admission_result or admission_result_for(error)
        if result is not None:
            receipt.complete_admission(result)
            _admission_completed(state, result)
        elif not isinstance(error, asyncio.CancelledError) or not state._submitted:
            state._submitted = False
            state._admitting = False
        raise
    else:
        _admission_completed(state, result)
    finally:
        _state_changed(state)


def _submit_one(state: DiagnosticState, target: _Target) -> None:
    record = state._record
    assert record is not None
    receipt = _receipt_for(state)
    state._submitted = True
    state._admitting = True
    try:
        if target.closed or target.finished:
            state._submitted = False
            state._admitting = False
            _final_fallback(state, target)
            return
        if target.channel is None or state._fallback_only:
            state._submitted = False
            state._admitting = False
            _fallback(state, target)
            return
        try:
            submitted = target.channel.submit(record, receipt=receipt)
            if submitted is not receipt:
                raise RuntimeError("日志通道必须保留传入的原诊断回执")
        except Exception:
            _fallback(state, target)
        except BaseException as error:
            result = receipt.admission_result or admission_result_for(error)
            if result is not None:
                receipt.complete_admission(result)
                _admission_completed(state, result)
            else:
                state._submitted = False
                state._admitting = False
            raise
        else:
            if receipt.admission_result is not None:
                _admission_completed(state, receipt.admission_result)
    finally:
        _state_changed(state)


def _fallback(state: DiagnosticState, target: _Target) -> None:
    assert state._record is not None
    written = False
    try:
        written = target.fallback(state._record.message) is not False
    except Exception:
        # stderr 本身已经失效，不递归诊断或反复写入，也不影响业务结果。
        pass
    _settle_fallback(state, written=written)


def _settle_fallback(state: DiagnosticState, *, written: bool) -> None:
    assert state._record is not None
    receipt = _receipt_for(state)
    result = AdmissionResult(
        AdmissionDecision(AdmissionKind.FALLBACK_STDERR, state._record.level),
        None, delivered=False, fallback_written=written,
    )
    receipt.complete_admission(result)
    receipt.complete_write(result)
    _complete(state, DiagnosticStatus.FALLBACK)


def _final_fallback(state: DiagnosticState, target: _Target) -> None:
    """目标已结束时完成最后一次交付；控制异常仍传播，不留下孤立责任。"""
    try:
        _fallback(state, target)
    finally:
        if state.status is DiagnosticStatus.HELD:
            _settle_fallback(state, written=False)
