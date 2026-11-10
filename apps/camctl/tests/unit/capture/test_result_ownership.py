"""结果文件归属按原责任和冻结产品规则判断。"""

import pytest

from camctl.capture.result_inputs import ObservedFile
from camctl.capture.result_ownership import require_result_file_ownership
from camctl.capture.results import FileKind, ProductRule
from camctl.contracts.values import ConsistencyError


def check(**changes):
    facts = dict(action_id=7, source_action_id=None, ownership_evidence=None,
                 responsibility_key='results/9', target_id=9,
                 observed_file=ObservedFile('/raw.lrf', {}, {}, True, 10,
                                            kind=FileKind.VIDEO, format_id='lrf',
                                            paired_identity='/raw.osv'),
                 product_rules=(ProductRule(FileKind.VIDEO, format_id='osv'),))
    require_result_file_ownership(**(facts | changes))


def test_fixed_owned_file_needs_no_optional_preview_exception():
    check(source_action_id=7, ownership_evidence={}, responsibility_key='scope/9',
          product_rules=())


def test_explicit_optional_preview_may_remain_unowned():
    check()


@pytest.mark.parametrize('changes', [
    {'responsibility_key': 'scope/9'},
    {'source_action_id': 8},
    {'source_action_id': 7, 'ownership_evidence': None},
    {'observed_file': ObservedFile('/raw.lrf', {}, {}, True, 10,
                                   kind=FileKind.VIDEO, format_id='lrf')},
    {'product_rules': ()},
    {'product_rules': (ProductRule(FileKind.VIDEO, format_id='lrf'),)},
])
def test_required_or_foreign_file_without_owned_evidence_is_rejected(changes):
    with pytest.raises(ConsistencyError, match='未确认归属'):
        check(**changes)
