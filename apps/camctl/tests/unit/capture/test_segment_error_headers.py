"""公开分段错误头只保存有限业务事实。"""
from camctl.contracts.workflow_errors import validate_error_details


def test_segment_gap_header_is_registered_without_private_references():
    validate_error_details('capture_segment_gap', {
        'source_action_instance_id': '7', 'description': '录像存在缺口，默认范围保留连续原片。',
        'gap_count': 2, 'default_scope': {'state': 'nonempty', 'member_count': 2},
        'retained_original_count': 4,
    })

import json
from dataclasses import replace
from enum import StrEnum
import pytest
from camctl.capture.segment_errors import SegmentErrorHeader
from camctl.capture.segment_models import DefaultScopeState, SegmentGap, SegmentGapKind, SegmentGapNeighborState, EvidenceState
from camctl.capture.segment_storage import document, decode_enum, encode_enum
from camctl.devices.segments import SegmentKey


def _gap():
    return SegmentGap(SegmentKey(1,3),SegmentKey(1,3),SegmentGapKind.MISSING_POSITION,'real-a','real-d',SegmentGapNeighborState.KNOWN,SegmentGapNeighborState.KNOWN)


@pytest.mark.parametrize('state,count',[(DefaultScopeState.UNKNOWN,None),(DefaultScopeState.EMPTY,0),(DefaultScopeState.NONEMPTY,2)])
def test_header_three_scope_states_remain_distinct(state,count):
    code='default_segment_scope_unconfirmed' if state is DefaultScopeState.UNKNOWN else 'default_segment_scope_empty' if state is DefaultScopeState.EMPTY else 'capture_segment_gap'
    header=SegmentErrorHeader(7,1,state,count,4,_gap())
    value=header.details(code)
    assert value['default_scope']=={'state':state.value,'member_count':count}
    assert 'gaps' not in value and 'retained_output_ids' not in value
    assert '7' in value['description']
    assert value['first_gap']['start_key']=={'directory_number':1,'index':3}


@pytest.mark.parametrize('change',[{'gap_count':0},{'scope_count':5},{'scope_count':True},{'retained_original_count':True},{'scope_state':DefaultScopeState.UNKNOWN}])
def test_header_rejects_contradictory_counts(change):
    with pytest.raises(ValueError):replace(SegmentErrorHeader(7,1,DefaultScopeState.NONEMPTY,2,4,_gap()),**change)


@pytest.mark.parametrize('change',[{'set_id':1},{'gaps':[{'kind':'missing_position'}]},{'gap_count':True},{'default_scope':{'state':'empty','member_count':1}}])
def test_public_header_rejects_private_or_malformed_fields(change):
    base=SegmentErrorHeader(7,1,DefaultScopeState.NONEMPTY,2,4,_gap()).details('capture_segment_gap')
    with pytest.raises(ValueError):validate_error_details('capture_segment_gap',{**base,**change})


def test_generated_text_enums_keep_strenum_and_integer_roundtrip():
    assert isinstance(EvidenceState.CONFIRMED,StrEnum)
    assert json.dumps(EvidenceState.CONFIRMED)=='"confirmed"'
    from camctl.capture.segment_storage import _ENUM_COLUMNS
    for kind in _ENUM_COLUMNS:
        for member in kind:
            assert isinstance(member,StrEnum)
            assert decode_enum(kind,encode_enum(member)) is member
            assert json.loads(json.dumps(member))==member.value
        for invalid in (True,0,-1,2**63):
            with pytest.raises(ValueError):decode_enum(kind,invalid)


def test_lower_bound_codec_preserves_exact_value_beyond_sqlite_integer():
    from camctl.capture.segment_storage import assessment_document,assessment_from_document
    from camctl.capture.segment_models import SegmentAccumulator,SegmentSetRef,CompletionFacts
    from camctl.capture.segments import finish_segment_assessment
    ref=SegmentSetRef(7,3,28,1)
    facts=CompletionFacts(EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.UNKNOWN,None,EvidenceState.UNKNOWN,EvidenceState.UNKNOWN,True,False)
    result=finish_segment_assessment(SegmentAccumulator(ref,10000,None),facts)
    enormous=9223372036854775807*4500
    result=replace(result,complete_segment_count=9223372036854775807,lower_bound_ms=enormous)
    raw=assessment_document(result)
    assert raw['lower_bound_ms']=='41505174165846491131500'
    assert assessment_from_document(raw)==result
    for invalid in ['00','+1','1.0',True,1]:
        with pytest.raises(ValueError):assessment_from_document({**raw,'lower_bound_ms':invalid})


def test_storage_request_accepts_references_and_classification_without_file_attributes():
    from inspect import signature
    from camctl.capture.segment_storage import SegmentMemberInput
    names=set(signature(SegmentMemberInput).parameters)
    assert 'member' not in names and 'size_bytes' not in names and 'sha256' not in names
    assert {'file_id','page_event_id','file_event_id','role','ownership','completion','pairing_state'}<=names


@pytest.mark.parametrize('code,state,count',[('default_segment_scope_empty','nonempty',1),('default_segment_scope_unconfirmed','empty',0)])
def test_scope_error_code_rejects_another_scope_state(code,state,count):
    value={'source_action_instance_id':'7','description':'来源默认范围已保存。','gap_count':0,'default_scope':{'state':state,'member_count':count},'retained_original_count':1}
    with pytest.raises(ValueError):validate_error_details(code,value)


def test_header_json_validation_rejects_growth_and_cross_field_count_mismatch():
    from camctl.capture.segment_errors import validate_segment_error_header
    header=SegmentErrorHeader(7,1,DefaultScopeState.NONEMPTY,2,4,_gap()).details('capture_segment_gap')
    assert validate_segment_error_header('capture_segment_gap',header).scope_count==2
    for changed in [{**header,'retained_original_count':1},{**header,'gaps':[]},{**header,'first_gap':None},{**header,'default_scope':{'state':'nonempty','member_count':True}}]:
        with pytest.raises(ValueError):validate_segment_error_header('capture_segment_gap',changed)


def test_unknown_gap_header_remains_null_and_cannot_be_capture_gap_error():
    header=SegmentErrorHeader(7,None,DefaultScopeState.UNKNOWN,None,2)
    raw=header.details('default_segment_scope_unconfirmed')
    assert raw['gap_count'] is None and 'first_gap' not in raw
    assert 'None' not in raw['description']
    with pytest.raises(ValueError):header.details('capture_segment_gap')
    with pytest.raises(ValueError):replace(header,scope_state=DefaultScopeState.EMPTY,scope_count=0)


def test_unknown_gap_assessment_cannot_claim_reliable_duration_or_scope():
    from camctl.capture.segment_models import SegmentAccumulator,SegmentSetRef,CompletionFacts
    from camctl.capture.segments import finish_segment_assessment
    facts=CompletionFacts(EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.UNKNOWN,None,EvidenceState.UNKNOWN,EvidenceState.UNKNOWN,True,False)
    result=finish_segment_assessment(SegmentAccumulator(SegmentSetRef(7,3,28,1),10000,None,order_unknown=True),facts)
    with pytest.raises(ValueError):replace(result,complete_segment_count=0,lower_bound_ms=0)


@pytest.mark.parametrize('position',[(0,0,'real',True),(0,1,'real',1),(1,1,'real',1,2),(1,1,'',1)])
def test_raw_member_cursor_rejects_invalid_technical_identity(position):
    from camctl.capture.segment_models import SegmentSetRef
    from camctl.capture.segment_storage import SegmentMemberCursor
    with pytest.raises(ValueError):SegmentMemberCursor(SegmentSetRef(7,3,28,1),position)


def test_page_request_rejects_unregistered_reference_only_fields():
    from camctl.capture.segment_storage import SegmentMemberInput
    from camctl.capture.segment_models import SegmentMemberRole,OwnershipState,FileCompletionState
    with pytest.raises(TypeError):SegmentMemberInput(1,2,3,SegmentMemberRole.ORIGINAL,OwnershipState.OWNED,FileCompletionState.COMPLETE,size_bytes=10)


def test_storage_header_rejects_unverified_neighbor_output_identity():
    from camctl.capture.segment_errors import validate_segment_error_header
    raw=SegmentErrorHeader(7,1,DefaultScopeState.NONEMPTY,2,4,_gap()).details('capture_segment_gap')
    raw['first_gap']['left']['output_id']='999'
    with pytest.raises(ValueError):validate_segment_error_header('capture_segment_gap',raw)


@pytest.mark.parametrize('expanded', [None, 'scope_outputs', 'neighbor_output'])
def test_finish_request_uses_the_finite_gap_header(expanded):
    from camctl.capture.media import RecordingFailure
    from camctl.capture.segment_finish import FinishSegmentRecording
    from camctl.capture.segment_models import SegmentSetRef
    raw=SegmentErrorHeader(7,1,DefaultScopeState.NONEMPTY,2,4,_gap()).details('capture_segment_gap')
    if expanded=='scope_outputs':raw['default_scope']['output_ids']=['1','2']
    elif expanded=='neighbor_output':raw['first_gap']['left']['output_id']='999'
    def request():
        return FinishSegmentRecording(SegmentSetRef(7,3,28,1),4,5,100,RecordingFailure('capture_segment_gap',raw))
    if expanded is None:
        assert request().failure.details==raw
    else:
        with pytest.raises(ValueError):request()
