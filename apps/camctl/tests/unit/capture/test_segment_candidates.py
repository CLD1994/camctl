"""分段正式原片资格只依赖固定成员事实。"""

import pytest

from camctl.capture.segment_candidates import eligible_original
from camctl.capture.segment_models import (
    FileCompletionState, OwnershipState, PreviewPairingState, SegmentMember, SegmentMemberRole,
)


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
