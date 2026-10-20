"""START 入口事实与会话许可的有限分区，不使用诊断或采集效果。"""
import pytest
from camctl.contracts.values import ConsistencyError
from camctl.devices.start_entry import (
    EntryPhase,StartEntryCoordinator,entry_from_rows,saved_entry,
)
from camctl.operations.models import AttemptTicket


def ticket(action_id=1):
    return AttemptTicket(1,'control','7',f'start/{action_id}',8)


def result(*,entered=False,prevented=False):
    return {'settlement':{'basis':'not_dispatched' if prevented else 'observed',
        'evidence':{'type':'start_call_prevented' if prevented else 'operation_returned','version':1,'data':{}}},
        'observations':[{'type':'hasselblad_start_entered','version':1,'data':{'activity_id':'7'}}] if entered else []}


@pytest.mark.parametrize('document,phase',[(None,EntryPhase.UNKNOWN),(result(),EntryPhase.UNKNOWN),
    (result(entered=True),EntryPhase.ENTERED),(result(prevented=True),EntryPhase.BLOCKED)])
def test_saved_entry_uses_only_distinct_original_evidence(document,phase):
    assert saved_entry(document,7) is phase


@pytest.mark.parametrize('document',[result(entered=True,prevented=True),
    {**result(entered=True),'observations':result(entered=True)['observations']*2},
    {**result(entered=True),'observations':[{'type':'hasselblad_start_entered','version':1,'data':{'activity_id':'9'}}]}])
def test_contradictory_or_wrong_identity_entry_is_rejected(document):
    with pytest.raises(ConsistencyError):
        saved_entry(document,7)


@pytest.mark.parametrize('document,phase',[(None,EntryPhase.UNKNOWN),(result(),EntryPhase.UNKNOWN),
    (result(entered=True),EntryPhase.ENTERED),(result(prevented=True),EntryPhase.BLOCKED)])
def test_no_photo_effect_does_not_replace_entry_fact(document,phase):
    assert entry_from_rows({}, {'id':7}, [{'result_json':document,'result_event_id':10,
        'status':3,'effect_state':2}]) is phase


def test_reserved_permit_never_allows_late_entry_or_rebinding():
    entries=StartEntryCoordinator()
    permit=entries.prepare('db',1)
    permit.bind('db',ticket())
    assert entries.reserve('db',1,no_intent=False) is permit
    with pytest.raises(ConsistencyError): permit.enter(ticket())
    with pytest.raises(ConsistencyError): permit.bind('db',ticket())
    assert entries.get('other',1) is None


def test_unbound_or_wrong_source_permit_cannot_enter():
    entries=StartEntryCoordinator()
    permit=entries.prepare('db',1)
    with pytest.raises(ConsistencyError): permit.enter(None)
    with pytest.raises(ConsistencyError): permit.bind('db',ticket(2))


@pytest.mark.parametrize('definition',[None,{}])
def test_missing_saved_execution_definition_is_not_an_enterable_photo(definition):
    from camctl.devices.start_entry import is_hasselblad_photo
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    assert not is_hasselblad_photo({'type':1,'driver_id':DRIVER_ID,'execution_spec_json':definition})
