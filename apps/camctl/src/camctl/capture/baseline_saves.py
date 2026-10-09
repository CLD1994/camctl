"""基准原保存申请的拥有者。"""
from dataclasses import dataclass, replace

from camctl.capture.baseline_models import (BaselineChunkResult, BaselineChunkSave,
    BaselineFixSave, BaselineRef, BaselineState)
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey, new_operation_key
from camctl.persistence.models import DbOutcome, DbOutcomeKind


@dataclass(frozen=True)
class PendingBaselineSave:
    request: BaselineChunkSave | BaselineFixSave
    key: OperationKey
    response: BaselineChunkResult | BaselineRef | None = None


class BaselineSaveOwner:
    """每次只持有一个完整申请；可靠响应交回前不能替换或清除。"""

    def __init__(self):
        self.pending: PendingBaselineSave | None = None

    def begin(self, request: BaselineChunkSave | BaselineFixSave, *, key: OperationKey | None = None) -> PendingBaselineSave:
        if not isinstance(request, (BaselineChunkSave, BaselineFixSave)):
            raise TypeError("基准拥有者只接受完整追加或固定申请")
        if self.pending is not None:
            if self.pending.request != request or (key is not None and self.pending.key != key):
                raise ConsistencyError("原基准保存申请仍持有，完整输入及 key 不可替换")
        else:
            self.pending = PendingBaselineSave(request, key if key is not None else new_operation_key())
        return self.pending

    def save(self, repository, owned) -> DbOutcome[BaselineChunkResult | BaselineRef]:
        pending = self.pending
        if pending is None:
            raise ConsistencyError("没有待保存的原基准申请")
        if pending.response is not None:
            return DbOutcome(DbOutcomeKind.COMPLETED, value=pending.response)
        method = (repository.append_baseline if isinstance(pending.request, BaselineChunkSave)
                  else repository.fix_baseline)
        receipt = method(pending.request, pending.key, owned)
        if receipt.kind is DbOutcomeKind.COMPLETED:
            response = receipt.value
            expected = BaselineChunkResult if isinstance(pending.request, BaselineChunkSave) else BaselineRef
            if not isinstance(response, expected) or response.activity_id != pending.request.activity_id:
                raise ConsistencyError("基准保存回执不属于原完整申请")
            if isinstance(response, BaselineChunkResult):
                if response.chunk_no != pending.request.chunk_no:
                    raise ConsistencyError("基准保存回执不是原批次")
                try:
                    ObjectId(response.event_id)
                except ValueError as error:
                    raise ConsistencyError("基准保存回执缺少实际事件身份") from error
            elif (response.state is not BaselineState.FIXED
                    or (response.chunk_count, response.entry_count) !=
                    (pending.request.chunk_count, pending.request.entry_count)):
                raise ConsistencyError("基准保存回执没有可靠固定原计数")
            self.pending = replace(pending, response=response)
        return receipt

    def take(self) -> BaselineChunkResult | BaselineRef:
        if self.pending is None or self.pending.response is None:
            raise ConsistencyError("基准原申请尚未可靠完成，不能推进下一步")
        response = self.pending.response
        self.pending = None
        return response
