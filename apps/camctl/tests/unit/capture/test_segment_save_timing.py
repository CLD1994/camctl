"""分段原保存及恢复将同一动作、方法和保存键交给事务计时。"""

import json
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.segment_completion import PendingSegmentWrite, _save, resume_segment_writes
from camctl.contracts.values import new_operation_key
from camctl.logging_runtime.performance import performance_logging
from camctl.logging_runtime.service import LogChannel
from camctl.logging_runtime.transaction_timing import trace_transaction
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.transaction import WriteReceipt


@pytest.mark.parametrize("resumed", [False, True])
def test_original_segment_save_fields_reach_transaction_summary(resumed):
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    repository = create_autospec(CaptureRepository, instance=True, spec_set=True)
    owned = SimpleNamespace(connection=SimpleNamespace(in_transaction=False))
    request, seen = object(), []
    @trace_transaction
    def transaction(command, operation_key, actual_owned):
        seen.append((command, operation_key, actual_owned))
        return WriteReceipt(kind="completed", result="saved")
    def save(command, operation_key, actual_owned):
        receipt = transaction(command, operation_key, actual_owned)
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
    repository.finish_segment_recording.side_effect = save
    key = new_operation_key()
    pending = ({15: PendingSegmentWrite(repository, "finish_segment_recording", (request,), key)}
               if resumed else {})
    with performance_logging(channel):
        if resumed:
            resume_segment_writes(owned, pending, 15)
        else:
            runtime = SimpleNamespace(owned=owned, pending_segment_writes=pending)
            assert _save(runtime, 15, repository, "finish_segment_recording", request) == "saved"
    assert not pending
    assert len(seen) == 1 and seen[0][0] is request and seen[0][2] is owned
    if resumed:
        assert seen[0][1] == key
    records = [json.loads(call.args[0].message.removeprefix("性能计时 "))
               for call in channel.slog.call_args_list]
    summary, = [record for record in records if record["stage"] == "db_transaction"]
    assert (summary["action_id"], summary["method"], summary["resumed"]) == (
        15, "finish_segment_recording", resumed)
    assert summary["operation_key"] == str(seen[0][1])
