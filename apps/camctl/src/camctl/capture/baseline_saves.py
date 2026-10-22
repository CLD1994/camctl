"""基准原保存申请的拥有者。"""
from dataclasses import dataclass, replace

from camctl.capture.baseline_models import (BaselineChunkResult, BaselineChunkSave,
    BaselineFixSave, BaselineReadCallView, BaselineReadIntent, BaselineReadReturn,
    BaselineReadState, BaselineReadUnconfirmedSave, BaselineRef, BaselineState)
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey, new_operation_key
from camctl.persistence.models import DbOutcome, DbOutcomeKind


@dataclass(frozen=True)
class PendingBaselineSave:
    request: BaselineChunkSave | BaselineFixSave | BaselineReadIntent | BaselineReadReturn | BaselineReadUnconfirmedSave
    key: OperationKey
    response: BaselineChunkResult | BaselineRef | BaselineReadCallView | None = None


class BaselineSaveOwner:
    """每次只持有一个完整申请；可靠响应交回前不能替换或清除。"""

    def __init__(self):
        self.pending: PendingBaselineSave | None = None

    def begin(self, request: BaselineChunkSave | BaselineFixSave | BaselineReadIntent | BaselineReadReturn | BaselineReadUnconfirmedSave,
              *, key: OperationKey | None = None) -> PendingBaselineSave:
        if not isinstance(request, (BaselineChunkSave, BaselineFixSave, BaselineReadIntent,
                                    BaselineReadReturn,BaselineReadUnconfirmedSave)):
            raise TypeError("基准拥有者需要完整的原始保存申请")
        if isinstance(request,BaselineReadIntent):
            if key is not None and key != request.call_key:
                raise ConsistencyError('基准意图必须保留原调用键')
            key=request.call_key
        elif isinstance(request,(BaselineReadReturn,BaselineReadUnconfirmedSave)) and key is None:
            raise ConsistencyError('基准返回必须显式使用原结果保存键')
        if self.pending is not None:
            if self.pending.request != request or (key is not None and self.pending.key != key):
                raise ConsistencyError("原基准保存申请仍持有，完整输入及 key 不可替换")
        else:
            self.pending = PendingBaselineSave(request, key if key is not None else new_operation_key())
        return self.pending

    def save(self, repository, owned) -> DbOutcome[BaselineChunkResult | BaselineRef | BaselineReadCallView]:
        pending = self.pending
        if pending is None:
            raise ConsistencyError("没有待保存的原基准申请")
        if pending.response is not None:
            return DbOutcome(DbOutcomeKind.COMPLETED, value=pending.response)
        if isinstance(pending.request,BaselineReadIntent):
            method=repository.save_baseline_read_intent
        elif isinstance(pending.request,BaselineReadReturn):
            method=repository.save_baseline_read_return
        elif isinstance(pending.request,BaselineReadUnconfirmedSave):
            method=repository.save_baseline_read_unconfirmed
        else:
            method = (repository.append_baseline if isinstance(pending.request, BaselineChunkSave)
                      else repository.fix_baseline)
        receipt = method(pending.request, pending.key, owned)
        if receipt.kind is DbOutcomeKind.COMPLETED:
            response = receipt.value
            if isinstance(pending.request,(BaselineReadIntent,BaselineReadReturn,BaselineReadUnconfirmedSave)):
                self._check_call_response(pending.request,response)
                self.pending=replace(pending,response=response)
                return receipt
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

    @staticmethod
    def _check_call_response(request,response):
        if not isinstance(response,BaselineReadCallView):
            raise ConsistencyError('基准调用保存没有交回实际调用事实')
        if isinstance(request,BaselineReadIntent):
            if response.request != request or response.state is not BaselineReadState.INTENDED:
                raise ConsistencyError('基准调用意图回执不属于原完整输入')
        elif isinstance(request,BaselineReadUnconfirmedSave):
            if response.call_id != request.call_id or response.state is not BaselineReadState.UNCONFIRMED:
                raise ConsistencyError('基准返回未知回执不属于原调用')
        elif (response.call_id != request.call_id or response.state is not BaselineReadState.RETURNED
                or response.returned_at != request.returned_at or response.error != request.error
                or response.next_cursor != (None if request.returned.page is None else request.returned.page.next_cursor)):
            raise ConsistencyError('基准实际返回回执不属于原申请')

    def take(self) -> BaselineChunkResult | BaselineRef | BaselineReadCallView:
        if self.pending is None or self.pending.response is None:
            raise ConsistencyError("基准原申请尚未可靠完成，不能推进下一步")
        response = self.pending.response
        self.pending = None
        return response
