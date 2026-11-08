"""读取槽位测试所需的正式设备批次准备。"""

from camctl.contracts.values import new_operation_key
from camctl.outputs.batches import BatchOutcome, BatchRequest
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.file_batches import FileBatchRepository, register_file_batch_guards


def acquire_read_batch(owned, action_id, device_id, occurred_at):
    register_file_batch_guards()
    result = FileBatchRepository().acquire(
        BatchRequest(action_id, device_id, occurred_at), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED and result.value is BatchOutcome.GRANTED, result
