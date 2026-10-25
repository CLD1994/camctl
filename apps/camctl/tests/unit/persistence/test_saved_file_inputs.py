"""固定文件输入核直接页关系和资格，不重新运行控制及等待算法。"""
import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.files import file_identity_key
from camctl.capture.result_inputs import page_from_outcome
from camctl.capture.result_pages import ResultPageRef, SavedResultPage
from camctl.capture.segment_models import FileCompletionState, OwnershipState
from camctl.capture.segment_storage import document
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary, TransactionRange
from camctl.contracts.values import ConsistencyError
from camctl.devices.evidence import DeviceObservation
from camctl.devices.tasks import CaptureResultPolicy
from camctl.operations.models import AttemptTicket, CallOutcome
from camctl.persistence.fact_reads import EventRecord
from camctl.persistence.repositories import result_file_proofs as files


@pytest.fixture(params=[
    (CaptureResultPolicy.OSMO_EMPTY_TAIL_CLASSIFICATION_V1,
     'dji-osmo360-ii', '/DCIM/CAM_001/CAM_20261025000000_0077_D.OSV', 'osv'),
    (CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1,
     'dji-action6', '/DCIM/DJI_001/DJI_20261025000000_0077_D.MP4', 'mp4'),
], ids=['osmo', 'action6'])
def input_facts(monkeypatch, request):
    db = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    db.in_transaction, db.total_changes = True, 0
    db.execute.side_effect = AssertionError('普通输入不查询控制、等待或基准来源')
    policy, driver_id, identity, format_id = request.param
    ticket = AttemptTicket(1, 'result', '6', 'results/6', 28)
    ref = ResultPageRef(ticket, 1, 872, None)
    page = page_from_outcome(ticket, CallOutcome(observations=(DeviceObservation(
        'result_files_listed', 2, dict(activity_id='6', entries=[dict(identity=identity,
            locator={'path':identity}, complete=True, size_bytes=10, kind='video', format_id=format_id)],
            cursor=None, next_cursor=None, set_finalized=False, completion_evidence=None)),)))
    saved = SavedResultPage(ref, page, 872, ((identity, 30),))
    file = dict(id=30, last_event_id=883, identity_key=file_identity_key('camera',driver_id,identity),
        source_action_id=21, role=int(enum_for('device_files.role').ORIGINAL), size_bytes=10,
        locator_json={'path':identity}, original_device_file_id=None, pairing_evidence_json=None,
        ownership_evidence_json={'method':int(enum_for('device_files.ownership_evidence_json.method').RESULT_PAGE_DIFFERENCE),
                                 'result_page_event_id':872},
        completion_state=int(enum_for('device_files.completion_state').COMPLETE),
        completion_evidence_json={'basis':int(enum_for('device_files.completion_evidence_json.basis').TIME_AND_OUTPUTS),
            'result_page_event_id':872, 'start_result_event_id':859, 'wait_completed_event_id':867,
            'observation':{'saved':'source'}})
    run = dict(id=28, action_id=21, activity_id=6)
    action = dict(id=21, device_id='camera', driver_id=driver_id, execution_spec_json={
        'result_policy':policy.value})
    monkeypatch.setattr(files, 'row_facts', create_autospec(files.row_facts,
        side_effect=lambda connection, table, identity: {'actions':action,'operation_runs':run}[table]))
    monkeypatch.setattr(files.result_pages, 'read_saved_page', create_autospec(files.result_pages.read_saved_page,
        return_value=(saved, {'run_id':28, 'activity_id':6, 'attempt_no':1})))
    monkeypatch.setattr(files, 'read_event', create_autospec(files.read_event,
        return_value=EventRecord((), None, TransactionRange(590,872,874))))
    monkeypatch.setattr(files, 'read_file_at_boundary', create_autospec(files.read_file_at_boundary, return_value=file))
    return SimpleNamespace(db=db,ref=ref,file=file,boundary=HistoryBoundary(600,885))


def test_saved_completion_does_not_reexecute_source_algorithm(input_facts):
    state = input_facts
    result = files.read_result_file_input(state.db,state.ref,30,state.boundary)
    assert result.completion is FileCompletionState.COMPLETE
    assert result.ownership is OwnershipState.OWNED
    assert (result.file_id,result.file_event_id,result.page_event_id) == (30,883,872)


def test_saved_file_input_can_be_encoded_without_changing_reference_types(input_facts):
    state = input_facts
    result = files.read_result_file_input(state.db, state.ref, 30, state.boundary)

    encoded = document(result)

    assert encoded['page_event_id'] == 872
    for name in ('file_id', 'page_event_id', 'file_event_id'):
        assert type(getattr(result, name)) is int
        assert type(encoded[name]) is int


@pytest.mark.parametrize('damage', ['size','locator'])
def test_saved_completion_rejects_direct_file_page_mismatch(input_facts,damage):
    state = input_facts
    if damage == 'size': state.file['size_bytes'] = 11
    else: state.file['locator_json'] = {'path':'/DCIM/other.OSV'}
    with pytest.raises(ConsistencyError):
        files.read_result_file_input(state.db,state.ref,30,state.boundary)


def test_unknown_saved_completion_is_not_promoted(input_facts):
    state = input_facts
    state.file['completion_evidence_json'] = {'basis':1,'observation':{}}
    result = files.read_result_file_input(state.db,state.ref,30,state.boundary)
    assert result.completion is FileCompletionState.UNKNOWN
    assert type(result.page_event_id) is int
    assert document(result)['page_event_id'] == state.ref.event_id
