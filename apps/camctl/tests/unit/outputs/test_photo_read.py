"""按需照片的新读取使用本次长度，不拼接旧下载字节。"""

import pytest

from camctl.operations.models import AttemptTicket
from camctl.outputs.photo_read import PhotoReadStart, decide_photo_read_start, parse_photo_read_start


def test_first_photo_read_uses_current_download_size():
    result = decide_photo_read_start(previous_size=None, committed_bytes=0,
                                     reset_pending=False, current_size=225280)
    assert (result.source_size, result.committed_bytes, result.reset_pending) == (225280, 0, False)


def test_new_photo_download_resets_old_bytes_even_if_size_is_unchanged():
    result = decide_photo_read_start(previous_size=224835, committed_bytes=100,
                                     reset_pending=False, current_size=224835)
    assert (result.source_size, result.committed_bytes, result.reset_pending) == (224835, 0, True)


def test_new_photo_download_keeps_its_own_length_when_previews_differ():
    result = decide_photo_read_start(previous_size=224835, committed_bytes=224835,
                                     reset_pending=False, current_size=225280)
    assert (result.source_size, result.committed_bytes, result.reset_pending) == (225280, 0, True)


@pytest.mark.parametrize("previous,committed", [(None, 1), (0, 0), (3, 4), (3, -1), (True, 0)])
def test_photo_read_rejects_contradictory_previous_progress(previous, committed):
    with pytest.raises(ValueError):
        decide_photo_read_start(previous_size=previous, committed_bytes=committed,
                                reset_pending=False, current_size=3)


def test_read_start_is_bound_to_the_original_ticket():
    ticket = AttemptTicket(2, "read", "7", "read/7", 5)
    request = PhotoReadStart(7, ticket, 225280, 100)
    assert request.evidence()["size_bytes"] == 225280
    assert request.evidence()["ticket"]["attempt_no"] == 2
    assert parse_photo_read_start(request.evidence(), 100) == request
    with pytest.raises(ValueError):
        PhotoReadStart(8, ticket, 225280, 100)


@pytest.mark.parametrize("change", [
    {"size_bytes": True}, {"size_bytes": 0}, {"size_bytes": 1.5},
    {"ticket": {"run_id": 5, "attempt_no": 2, "operation": "read",
                "target_id": "07", "responsibility_key": "read/7"}},
])
def test_read_start_evidence_rejects_invalid_length_or_identity(change):
    ticket = AttemptTicket(2, "read", "7", "read/7", 5)
    evidence = PhotoReadStart(7, ticket, 31, 100).evidence() | change
    with pytest.raises(ValueError):
        parse_photo_read_start(evidence, 100)
