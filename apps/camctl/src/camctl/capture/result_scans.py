"""同一 RESULTS 轮次的有界分页拥有者。"""
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping

from camctl.capture.result_inputs import ResultPage, page_from_outcome
from camctl.capture.result_pages import ResultPageRef, ResultPageSave, ResultPageSaveOwner
from camctl.contracts.values import ConsistencyError
from camctl.devices.directory import DirectoryCursor
from camctl.operations.models import AttemptTicket, ValidatedOutcome
from camctl.operations.owned_calls import check_call_interruption
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind


@dataclass
class PendingResultScan:
    """只持有本次尚未交接的页；可靠页输入留在原历史范围。"""

    ticket: AttemptTicket
    output_scope: Mapping | None
    timeout_s: Decimal
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
    finished: bool = False

    def __post_init__(self):
        if not isinstance(self.timeout_s, Decimal) or not self.timeout_s.is_finite() or self.timeout_s <= 0:
            raise ValueError("结果扫描必须使用本尝试已采用的有限正时限")
        self.output_scope = deepcopy(self.output_scope)


def resume_result_page_saves(owned, *, pending_scans, repository):
    """只核实原页；设备绑定、时钟和新的业务资格不参与原申请。"""
    for pending in pending_scans.values():
        if pending.saves.pending is None:
            continue
        receipt = pending.saves.save(repository, owned)
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError(f"原结果页保存尚未可靠完成（{receipt.kind.value}）: {receipt.error}")


async def advance_result_scan(pending: PendingResultScan, *, runtime) -> bool:
    """可靠交接后继续原轮次；实际错误或中断不产生隐式重读。"""
    if pending.finished:
        return True
    if pending.interrupted:
        raise ConsistencyError("原结果扫描已中断，须沿原尝试恢复与新轮次规则收场")
    if pending.in_call:
        return False
    while True:
        if pending.saves.pending is not None:
            original = pending.saves.pending.request
            receipt = pending.saves.save(runtime.capture, runtime.owned)
            if receipt.kind is not DbOutcomeKind.COMPLETED:
                raise ConsistencyError(f"原结果页保存未完成（{receipt.kind.value}）: {receipt.error}")
            page = page_from_outcome(original.ticket, original.outcome.outcome, cursor=original.cursor)
            pending.last_ref = pending.saves.take()
            pending.last_outcome = original.outcome
            pending.last_cursor = original.cursor
            pending.occurred_at = original.occurred_at
            pending.cursor = page.next_cursor
            pending.finished = page.next_cursor is None or page.outcome.error is not None
            if pending.finished:
                return True
            pending.page_no += 1
        check_call_interruption()
        pending.in_call = True
        try:
            page = await runtime.results.list_page(pending.ticket, cursor=pending.cursor,
                timeout_s=pending.timeout_s, output_scope=pending.output_scope)
            # 实际返回立即成为原申请；保存未知不再取时钟或执行列举。
            occurred_at, returned_ns = runtime.wall_us(), runtime.monotonic_ns()
            if not isinstance(page, ResultPage):
                raise ConsistencyError("分页端口未返回完整实际 ResultPage")
            validated = validate_outcome(pending.ticket, page.outcome, runtime.evidence)
            request = ResultPageSave(pending.ticket, pending.page_no, pending.cursor, validated, occurred_at)
            pending.saves.begin(request)
            pending.returned_ns = returned_ns
        except BaseException:
            # 未取得可保存的实际结果时保留原责任，下一推进不能重新列举。
            pending.interrupted = True
            raise
        finally:
            pending.in_call = False


class SavedResultEntries:
    """沿可靠范围重复按页读取原输入，只保留一页及计数。"""

    def __init__(self, repository, owned, last_ref: ResultPageRef):
        self.repository, self.owned, self.last_ref = repository, owned, last_ref
        self._count = None

    def pages(self):
        cursor = None
        while True:
            batch = self.repository.read_result_pages(self.last_ref.ticket, cursor, 1, self.owned)
            for saved in batch.items:
                if saved.ref.event_id > self.last_ref.event_id:
                    raise ConsistencyError("结果输入超出原可靠页范围")
                yield saved
            if batch.next_cursor is None:
                if not batch.items or batch.items[-1].ref != self.last_ref:
                    raise ConsistencyError("结果输入缺少原可靠末页")
                return
            cursor = batch.next_cursor

    @property
    def last_page(self):
        return self.repository.read_result_page(self.last_ref, self.owned).page

    @property
    def completion_page(self):
        """完成观察可以来自任一可靠页，不要求末页重复提供。"""
        return next((saved for saved in self.pages()
                     if saved.page.completion_evidence is not None), None)

    def __iter__(self):
        for saved in self.pages():
            identities = {identity for identity, _ in saved.file_ids}
            yield from (entry for entry in saved.page.entries if entry.identity in identities)

    def __len__(self):
        if self._count is None:
            self._count = sum(len(saved.file_ids) for saved in self.pages())
        return self._count

    def find(self, identity, *, allow_missing=False):
        return self.repository.read_result_file_input(self.last_ref, identity, self.owned,
                                                    allow_missing=allow_missing)


class SourceResultEntries:
    """按物理文件身份分页消费全部原轮次已经可靠确认的来源事实。"""

    def __init__(self, repository, owned, ticket):
        self.repository, self.owned, self.ticket = repository, owned, ticket

    def records(self):
        cursor = None
        while True:
            page = self.repository.read_result_sources(self.ticket, cursor, 32, self.owned)
            yield from page.items
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    def __iter__(self):
        return (entry for entry, _, _ in self.records())


class RegisteredSourceFiles:
    def __init__(self, entries: SourceResultEntries):
        self.entries = entries

    def __iter__(self):
        return ((file, file_id) for _, file, file_id in self.entries.records())
