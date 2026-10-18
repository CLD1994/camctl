"""活跃成员最近消费身份与默认轮次；不持有运行时或完整结果历史。"""

from dataclasses import dataclass, replace


@dataclass
class CleanupConsumption:
    delete_identity: tuple | None = None
    query_identity: tuple | None = None
    result_identity: tuple | None = None
    round_token: object | None = None


def record_consumption(runtime, pending, *, query=False):
    """只由可靠消费成功点调用；原结果键唯一标识实际持久结果。"""
    record = runtime.consumed_queries.setdefault(pending.item_id, CleanupConsumption())
    record.result_identity = (
        pending.database_id,
        pending.item_id,
        pending.ticket,
        pending.saved_key or pending.key,
    )
    if query:
        record.query_identity = record.result_identity
    else:
        record.delete_identity = record.result_identity
    record.round_token = runtime.round_token


def handed_off_this_round(runtime, item_id):
    record = runtime.consumed_queries.get(item_id)
    return (
        runtime.round_token is not None
        and record is not None
        and record.round_token is runtime.round_token
        and record.result_identity is not None
        and record.result_identity[:2] == (runtime.owned.metadata.instance_id, item_id)
    )


def operation_runtime(runtime, item_id, operation):
    """派生运行时保持同一轮次及同一会话消费事实。"""
    if runtime.for_item is None:
        return runtime
    if runtime.for_operation is not None:
        derived = runtime.for_operation(item_id, operation)
    else:
        derived = runtime.for_item(item_id)
    return replace(
        derived,
        round_token=runtime.round_token,
        consumed_queries=runtime.consumed_queries,
        allow_new_calls=runtime.allow_new_calls,
    )


def operation_supported(runtime, item_id, operation):
    if runtime.binding_of(item_id) is None:
        return True
    supported = (
        runtime.delete_supported if operation == "delete" else runtime.query_supported
    )
    if supported is None and runtime.driver is None:
        from camctl.contracts.values import ConsistencyError

        raise ConsistencyError("当前文件操作协作者尚未可靠解析")
    return supported is not False
