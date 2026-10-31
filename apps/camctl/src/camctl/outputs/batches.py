"""同机文件批次的资格决策；数据库与实际调用由协作者负责。"""

from dataclasses import dataclass
from enum import StrEnum


class BatchOutcome(StrEnum):
    GRANTED = "granted"
    HELD = "held"
    WAIT = "wait"
    FINISHED = "finished"
    RELEASED = "released"


@dataclass(frozen=True)
class BatchRequest:
    action_id: int
    device_id: str
    occurred_at: int

    def __post_init__(self):
        from camctl.contracts.values import ObjectId, UtcMicros
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)
        if not isinstance(self.device_id, str) or not self.device_id:
            raise ValueError("文件批次必须指定原相机")


def decide_batch_grant(*, action_id, holder_action_id, ready, first_action_id):
    """已持有者保持归属；新批次只按当前完整资格竞争。"""
    if holder_action_id is not None:
        return BatchOutcome.HELD if holder_action_id == action_id else BatchOutcome.WAIT
    if ready and first_action_id == action_id:
        return BatchOutcome.GRANTED
    return BatchOutcome.WAIT


def read_member_finished(*, item_terminal, has_copy, verification_complete,
                         read_ended, retry_wait, unsettled):
    """可靠终态或不可再读取的完整副本才能结束该成员的设备阶段。"""
    if unsettled or retry_wait:
        return False
    if item_terminal:
        return True
    return has_copy and verification_complete and read_ended


async def _save_batch(database, request, *, release, pending):
    """保留原申请、时刻和键，直到原提交的确定结果被接手。"""
    from camctl.contracts.values import ConsistencyError, new_operation_key
    from camctl.persistence.models import DbOutcomeKind, db_failure, db_outcome_for
    from camctl.persistence.repositories.file_batches import FileBatchRepository
    identity = (request.action_id, request.device_id, release)
    if identity not in pending:
        pending[identity] = (request, new_operation_key())
    original, key = pending[identity]
    repository = FileBatchRepository()
    save = repository.release if release else repository.acquire
    try:
        outcome = await database.write(
            lambda owned: save(original, key, owned), key=key)
    except BaseException as error:
        actual = db_outcome_for(error)
        if (actual is not None and actual.kind is DbOutcomeKind.COMPLETED
                and isinstance(actual.value, BatchOutcome)):
            del pending[identity]
        raise
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(ConsistencyError, "文件批次保存结果未确认", outcome)
    del pending[identity]
    return outcome.value


async def retry_file_batches(database, pending):
    for (_, _, release), (request, _) in tuple(pending.items()):
        await _save_batch(database, request, release=release, pending=pending)


async def acquire_file_batch(database, request, *, pending):
    """取得原申请的可靠结果；尚未接手时沿用原键和输入。"""
    outcome = await _save_batch(database, request, release=False, pending=pending)
    return outcome in (BatchOutcome.GRANTED, BatchOutcome.HELD)


async def release_finished_batches(database, now, *, pending):
    """持有者独立分页发现，归属不随成员发现页或内存缓存消失。"""
    from camctl.persistence.repositories.file_batch_reads import FileBatchReads
    if not await database.read(lambda owned: FileBatchReads(owned.connection).supported()):
        return
    await retry_file_batches(database, pending)
    after = 0
    while True:
        rows = await database.read(lambda owned: owned.connection.execute(
            "SELECT id,action_id,device_id FROM device_file_batches"
            " WHERE released_event_id IS NULL AND id>? ORDER BY id LIMIT 64", (after,),
        ).fetchall(), description="发现同机文件批次")
        if not rows:
            return
        after = rows[-1][0]
        for _, action_id, device_id in rows:
            await _save_batch(database, BatchRequest(action_id, device_id, now), release=True, pending=pending)
