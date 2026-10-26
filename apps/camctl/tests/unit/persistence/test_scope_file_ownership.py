"""范围页只以原差集固定文件来源，不要求元数据已知。"""
import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.files import OwnershipFromResultPageSave, file_identity_key
from camctl.capture.result_inputs import observed_file
from camctl.capture.result_pages import ResultPageRef
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.operations.models import AttemptTicket
from camctl.persistence.repositories import result_file_ownership as ownership, result_pages


@pytest.mark.parametrize('policy,identity,expected,existing', [
    ('osmo_empty_tail_classification_v1', '/DCIM/CAM_001/a.OSV', 'ORIGINAL', None),
    ('osmo_empty_tail_classification_v1', '/DCIM/CAM_001/a.LRF', 'PREVIEW', None),
    ('action6_video_segments_v1', '/DCIM/DJI_001/a.MP4', 'ORIGINAL', None),
    ('action6_video_segments_v1', '/DCIM/DJI_001/a.LRF', 'PREVIEW', None),
    (None, '/DCIM/a.unknown', 'UNDETERMINED', None),
    (None, '/DCIM/a.unknown', 'ORIGINAL', 'ORIGINAL'),
    (None, '/DCIM/a.unknown', 'PREVIEW', 'PREVIEW'),
])
def test_scope_ownership_does_not_require_file_metadata(monkeypatch, policy, identity, expected, existing):
    connection = create_autospec(sqlite3.Connection, instance=True)
    ticket = AttemptTicket(1, 'result', '8', 'scope/8', 5)
    ref = ResultPageRef(ticket, 1, 10, None)
    entry = observed_file(dict(identity=identity, locator={'path': identity}, complete=False,
        size_bytes=None, format_id=None))
    saved = SimpleNamespace(ref=ref, file_ids=((identity, 4),), page=SimpleNamespace(entries=(entry,)))
    run = dict(id=5, kind=int(enum_for('operation_runs.kind').RESOLVE_OUTPUT_SCOPE))
    action = dict(id=3, device_id='camera', driver_id='driver',
        execution_spec_json={} if policy is None else {'result_policy': policy})
    activity = dict(id=8)
    attempt = dict(id=9, result_first_page_event_id=10, result_last_page_event_id=10)
    file = dict(identity_key=file_identity_key('camera', 'driver', identity))
    if existing is not None:
        file.update(source_action_id=3, ownership_evidence_json={'saved': True},
            role=int(enum_for('device_files.role')[existing]))
    monkeypatch.setattr(result_pages, '_at_record', create_autospec(result_pages._at_record,
        return_value=(saved, {}, object(), object())))
    monkeypatch.setattr(result_pages, '_load', create_autospec(result_pages._load,
        return_value=(run, attempt, action, activity)))
    monkeypatch.setattr(result_pages, 'committed_page_proof', create_autospec(result_pages.committed_page_proof,
        return_value=(action, activity, attempt, run)))
    monkeypatch.setattr(ownership, 'row_facts', create_autospec(ownership.row_facts, return_value=file))

    source, role, _ = ownership._load_proof(connection, OwnershipFromResultPageSave(ref, 4, 20))

    assert source == 3 and role == int(enum_for('device_files.role')[expected])


@pytest.mark.parametrize('responsibility,accepted', [('results/8', True), ('scope/8', False)])
def test_scope_requires_ownership_for_optional_preview(monkeypatch, responsibility, accepted):
    action = {'id': 3, 'execution_spec_json': {'product_rules': []}}
    ticket = AttemptTicket(1, 'result', '8', responsibility, 5)
    entry = observed_file(dict(identity='/DCIM/a.LRF', locator={'path': '/DCIM/a.LRF'},
        complete=False, size_bytes=None, kind='video', format_id='lrf', paired_identity='/DCIM/a.OSV'))
    facts = {'source_action_id': None, 'ownership_evidence_json': None}
    monkeypatch.setattr(result_pages, 'iter_result_file_facts', create_autospec(
        result_pages.iter_result_file_facts, return_value=iter(((entry, facts),))))
    monkeypatch.setattr('camctl.capture.results.is_optional_preview', lambda *args: True)
    if accepted:
        result_pages.verify_output_scope_ownership(object(), action, ticket, 10)
    else:
        with pytest.raises(ConsistencyError):
            result_pages.verify_output_scope_ownership(object(), action, ticket, 10)
