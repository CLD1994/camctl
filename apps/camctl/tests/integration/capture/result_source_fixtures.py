"""按公开仓储契约准备旧 RESULTS 历史，业务终态仍由正式处理器消费。"""

from dataclasses import dataclass

from camctl.capture.files import FileObservationSave, FilePresenceSave, OwnershipSave, FileCompletionSave
from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import AttemptFinish, AttemptIntent, AttemptTarget, OperationKind
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository

from .result_consumer_fixtures import RESULT_EVIDENCE


@dataclass(frozen=True)
class StoredResult:
    ticket: object
    entries: tuple
    outcome: object
    occurred_at: int


async def save_v1_round(owned, runtime, action_id, port, config, occurred_at, *,
                        result_first=False, before_completion=None):
    """文件完成与原结果可先后提交；所有写入使用实际数据库 worker。"""
    activity_id, = owned.connection.execute(
        'SELECT id FROM device_activities WHERE action_id=?', (action_id,)).fetchone()
    intent = AttemptIntent(operation='result', action_id=action_id,
        kind=OperationKind.CHECK_CAPTURE_RESULTS, target=AttemptTarget(activity_id=activity_id),
        query_purpose=None, config=config, occurred_at=occurred_at)
    intent_key = new_operation_key()
    begun = await runtime.database.write(lambda current: OperationRepository().begin_attempt(
        intent, intent_key, current), key=intent_key)
    assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
    ticket = begun.value.ticket
    assert ticket is not None, begun.value
    listed = await port.list_round(ticket, timeout_s=config.timeout_s)
    finish = AttemptFinish(ticket, validate_outcome(ticket, listed.outcome, RESULT_EVIDENCE),
        occurred_at, retry_wait=True)
    result_key = new_operation_key()
    repository = CaptureRepository()

    async def write(method, command, key=None):
        key = new_operation_key() if key is None else key
        receipt = await runtime.database.write(lambda current: method(command, key, current), key=key)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        return receipt.value

    async def save_result():
        return await write(OperationRepository().finish_attempt, finish, result_key)

    if result_first:
        await save_result()
    for entry in listed.entries:
        observed = await write(repository.save_file_observation, FileObservationSave(action_id,
            entry.identity, entry.locator, occurred_at, entry.original_name, entry.media_type))
        file_id = observed.file_id
        presence = await runtime.database.read(lambda current: current.connection.execute(
            'SELECT presence_state FROM device_files WHERE id=?', (file_id,)).fetchone())
        if presence != (2,):
            await write(repository.save_file_presence, FilePresenceSave(file_id, 2, occurred_at))
        await write(repository.save_file_ownership, OwnershipSave(file_id, action_id, 1, 2,
            entry.evidence, occurred_at))
        known = await runtime.database.read(lambda current: current.connection.execute(
            'SELECT completion_state FROM device_files WHERE id=?', (file_id,)).fetchone())
        if entry.complete and known != (3,):
            command = FileCompletionSave(file_id, 3, occurred_at, basis=1,
                observation=entry.evidence, size_bytes=entry.size_bytes)
            key = new_operation_key()
            if before_completion is not None:
                runtime = await before_completion(runtime, command, key)
            await write(repository.save_file_completion, command, key)
    if not result_first:
        await save_result()
    return runtime, StoredResult(ticket, listed.entries, listed.outcome, occurred_at)


def current_v1_entries(owned, ticket):
    """按已登记尝试的公开结果文档读取本轮条目，保留空结果。"""
    from camctl.capture.result_inputs import files_from_outcome, saved_outcome
    from camctl.contracts.json_values import parse_exact_json
    status, effect, result, error = owned.connection.execute(
        'SELECT status,effect_state,result_json,error_json FROM operation_attempts'
        ' WHERE run_id=? AND attempt_no=?', (ticket.run_id, ticket.attempt_id)).fetchone()
    outcome = saved_outcome(status, effect, parse_exact_json(result),
        None if error is None else parse_exact_json(error))
    return files_from_outcome(ticket, outcome)
