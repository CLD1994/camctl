"""固定取消的入口复合申请使用封闭结构。"""
import pytest
from camctl.history.validators import _check_evidence_member, EventValidationError


@pytest.mark.parametrize('value', [None, {}, {'unexpected': 1},
    {'action_id': 1, 'occurred_at': True, 'targets': [], 'blocks': []}])
def test_photo_entry_request_rejects_incomplete_or_untyped_header(value):
    with pytest.raises(EventValidationError):
        _check_evidence_member(1, 'photo_entry_request', value, 3)


def _valid():
    return {'action_id':2,'occurred_at':1750000000000000,
        'targets':[{'action_id':1,'basis':1,'effect':1}],
        'blocks':[{'database_id':'original-db','action_id':1,'ticket':{
            'attempt_id':1,'operation':'control','target_id':'7',
            'responsibility_key':'start/1','run_id':8}}]}


def test_photo_entry_request_accepts_original_control_ticket_or_no_intent():
    _check_evidence_member(1,'photo_entry_request',_valid(),3)
    value=_valid()
    value['blocks'][0]['ticket']=None
    _check_evidence_member(1,'photo_entry_request',value,3)


@pytest.mark.parametrize('field',['operation','responsibility','target','source','duplicate','basis','effect'])
def test_photo_entry_request_rejects_false_original_identity(field):
    value=_valid()
    block=value['blocks'][0]
    if field=='operation': block['ticket']['operation']='read'
    if field=='responsibility': block['ticket']['responsibility_key']='start/9'
    if field=='target': block['ticket']['target_id']='07'
    if field=='source': block['action_id']=9
    if field=='duplicate': value['blocks'].append(dict(block))
    if field=='basis': value['targets'][0]['basis']=2
    if field=='effect': value['targets'][0]['effect']=3
    with pytest.raises(EventValidationError):
        _check_evidence_member(1,'photo_entry_request',value,3)
