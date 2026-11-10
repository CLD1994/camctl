"""扫描族配对只消费已固定的成员事实。"""

import pytest
from types import SimpleNamespace

from camctl.capture.result_pairing import ScanFamilyAssessment, result_file_family, scan_allows_result_pairing
from camctl.capture.segment_models import FileCompletionState, OwnershipState
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices.tasks import CaptureResultPolicy


@pytest.mark.parametrize(('complete', 'finalized', 'error', 'cursor', 'expected'), [
    (True, True, None, None, True),
    (False, True, None, None, False),
    (True, False, None, None, False),
    (True, True, 'error', None, False),
    (True, True, None, 'next', False),
])
def test_pairing_requires_a_complete_finalized_successful_scan_without_next_page(
        complete, finalized, error, cursor, expected):
    page = SimpleNamespace(scan_complete=complete, set_finalized=finalized,
                           outcome=SimpleNamespace(error=error))
    assert scan_allows_result_pairing(page, SimpleNamespace(next_cursor=cursor)) is expected


def test_incomplete_scan_short_circuits_other_facts():
    assert not scan_allows_result_pairing(SimpleNamespace(scan_complete=False), object())


def observe(assessment, **changes):
    facts = dict(ownership=OwnershipState.OWNED, completion=FileCompletionState.COMPLETE,
                 observed_complete=True, observed_size=10, observed_locator={'path': '/a'},
                 saved_file={'size_bytes': 10, 'locator_json': {'path': '/a'}})
    assessment.observe(**(facts | changes))


@pytest.mark.parametrize(('original_count', 'preview_count', 'size', 'existing', 'expected'), [
    (0, 1, 10, None, 'MISSING'),
    (2, 1, 10, None, 'AMBIGUOUS'),
    (1, 2, 0, None, 'AMBIGUOUS'),
    (1, 1, 0, None, 'INVALID'),
    (1, 1, 10, None, 'UNIQUE'),
    (1, 1, 10, 7, 'UNIQUE'),
    (1, 1, 10, 8, 'CONFLICT'),
])
def test_reliable_family_classifies_counts_size_and_existing_relation(
        original_count, preview_count, size, existing, expected):
    assessment = ScanFamilyAssessment()
    observe(assessment, observed_size=size, saved_file={'size_bytes': size, 'locator_json': {'path': '/a'}})
    reason = assessment.conclusion(original_count=original_count, preview_count=preview_count,
                                   original_id=7 if original_count == 1 else None,
                                   preview_file={'original_device_file_id': existing})
    assert reason is enum_for('device_file_pairing_facts.reason')[expected]


@pytest.mark.parametrize('changes', [
    {'ownership': OwnershipState.UNKNOWN},
    {'ownership': OwnershipState.NOT_OWNED},
    {'completion': FileCompletionState.UNKNOWN},
    {'completion': FileCompletionState.INCOMPLETE},
    {'observed_complete': False},
    {'observed_size': None},
])
def test_unreliable_member_keeps_whole_family_unknown(changes):
    assessment = ScanFamilyAssessment()
    observe(assessment)
    observe(assessment, **changes)
    assert assessment.conclusion(original_count=1, preview_count=1,
                                 original_id=7, preview_file={}) is None


@pytest.mark.parametrize('changes', [
    {'saved_file': {'size_bytes': 11, 'locator_json': {'path': '/a'}}},
    {'saved_file': {'size_bytes': 10, 'locator_json': {'path': '/other'}}},
])
def test_reliable_observation_cannot_contradict_fixed_file(changes):
    assessment = ScanFamilyAssessment()
    with pytest.raises(ConsistencyError, match='当前族原观察与固定文件大小或定位矛盾'):
        observe(assessment, **changes)


def test_unknown_member_does_not_hide_later_reliable_contradiction():
    assessment = ScanFamilyAssessment()
    observe(assessment, ownership=OwnershipState.UNKNOWN)
    with pytest.raises(ConsistencyError):
        observe(assessment, saved_file={'size_bytes': 11, 'locator_json': {'path': '/a'}})


@pytest.mark.parametrize(('identity', 'policy', 'expected'), [
    ('/DCIM/abc.OSV', None, ('/DCIM', 'abc', 'ORIGINAL')),
    ('/DCIM/abc.LrF', None, ('/DCIM', 'abc', 'PREVIEW')),
    ('/abc.OSV', None, ('/', 'abc', 'ORIGINAL')),
    ('abc.OSV', None, None),
    ('/DCIM/.OSV', None, None),
    ('/DCIM/abc.mp4', None, None),
    ('/DCIM/abc.MP4', CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value,
     ('/DCIM', 'abc', 'ORIGINAL')),
    ('/DCIM/abc.LRF', CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value,
     ('/DCIM', 'abc', 'PREVIEW')),
    ('/DCIM/abc.JPG', CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value, None),
])
def test_family_classification_uses_frozen_result_policy(identity, policy, expected):
    if expected is None:
        assert result_file_family(identity, result_policy=policy) == (None, None, None)
    else:
        directory, stem, role = expected
        role_id = enum_for('result_page_files.family_role')[role]
        assert result_file_family(identity, result_policy=policy) == (directory, stem, int(role_id))
