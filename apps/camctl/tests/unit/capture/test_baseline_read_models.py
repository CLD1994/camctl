"""基准调用意图固定原输入，不以意图证明设备已经派发。"""
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from camctl.capture import baseline_models as models
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor, DirectoryRequest


_BINDING = DeviceBinding('cam-1', 'camera-driver')
_ROOTS = ('/data/A', '/data/B')


def _intent(**changes):
    values = dict(action_id=1, activity_id=2, binding=_BINDING,
        directories=_ROOTS, cursor=None, timeout_s=Decimal('0.125'),
        baseline_ref=models.BaselineRef(2, models.BaselineState.COLLECTING, None, None, 0, 0),
        read_no=1, call_key='1' * 32, result_save_key='2' * 32, occurred_at=1)
    values.update(changes)
    return models.BaselineReadIntent(**values)


def test_intent_freezes_complete_normalized_directory_request():
    directories = ['/data/B', '/data/A']
    intent = _intent(directories=directories)
    directories.append('/data/C')
    assert intent.directories == _ROOTS
    assert intent.directory_request() == DirectoryRequest(_BINDING, _ROOTS, None, 128, Decimal('0.125'))
    assert intent.baseline_ref == models.BaselineRef(2, models.BaselineState.COLLECTING, None, None, 0, 0)
    assert (intent.call_key, intent.result_save_key, intent.read_no, intent.occurred_at) == (
        '1' * 32, '2' * 32, 1, 1)
    with pytest.raises(FrozenInstanceError):
        intent.read_no = 2


@pytest.mark.parametrize('timeout', [Decimal('0'), Decimal('-1'), Decimal('NaN'),
    Decimal('Infinity'), 1, 0.5, True, '1', None])
def test_intent_requires_actual_finite_positive_decimal_timeout(timeout):
    with pytest.raises(ValueError):
        _intent(timeout_s=timeout)


@pytest.mark.parametrize('field', ['action_id', 'activity_id', 'read_no'])
@pytest.mark.parametrize('value', [0, -1, True, '1', 1.0, None])
def test_intent_rejects_invalid_numeric_identity(field, value):
    with pytest.raises(ValueError):
        _intent(**{field: value})


@pytest.mark.parametrize('field', ['call_key', 'result_save_key'])
@pytest.mark.parametrize('value', ['', 'A' * 32, 'g' * 32, '1' * 31, 1, None])
def test_intent_rejects_noncanonical_operation_identity(field, value):
    with pytest.raises(ValueError):
        _intent(**{field: value})


def test_call_and_result_keys_must_be_distinct():
    with pytest.raises(ValueError):
        _intent(result_save_key='1' * 32)


@pytest.mark.parametrize('reference', [None,
    models.BaselineRef(3, models.BaselineState.COLLECTING, None, None, 0, 0),
    models.BaselineRef(2, models.BaselineState.FIXED, None, None, 0, 0),
    models.BaselineRef(2, models.BaselineState.COLLECTING, 5, 5, 1, 1)])
def test_first_read_requires_same_collecting_activity_and_empty_prefix(reference):
    with pytest.raises(ValueError):
        _intent(baseline_ref=reference)


def test_followup_retains_original_cursor_and_prefix():
    cursor = DirectoryCursor(_BINDING, _ROOTS, 1, '/data/B/one.osv')
    reference = models.BaselineRef(2, models.BaselineState.COLLECTING, 5, 8, 2, 129)
    intent = _intent(read_no=3, cursor=cursor, baseline_ref=reference)
    assert intent.directory_request().cursor == cursor
    assert intent.baseline_ref == reference
    with pytest.raises(ValueError):
        replace(intent, cursor=None)
    with pytest.raises(ValueError):
        replace(intent, read_no=1)


def test_cursor_cannot_change_original_binding_or_scope():
    foreign = DirectoryCursor(DeviceBinding('other', 'camera-driver'), _ROOTS, 0, None)
    narrow = DirectoryCursor(_BINDING, (_ROOTS[0],), 0, None)
    for cursor in (foreign, narrow):
        with pytest.raises(ValueError):
            _intent(read_no=2, cursor=cursor)


def test_actual_return_freezes_error_details_without_tool_diagnostics():
    from camctl.devices.directory import DirectoryRead
    from camctl.operations.models import ErrorValue
    details={'command':{'path':'/data/A'}}
    error=ErrorValue('directory_failed','baseline',details)
    request=models.BaselineReadReturn(1,2,error,DirectoryRead(None,error,None))
    details['command']['path']='/different'
    assert request.error.details=={'command':{'path':'/data/A'}}
    assert request.returned.error==request.error
    assert request.returned.outcome is None


def test_actual_return_rejects_a_different_error_from_same_read():
    from camctl.devices.directory import DirectoryRead
    from camctl.operations.models import ErrorValue
    error=ErrorValue('directory_failed','baseline',{})
    with pytest.raises(ValueError):
        models.BaselineReadReturn(1,2,None,DirectoryRead(None,error,None))
