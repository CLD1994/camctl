"""分段查询在场结果与显式取回项结算保持同一可靠事实。"""

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.contracts.workflow_errors import item_error_id
from camctl.outputs.segment_query_rules import (
    queried_item_values, queried_output_values, require_query_output_projection,
    require_query_resolution,
)


@pytest.mark.parametrize('present', [True, False])
def test_actual_presence_drives_matching_output_and_item(present):
    output = {'id': 17, 'device_file_id': 9, 'availability': 1 if present else 4}
    after = queried_output_values(present, output['id'])
    assert after['availability'] == output['availability']
    require_query_output_projection(present=present, output=output,
        file={'presence_state': 2 if present else 3}, before={'availability': 5}, after=after)
    require_query_resolution(present=present, resolve=present)
    values = queried_item_values(output=output, present=present, error_code=None,
                                 error_details=None, result_event_id=4)
    assert values['output_id'] == 17
    assert values['status'] == (2 if present else 4)
    if not present:
        assert values['error_code'] == item_error_id('obtain_items', 'output_unavailable')
        assert values['error_details_json'] == {'output_id': '17', 'availability': 'missing'}


@pytest.mark.parametrize(('present', 'resolve'), [(None, True), (None, False), (False, True), (True, False)])
def test_query_resolution_does_not_replace_unknown_or_opposite_result(present, resolve):
    with pytest.raises(ConsistencyError):
        require_query_resolution(present=present, resolve=resolve)


def test_output_projection_requires_actual_prior_file_presence_and_exact_change():
    output = {'id': 17, 'device_file_id': 9}
    after = queried_output_values(True, 17)
    for file, before, changed in (
        (None, {'availability': 5}, after),
        ({'presence_state': 3}, {'availability': 5}, after),
        ({'presence_state': 2}, {'availability': 1}, after),
        ({'presence_state': 2}, {'availability': 5}, {**after, 'extra': 1}),
    ):
        with pytest.raises(ConsistencyError, match='原文件结果不符'):
            require_query_output_projection(present=True, output=output, file=file,
                                            before=before, after=changed)


@pytest.mark.parametrize(('present', 'availability'), [(None, 1), (True, 4), (False, 1)])
def test_actual_query_settlement_requires_matching_reliable_output(present, availability):
    with pytest.raises(ConsistencyError):
        queried_item_values(output={'id': 17, 'availability': availability}, present=present,
                            error_code=None, error_details=None, result_event_id=4)


def test_unavailable_without_query_must_preserve_saved_output_state():
    code = item_error_id('obtain_items', 'output_unavailable')
    output = {'id': 17, 'availability': 3}
    details = {'output_id': '17', 'availability': 'cleaned'}
    values = queried_item_values(output=output, present=None, error_code=code,
                                 error_details=details, result_event_id=None)
    assert values['error_details_json'] == details
    assert values['error_details_json'] is not details
    with pytest.raises(ConsistencyError):
        queried_item_values(output=output, present=None, error_code=code,
                            error_details={'output_id': '17', 'availability': 'missing'},
                            result_event_id=None)
