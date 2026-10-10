"""同一 RESULTS 尝试中逐页原保存请求与可靠历史引用。"""
from copy import deepcopy
from dataclasses import dataclass, replace

from camctl.capture.result_inputs import ResultPage, page_from_outcome
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey, UtcMicros, new_operation_key
from camctl.devices.directory import DirectoryCursor
from camctl.operations.models import AttemptTicket, ValidatedOutcome
from camctl.operations.result_format import error_document, result_document
from camctl.persistence.models import DbOutcome, DbOutcomeKind


@dataclass(frozen=True)
class ResultPageSave:
    ticket: AttemptTicket
    page_no: int
    cursor: DirectoryCursor | None
    outcome: ValidatedOutcome
    occurred_at: int

    def __post_init__(self):
        ObjectId(self.ticket.run_id)
        ObjectId(self.ticket.attempt_id)
        ObjectId(self.page_no)
        UtcMicros(self.occurred_at)
        if not isinstance(self.outcome, ValidatedOutcome) or self.outcome.ticket != self.ticket:
            raise ValueError("结果页必须沿原票据保持已验证的完整实际结果")
        page_from_outcome(self.ticket, self.outcome.outcome, cursor=self.cursor)


@dataclass(frozen=True)
class ResultPageRef:
    ticket: AttemptTicket
    page_no: int
    event_id: int
    next_cursor: DirectoryCursor | None


@dataclass(frozen=True)
class OutputSetFinalizationSave:
    """原 RESULTS 已保存后，独立交接其可靠末页的文件范围事实。"""

    action_id: int
    page: ResultPageRef
    occurred_at: int

    def __post_init__(self):
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)
        if not isinstance(self.page, ResultPageRef):
            raise TypeError("独立集合事实要求原完整末页引用")


@dataclass(frozen=True)
class SavedResultPage:
    ref: ResultPageRef
    page: ResultPage
    occurred_at: int
    file_ids: tuple[tuple[str, int], ...] = ()


def result_page_input(request: ResultPageSave) -> dict:
    """原完整输入的登记编码；JSON 布尔值与数字不能互相替换。"""
    actual = request.outcome.outcome
    return {"run_id": request.ticket.run_id, "attempt_no": request.ticket.attempt_id,
            "activity_id": int(request.ticket.target_id), "page_no": request.page_no,
            "cursor": None if request.cursor is None else request.cursor.as_json(),
            "outcome": {"status": int(enum_for("operation_attempts.status")[actual.status.name]),
                        "effect_state": int(enum_for("operation_attempts.effect_state")[actual.effect.name]),
                        "error": error_document(actual.error), "result": result_document(actual)}}


@dataclass(frozen=True)
class PendingResultPageSave:
    request: ResultPageSave
    key: OperationKey
    response: ResultPageRef | None = None


class ResultPageSaveOwner:
    """每次持有一页；完整实际输入、key 与时刻在可靠交接前保持。"""

    def __init__(self):
        self.pending: PendingResultPageSave | None = None

    def begin(self, request: ResultPageSave, *, key: OperationKey | None = None):
        if not isinstance(request, ResultPageSave):
            raise TypeError("结果页拥有者要求完整原保存申请")
        if self.pending is not None:
            original = self.pending.request
            if (original.ticket != request.ticket or original.occurred_at != request.occurred_at
                    or not json_equal(result_page_input(original), result_page_input(request))
                    or key is not None and key != self.pending.key):
                raise ConsistencyError("原结果页未交接，完整输入、key 和时刻不能替换")
        else:
            self.pending = PendingResultPageSave(deepcopy(request), key if key is not None else new_operation_key())
        return self.pending

    def save(self, repository, owned) -> DbOutcome[ResultPageRef]:
        pending = self.pending
        if pending is None:
            raise ConsistencyError("没有原结果页保存责任")
        if pending.response is not None:
            return DbOutcome(DbOutcomeKind.COMPLETED, pending.response)
        receipt = repository.save_result_page(pending.request, pending.key, owned)
        if receipt.kind is DbOutcomeKind.COMPLETED:
            response = receipt.value
            page = page_from_outcome(pending.request.ticket, pending.request.outcome.outcome, cursor=pending.request.cursor)
            if (not isinstance(response, ResultPageRef) or response.ticket != pending.request.ticket
                    or response.page_no != pending.request.page_no or response.next_cursor != page.next_cursor):
                raise ConsistencyError("结果页可靠回执不是原尝试、原批次或原后续游标")
            try:
                ObjectId(response.event_id)
            except ValueError as error:
                raise ConsistencyError("结果页回执缺少实际历史身份") from error
            self.pending = replace(pending, response=response)
        return receipt

    def take(self) -> ResultPageRef:
        if self.pending is None or self.pending.response is None:
            raise ConsistencyError("原页尚未可靠交接，不能读取下一页")
        response = self.pending.response
        self.pending = None
        return response
