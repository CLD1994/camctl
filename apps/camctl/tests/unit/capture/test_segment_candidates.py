"""分段正式原片资格只依赖固定成员事实。"""

import pytest

from camctl.capture.segment_candidates import (
    eligible_original, eligible_paired_preview, eligible_photo_original, is_paired_preview,
    is_segmented_result_policy,
    ordinary_segment_source, require_segment_write_kind,
)
from camctl.capture.segment_models import (
    FileCompletionState, OwnershipState, PreviewPairingState, SegmentMember, SegmentMemberRole,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices.segment_profiles import SegmentFileFacts, SegmentFileKind
from camctl.devices.segments import SegmentKey
from camctl.devices.tasks import CaptureResultPolicy


@pytest.mark.parametrize(('source', 'publication', 'kind', 'policy', 'allowed'), [
    ('COMPLETE_SCAN', 'CANCELED', 'MEMBERS', True, True),
    ('RETAINED_SOURCES', 'RESULT', 'FIX_MEMBERS', True, True),
    ('NO_RESULTS', 'RESULT', 'ASSESS', True, True),
    ('NO_RESULTS', 'CANCELED', 'SCOPE', True, True),
    ('RETAINED_SOURCES', 'CANCELED', 'ACTIVATE', True, True),
    ('COMPLETE_SCAN', 'CANCELED', 'PUBLISH', True, True),
    ('RETAINED_SOURCES', 'RESULT', 'PUBLISH', True, False),
    ('RETAINED_SOURCES', 'CANCELED', 'OTHER', True, False),
    ('RETAINED_SOURCES', 'RESULT', 'MEMBERS', False, False),
])
def test_nonordinary_segment_write_requires_compatible_action6(source, publication, kind, policy, allowed):
    row = {'source_kind': int(enum_for('segment_sets.source_kind')[source]),
           'publication_kind': int(enum_for('segment_sets.publication_kind')[publication])}
    action = {'execution_spec_json': {'result_policy': CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value}} if policy else None
    assert not ordinary_segment_source(row)
    if allowed:
        require_segment_write_kind(setrow=row, kind=kind, action=action)
    else:
        with pytest.raises(ConsistencyError):
            require_segment_write_kind(setrow=row, kind=kind, action=action)


def test_ordinary_complete_scan_result_needs_no_action_fact():
    row = {'source_kind': int(enum_for('segment_sets.source_kind').COMPLETE_SCAN),
           'publication_kind': int(enum_for('segment_sets.publication_kind').RESULT)}
    assert ordinary_segment_source(row)


@pytest.mark.parametrize(('source', 'publication', 'policy'), [
    (999, int(enum_for('segment_sets.publication_kind').RESULT),
     CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value),
    (int(enum_for('segment_sets.source_kind').RETAINED_SOURCES), 999,
     CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value),
    (int(enum_for('segment_sets.source_kind').RETAINED_SOURCES),
     int(enum_for('segment_sets.publication_kind').RESULT),
     CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value),
])
def test_segment_write_rejects_other_source_publication_or_policy(source, publication, policy):
    with pytest.raises(ConsistencyError):
        require_segment_write_kind(setrow={'source_kind': source, 'publication_kind': publication},
                                   kind='MEMBERS', action={'execution_spec_json': {'result_policy': policy}})


@pytest.mark.parametrize(('role', 'ownership', 'completion', 'size', 'expected'), [
    (SegmentMemberRole.ORIGINAL, OwnershipState.OWNED, FileCompletionState.COMPLETE, 1, True),
    (SegmentMemberRole.ORIGINAL, OwnershipState.OWNED, FileCompletionState.COMPLETE, 0, False),
    (SegmentMemberRole.ORIGINAL, OwnershipState.OWNED, FileCompletionState.COMPLETE, None, False),
    (SegmentMemberRole.ORIGINAL, OwnershipState.UNKNOWN, FileCompletionState.COMPLETE, 1, False),
    (SegmentMemberRole.ORIGINAL, OwnershipState.NOT_OWNED, FileCompletionState.COMPLETE, 1, False),
    (SegmentMemberRole.ORIGINAL, OwnershipState.OWNED, FileCompletionState.UNKNOWN, 1, False),
    (SegmentMemberRole.ORIGINAL, OwnershipState.OWNED, FileCompletionState.INCOMPLETE, 1, False),
    (SegmentMemberRole.PREVIEW, OwnershipState.OWNED, FileCompletionState.COMPLETE, 1, False),
    (SegmentMemberRole.OTHER, OwnershipState.OWNED, FileCompletionState.COMPLETE, 1, False),
])
def test_only_complete_nonempty_owned_original_is_eligible(role, ownership, completion, size, expected):
    pairing = (PreviewPairingState.UNKNOWN if role is SegmentMemberRole.PREVIEW
               else PreviewPairingState.NOT_APPLICABLE)
    member = SegmentMember('/DCIM/file', None, role, ownership, completion, size,
                           pairing_state=pairing)
    assert eligible_original(member) is expected


@pytest.mark.parametrize(('policy', 'expected'), [
    (CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value, True),
    (CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value, True),
    (CaptureResultPolicy.OSMO_EMPTY_TAIL_CLASSIFICATION_V1.value, False),
    (None, False), ('unknown', False),
])
def test_segmented_source_requires_an_applicable_frozen_policy(policy, expected):
    assert is_segmented_result_policy(policy) is expected


def test_photo_candidate_requires_own_fixed_file_to_be_complete_and_nonempty():
    member = SegmentMember('/DCIM/photo.jpg', None, SegmentMemberRole.OTHER,
        OwnershipState.OWNED, FileCompletionState.COMPLETE, 12)
    assert eligible_photo_original(member, SegmentFileKind.PHOTO)
    assert not eligible_photo_original(member, SegmentFileKind.OTHER)
    empty = SegmentMember('/DCIM/photo.jpg', None, SegmentMemberRole.OTHER,
        OwnershipState.OWNED, FileCompletionState.COMPLETE, 0)
    assert not eligible_photo_original(empty, SegmentFileKind.PHOTO)


def preview_facts(*, scan_pairing):
    preview = SegmentMember('/DCIM/file.lrf', None, SegmentMemberRole.PREVIEW,
        OwnershipState.OWNED, FileCompletionState.COMPLETE, 10,
        pairing_identity='/DCIM/file.osv', pairing_state=PreviewPairingState.PAIRED)
    original = SegmentMember('/DCIM/file.osv', None, SegmentMemberRole.ORIGINAL,
        OwnershipState.OWNED, FileCompletionState.COMPLETE, 20)
    if scan_pairing:
        role = enum_for('result_page_files.family_role')
        left = ('/DCIM', 'file', int(role.PREVIEW))
        right = ('/DCIM', 'file', int(role.ORIGINAL))
    else:
        left = SegmentFileFacts(SegmentFileKind.PREVIEW, SegmentKey(1, 1), '/DCIM/file')
        right = SegmentFileFacts(SegmentFileKind.VIDEO, SegmentKey(1, 1), '/DCIM/file')
    return dict(member=preview, original=original, preview_file={'original_device_file_id': 8},
        original_file_id=8, pairing_fact_id=3 if scan_pairing else None,
        pairing_fact={'original_file_id': 8} if scan_pairing else None,
        preview_family=left, original_family=right)


@pytest.mark.parametrize('scan_pairing', [False, True])
def test_preview_requires_both_fixed_endpoints_and_matching_family(scan_pairing):
    facts = preview_facts(scan_pairing=scan_pairing)
    assert is_paired_preview(facts['member'])
    assert not is_paired_preview(facts['original'])
    assert eligible_paired_preview(**facts)
    empty = SegmentMember('/DCIM/file.osv', None, SegmentMemberRole.ORIGINAL,
        OwnershipState.OWNED, FileCompletionState.COMPLETE, 0)
    assert not eligible_paired_preview(**(facts | {'original': empty}))
    with pytest.raises(ConsistencyError, match='固定文件关联'):
        eligible_paired_preview(**(facts | {'preview_file': {'original_device_file_id': 9}}))
    wrong_family = (facts['preview_family'] if scan_pairing else
                    SegmentFileFacts(SegmentFileKind.VIDEO, SegmentKey(1, 1), '/DCIM/other'))
    with pytest.raises(ConsistencyError, match='同完整名称主干'):
        eligible_paired_preview(**(facts | {'original_family': wrong_family}))
