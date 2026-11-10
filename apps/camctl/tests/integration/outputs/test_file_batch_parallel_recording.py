"""批次取得与已开始录像的设备兼容边界。"""

import pytest

from camctl.contracts.values import new_operation_key
from camctl.outputs.batches import BatchOutcome, BatchRequest
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.file_batches import FileBatchRepository

from .test_qualification import _NOW, _seed_action
from .test_read_associations import _command, read_targets  # noqa: F401
from .test_read_slot import _prepare


@pytest.mark.parametrize("parallel,action_type,activity_state,started,expected", [
    (True, 2, 2, True, BatchOutcome.GRANTED),
    (False, 2, 2, True, BatchOutcome.WAIT),
    (True, 2, 1, False, BatchOutcome.WAIT),
    (True, 2, 1, True, BatchOutcome.WAIT),
    (True, 1, 2, True, BatchOutcome.WAIT),
    (True, 3, 2, True, BatchOutcome.WAIT),
])
def test_read_batch_respects_recording_capability_and_activity(
        read_targets, parallel, action_type, activity_state, started, expected):
    owned = read_targets
    connection = owned.connection
    connection.execute("UPDATE actions SET status=3 WHERE id=11")
    _prepare(owned, _command())
    _seed_action(connection, 13, 1, action_type=action_type)
    connection.execute(
        "INSERT INTO device_activities (id,action_id,task_key,state_query_supported,"
        "stop_supported,safe_repeat_stop,start_return_meaning,completion_mode,"
        "ownership_mode,output_scope_json,baseline_state,dispatch_state,activity_state,"
        "occupancy_state,sent_at,started_at,result_set_state)"
        " VALUES (13,13,?,1,1,1,1,1,1,'{}',1,3,?,1,?,?,1)",
        ("d" * 32, activity_state, _NOW, _NOW if started else None))
    connection.commit()
    result = FileBatchRepository().acquire(
        BatchRequest(31, "cam-1", _NOW + 1, capture_read_parallel_supported=parallel),
        new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value is expected
    count = connection.execute("SELECT COUNT(*) FROM device_file_batches").fetchone()[0]
    assert count == int(expected is BatchOutcome.GRANTED)


@pytest.mark.parametrize("kind,purpose,key", [(2, None, "stop/13"), (6, 3, "query/activity/13/13")])
def test_recording_control_in_flight_blocks_parallel_read_batch(read_targets, kind, purpose, key):
    owned = read_targets
    connection = owned.connection
    connection.execute("UPDATE actions SET status=3 WHERE id=11")
    _prepare(owned, _command())
    _seed_action(connection, 13, 1, action_type=2)
    connection.execute(
        "INSERT INTO device_activities (id,action_id,task_key,state_query_supported,"
        "stop_supported,safe_repeat_stop,start_return_meaning,completion_mode,"
        "ownership_mode,output_scope_json,baseline_state,dispatch_state,activity_state,"
        "occupancy_state,sent_at,started_at,result_set_state)"
        " VALUES (13,13,?,1,1,1,1,1,1,'{}',1,3,2,1,?,?,1)",
        ("d" * 32, _NOW, _NOW))
    connection.execute(
        "INSERT INTO operation_runs (id,action_id,kind,query_purpose,responsibility_key,"
        "activity_id,status,attempts_used,max_attempts_used,timeout_s_json,"
        "retry_interval_s_json,retry_wait_required) VALUES (13,13,?,?,?,13,2,1,3,'1','1',0)",
        (kind, purpose, key))
    connection.execute(
        "INSERT INTO operation_attempts (id,run_id,attempt_no,status,intent_event_id,"
        "max_attempts_used,timeout_s_json,retry_interval_s_json,effect_state)"
        " VALUES (13,13,1,1,1,3,'1','1',1)")
    connection.commit()
    result = FileBatchRepository().acquire(
        BatchRequest(31, "cam-1", _NOW + 1, capture_read_parallel_supported=True),
        new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value is BatchOutcome.WAIT
    assert connection.execute("SELECT COUNT(*) FROM device_file_batches").fetchone() == (0,)
