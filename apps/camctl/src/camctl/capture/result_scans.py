"""同一 RESULTS 轮次的有界分页拥有者。"""
import asyncio
from copy import deepcopy
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Awaitable, Callable, Mapping
from time import monotonic_ns

from camctl.capture.result_inputs import ResultPage, page_from_outcome
from camctl.capture.results import FileKind
from camctl.capture.result_pages import ResultPageRef, ResultPageSave, ResultPageSaveOwner, SavedResultPage
from camctl.capture.result_reading import ResultReadingContext
from camctl.contracts.values import ConsistencyError
from camctl.devices.directory import DirectoryCursor
from camctl.devices.capture_completion import is_stop_context, result_page_timeout_s
from camctl.devices.recording_completion import completed_file_wait_source
from camctl.operations.models import AttemptTicket, ValidatedOutcome
from camctl.operations.owned_calls import check_call_interruption
from camctl.operations.validation import validate_outcome
from camctl.operations.tool_diagnostics import DiagnosticsValidationError
from camctl.persistence.business import settle_required
from camctl.persistence.models import db_failure, DbOutcomeKind, db_outcome_for
from camctl.logging_runtime.performance import measure_performance
from camctl.logging_runtime.events import log_context, record_event
from camctl.logging_runtime.diagnostics import DiagnosticState, record_call_outcome, record_error


@dataclass
class PendingResultScan:
    """只持有本次尚未交接的页；可靠页输入留在原历史范围。"""

    ticket: AttemptTicket
    output_scope: Mapping | None
    timeout_s: Decimal
    completion_context: Mapping | None = None
    video_result_mode: str | None = None
    page_no: int = 1
    cursor: DirectoryCursor | None = None
    saves: ResultPageSaveOwner = field(default_factory=ResultPageSaveOwner)
    last_ref: ResultPageRef | None = None
    last_outcome: ValidatedOutcome | None = None
    last_cursor: DirectoryCursor | None = None
    occurred_at: int | None = None
    returned_ns: int | None = None
    in_call: bool = False
    interrupted: bool = False
    #: 本轮主机不再读取；实际扫描是否完整仍由可靠末页单独表达。
    finished: bool = False
    diagnostic: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)
    validation_diagnostic: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)
    scope_only: bool = False
    action_id: int | None = None
    device_id: str | None = None
    round_began_ns: int | None = field(default=None, init=False, repr=False)
    round_ended: bool = field(default=False, init=False, repr=False)
    reading_context: ResultReadingContext = field(init=False)

    def __post_init__(self):
        if not isinstance(self.timeout_s, Decimal) or not self.timeout_s.is_finite() or self.timeout_s <= 0:
            raise ValueError("结果扫描必须使用本尝试已采用的有限正时限")
        self.output_scope = deepcopy(self.output_scope)
        self.completion_context = deepcopy(self.completion_context)
        self.reading_context = ResultReadingContext(self.ticket)

    def close(self):
        self.reading_context.close()

    def log_fields(self):
        fields = {"activity_id": self.ticket.target_id, "run_id": self.ticket.run_id,
            "attempt_id": self.ticket.attempt_id, "scope_only": self.scope_only}
        if self.action_id is not None:
            fields["action_id"] = self.action_id
        if self.device_id is not None:
            fields["device_id"] = self.device_id
        return fields

    def log_return(self, event, **fields):
        if self.round_began_ns is not None and not self.round_ended:
            self.round_ended = True
            record_event("capture.result_scan", event=event,
                elapsed_ms=round((monotonic_ns() - self.round_began_ns) / 1_000_000, 3),
                **self.log_fields(), **fields)


async def resume_result_page_saves(database, *, pending_scans, repository):
    """只核实原页；设备绑定、时钟和新的业务资格不参与原申请。"""
    for pending in tuple(pending_scans.values()):
        if pending.saves.pending is None:
            continue
        with measure_performance("result_page_save", run_id=pending.ticket.run_id,
                attempt_no=pending.ticket.attempt_id, page_no=pending.saves.pending.request.page_no):
            receipt = (await pending.saves.resume_save(repository, database))
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            raise db_failure(ConsistencyError, f"原结果页保存尚未可靠完成（{receipt.kind.value}）: {receipt.error}", receipt)


async def advance_result_scan(pending: PendingResultScan, *, runtime,
        can_read: Callable[[], Awaitable[bool]] | None = None,
        page_qualified: Callable[[SavedResultPage], Awaitable[bool]] | None = None) -> bool:
    """可靠交接后继续原轮次；实际错误或中断不产生隐式重读。"""
    try:
        return await _advance_result_scan(pending, runtime=runtime,
            can_read=can_read, page_qualified=page_qualified)
    except asyncio.CancelledError as error:
        pending.log_return("cancelled", exception_type=type(error).__name__, page_no=pending.page_no)
        raise


async def _advance_result_scan(pending, *, runtime, can_read, page_qualified):
    if pending.finished:
        pending.close()
        return True
    if pending.interrupted:
        raise ConsistencyError("原结果扫描已中断，须沿原尝试恢复与新轮次规则收场")
    if pending.in_call:
        return False
    while True:
        if pending.saves.pending is not None:
            if await settle_required(_accept_saved_page(pending, runtime, page_qualified)):
                return True
        if can_read is not None and not await can_read():
            return False
        check_call_interruption()
        pending.in_call = True
        try:
            if pending.round_began_ns is None:
                pending.round_began_ns = monotonic_ns()
                record_event("capture.result_scan", event="started", **pending.log_fields())
            page_kwargs = {
                "cursor": pending.cursor,
                "timeout_s": result_page_timeout_s(pending.timeout_s, pending.completion_context),
                "output_scope": pending.output_scope,
                "completion_context": deepcopy(pending.completion_context),
                "reading_context": pending.reading_context,
            }
            if pending.video_result_mode is not None:
                page_kwargs["video_result_mode"] = pending.video_result_mode
            if pending.scope_only:
                page_kwargs["scope_only"] = True
            with log_context(**pending.log_fields()):
                page = await runtime.results.list_page(pending.ticket, **page_kwargs)
                # 实际返回立即成为原申请；保存未知不再取时钟或执行列举。
                occurred_at, returned_ns = runtime.wall_us(), runtime.monotonic_ns()
            if not isinstance(page, ResultPage):
                raise ConsistencyError("分页端口未返回完整实际 ResultPage")
            record_call_outcome(page.outcome, ticket=pending.ticket,
                context={"stage": "capture.result_page", "page_no": pending.page_no},
                state=pending.diagnostic)
            try:
                validated = validate_outcome(pending.ticket, page.outcome, runtime.evidence)
            except DiagnosticsValidationError as error:
                record_error(error, stage="capture.result_page_validation",
                    context={"ticket": pending.ticket, "page_no": pending.page_no},
                    details={"outcome": page.outcome}, state=pending.validation_diagnostic)
                raise
            request = ResultPageSave(pending.ticket, pending.page_no, pending.cursor, validated, occurred_at)
            pending.saves.begin(request)
            pending.returned_ns = returned_ns
        except BaseException as error:
            # 未取得可保存的实际结果时保留原责任，下一推进不能重新列举。
            pending.interrupted = True
            pending.log_return("cancelled" if isinstance(error, asyncio.CancelledError)
                else "raised", exception_type=type(error).__name__, page_no=pending.page_no)
            try:
                pending.close()
            except OSError as close_error:
                raise error from close_error
            raise
        finally:
            pending.in_call = False


async def _accept_saved_page(pending, runtime, page_qualified):
    held = pending.saves.pending
    held.application.retry_failed()
    return await held.application.run(lambda: _apply_saved_page(pending, runtime, page_qualified, held))


async def _apply_saved_page(pending, runtime, page_qualified, held):
    original = held.request
    record_call_outcome(original.outcome.outcome, ticket=original.ticket,
        context={"stage": "capture.result_page", "page_no": original.page_no},
        state=pending.diagnostic)
    delivery_error = None
    try:
        with measure_performance("result_page_save", run_id=pending.ticket.run_id,
                attempt_no=pending.ticket.attempt_id, page_no=original.page_no):
            receipt = await pending.saves.resume_save(runtime.capture, runtime.database)
    except BaseException as error:
        receipt = db_outcome_for(error)
        if receipt is None or receipt.kind is not DbOutcomeKind.COMPLETED:
            raise
        delivery_error = error
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(ConsistencyError, f"原结果页保存未完成（{receipt.kind.value}）: {receipt.error}", receipt)
    page = page_from_outcome(original.ticket, original.outcome.outcome, cursor=original.cursor)
    # 所有读取完成后一次交接该页；读取失败仍保留原response供下次恢复。
    saved = SavedResultPage(pending.saves.pending.response, page, original.occurred_at)
    await _retain_completed_wait(pending, saved, runtime=runtime)
    qualified = page_qualified is None or await page_qualified(saved)
    pending.last_ref = pending.saves.take()
    pending.last_outcome = original.outcome
    pending.last_cursor = original.cursor
    pending.occurred_at = original.occurred_at
    pending.cursor = page.next_cursor
    pending.finished = page.next_cursor is None or page.outcome.error is not None or not qualified
    if pending.finished:
        pending.log_return("returned", page_count=pending.page_no,
            scan_complete=page.scan_complete, qualified=qualified,
            status=page.outcome.status.value,
            error_code=None if page.outcome.error is None else page.outcome.error.code)
        pending.close()
    else:
        pending.page_no += 1
    if delivery_error is not None:
        raise delivery_error
    return pending.finished


def completed_wait_from_page(saved: SavedResultPage, context, *, repository, owned) -> dict | None:
    """可靠复制页仍返回真正执行等待的原页，引用错误停止继续复用。"""
    if not is_stop_context(context) or saved.page.outcome.settlement is None:
        return None
    if saved.ref.ticket.target_id != context["activity_id"]:
        raise ConsistencyError("等待事实的可靠结果页属于其他活动")
    data = saved.page.outcome.settlement.evidence.data
    if isinstance(data, Mapping) and data.get("file_completion") is None and data.get("file_completion_source_page_event_id") is None:
        return None
    from camctl.persistence.repositories.result_sources import ResultSourceKind
    original = repository.read_result_source(saved.ref.ticket, owned)
    if (original.kind is not ResultSourceKind.STOP or original.context is None
            or original.context["stop_result_event_id"] != context["stop_result_event_id"]):
        raise ConsistencyError("声明停止等待的原页没有原意图之前的同一 STOP 来源")
    source_id = data.get("file_completion_source_page_event_id")
    source_page = None
    try:
        if source_id is not None:
            source = repository.read_result_page_at(source_id, owned)
            source_data = (None if source.page.outcome.settlement is None else
                source.page.outcome.settlement.evidence.data)
            source_page = {"event_id": source.ref.event_id,
                "activity_id": source.ref.ticket.target_id, "data": source_data}
        actual_source = completed_file_wait_source(data,
            page_event_id=saved.ref.event_id, activity_id=context["activity_id"],
            stop_result_event_id=context["stop_result_event_id"],
            required_wait_ms=context["file_completion_wait_ms"], source_page=source_page)
    except ValueError as error:
        raise ConsistencyError(f"结果页的完成等待来源无效: {error}") from error
    if actual_source is None:
        return None
    if source_page is not None:
        original = repository.read_result_source(source.ref.ticket, owned)
        if (original.kind is not ResultSourceKind.STOP or original.context is None
                or original.context["stop_result_event_id"] != context["stop_result_event_id"]):
            raise ConsistencyError("实际停止等待页没有原意图之前的同一 STOP 来源")
    return {"result_page_event_id": actual_source, "evidence": deepcopy(data["file_completion"])}


async def _retain_completed_wait(pending: PendingResultScan, saved: SavedResultPage, *, runtime) -> None:
    """后续页沿用原实际等待来源，文件集合完成仍独立判定。"""
    completion = await runtime.database.read(lambda owned: completed_wait_from_page(
        saved, pending.completion_context, repository=runtime.capture, owned=owned))
    if completion is None:
        return
    pending.completion_context = {**pending.completion_context, "prior_completion": completion}


class SavedResultEntries:
    """沿可靠范围重复按页读取原输入，只保留一页及计数。"""

    def __init__(self, repository, database, last_ref: ResultPageRef):
        self.repository, self.database, self.last_ref = repository, database, last_ref
        self._count = None

    async def pages(self):
        cursor = None
        while True:
            batch = await self.database.read(lambda owned: self.repository.read_result_pages(
                self.last_ref.ticket, cursor, 1, owned), description="读取一页可靠结果")
            for saved in batch.items:
                if saved.ref.event_id > self.last_ref.event_id:
                    raise ConsistencyError("结果输入超出原可靠页范围")
                yield saved
            if batch.next_cursor is None:
                if not batch.items or batch.items[-1].ref != self.last_ref:
                    raise ConsistencyError("结果输入缺少原可靠末页")
                return
            cursor = batch.next_cursor

    async def last_page(self):
        saved = await self.database.read(lambda owned: self.repository.read_result_page(self.last_ref, owned))
        return saved.page

    async def completion_page(self):
        """完成观察可以来自任一可靠页，不要求末页重复提供。"""
        async for saved in (self.pages()):
            if saved.page.completion_evidence is not None:
                return saved
        return None

    async def __aiter__(self):
        async for saved in self.pages():
            identities = {identity for identity, _ in saved.file_ids}
            for entry in saved.page.entries:
                if entry.identity in identities:
                    yield entry

    async def count(self):
        if self._count is None:
            count = 0
            async for saved in (self.pages()):
                count += len(saved.file_ids)
            self._count = count
        return self._count

    async def find(self, identity, *, allow_missing=False):
        return await self.database.read(lambda owned: self.repository.read_result_file_input(
            self.last_ref, identity, owned, allow_missing=allow_missing))


class QualifiedResultEntries(SavedResultEntries):
    """逐页筛选原时刻；坏页仍由未筛选的原输入保存和核对。"""

    def __init__(self, entries: SavedResultEntries, lower_bound: int, *, source_qualified=True):
        super().__init__(entries.repository, entries.database, entries.last_ref)
        self.lower_bound, self.source_qualified = lower_bound, source_qualified

    async def pages(self):
        async for saved in (super().pages()):
            if self.source_qualified and saved.occurred_at >= self.lower_bound:
                yield saved

    async def find(self, identity, *, allow_missing=False):
        async for saved in (self.pages()):
            identifiers = dict(saved.file_ids)
            if identity in identifiers:
                entry = next(entry for entry in saved.page.entries if entry.identity == identity)
                return entry, identifiers[identity]
        if allow_missing:
            return None
        raise ConsistencyError("配对原片缺少合格原结果页")


class SourceResultEntries:
    """按物理文件身份分页消费全部原轮次已经可靠确认的来源事实。"""

    def __init__(self, repository, database, ticket, *, current_entries=None):
        self.repository, self.database, self.ticket = repository, database, ticket
        self.current_entries = current_entries

    async def records(self):
        cursor = None
        while True:
            page = await self.database.read(lambda owned: self.repository.read_result_sources(
                self.ticket, cursor, 32, owned), description="读取一批已保存文件来源")
            for entry, file, file_id in page.items:
                if self.current_entries is not None:
                    current = await self.current_entries.find(entry.identity, allow_missing=True)
                    if current is None:
                        continue
                    original_pair = entry.paired_identity
                    entry, original_id = current
                    if original_id != file_id:
                        raise ConsistencyError("当前原页改变已登记文件身份")
                    if entry.paired_identity != original_pair:
                        raise ConsistencyError("当前原页改变已登记文件配对")
                    if (entry.paired_identity is not None and await self.current_entries.find(
                            entry.paired_identity, allow_missing=True) is None):
                        continue
                    complete = entry.complete and file.complete and entry.size_bytes == file.size_bytes
                    entry = replace(entry, complete=complete, size_bytes=entry.size_bytes if complete else None)
                    file = replace(file, kind=entry.kind, format_id=entry.format_id,
                        pairing_confirmed=True if entry.paired_identity is not None else None,
                        complete=complete, size_bytes=entry.size_bytes)
                yield entry, file, file_id
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    async def __aiter__(self):
        async for entry, _, _ in self.records():
            yield entry


class ObservedResultEntries:
    """旧 v1 本轮已登记输入；原返回尚未保存时也不读取保留历史。"""

    def __init__(self, ticket, entries, registered):
        self.ticket = ticket
        self.registered = registered
        self.current_entries = tuple(replace(entry, complete=file.complete,
            size_bytes=file.size_bytes) for entry, (file, _) in zip(entries, registered, strict=True))

    async def records(self):
        for entry, (file, file_id) in zip(self.current_entries, self.registered, strict=True):
            yield entry, file, file_id

    async def __aiter__(self):
        for entry in self.current_entries:
            yield entry


class RegisteredSourceFiles:
    def __init__(self, entries: SourceResultEntries | ObservedResultEntries):
        self.entries = entries

    async def __aiter__(self):
        async for _, file, file_id in self.entries.records():
            yield file, file_id


class TerminalResultEntries(SourceResultEntries):
    """终态逐文件合并原保留来源与合格当前观察，不改变原完成事实。"""

    async def records(self):
        retained = SourceResultEntries(self.repository, self.database, self.ticket)
        async for entry, file, file_id in (retained.records()):
            if self.current_entries is not None:
                current = (await self.current_entries.find(entry.identity, allow_missing=True)
                    if isinstance(self.current_entries, SavedResultEntries) else
                    next(((item, file_id) for item in self.current_entries if item.identity == entry.identity), None))
                if current is not None:
                    observed, original_id = current
                    if original_id != file_id:
                        raise ConsistencyError("终态当前观察改变原文件身份")
                    if observed.paired_identity != entry.paired_identity:
                        raise ConsistencyError("终态当前观察改变已登记文件配对")
                    if observed.complete and file.complete and observed.size_bytes == file.size_bytes:
                        # 当前未知的可选元信息不撤销原可靠值；仅用于终态保留，
                        # 当前集合仍使用未补入这些原值的 current 视图。
                        entry = replace(observed,
                            kind=(observed.kind if observed.kind is not FileKind.OTHER
                                or observed.evidence.get('kind') == FileKind.OTHER.value else entry.kind),
                            format_id=observed.format_id if observed.format_id is not None else entry.format_id,
                            original_name=observed.original_name if observed.original_name is not None else entry.original_name,
                            media_type=observed.media_type if observed.media_type is not None else entry.media_type)
                        file = replace(file, kind=entry.kind, format_id=entry.format_id,
                            pairing_confirmed=True if entry.paired_identity is not None else None)
            yield entry, file, file_id


async def iter_result_entries(entries):
    """原实际返回为有限元组，可靠历史按异步页顺序读取。"""
    if hasattr(entries, "__aiter__"):
        async for item in entries:
            yield item
    else:
        for item in entries:
            yield item


async def result_entry_count(entries):
    if isinstance(entries, SavedResultEntries):
        return await entries.count()
    return len(entries)


async def assess_registered_files(registered, requirements, *, set_finalized):
    from camctl.capture.results import CaptureFileAssessment
    state = CaptureFileAssessment(requirements, set_finalized=set_finalized)
    async for file, _ in iter_result_entries(registered):
        state.add(file)
    return state.finish()


async def max_result_value(values, *, default=None):
    result = default
    async for value in values:
        result = value if result is None else max(result, value)
    return result


async def any_result(values):
    async for value in values:
        if value:
            return True
    return False


async def registered_observations(registered, entries):
    if isinstance(registered, RegisteredSourceFiles):
        async for entry, file, file_id in registered.entries.records():
            yield (file, file_id), entry
    else:
        for pair, entry in zip(registered, entries, strict=True):
            yield pair, entry
