"""主机拍摄申请的纯格式契约。"""

from dataclasses import asdict
from decimal import Decimal

import pytest

from camctl.contracts.values import MAX_OBJECT_ID
from camctl.history.host_photo_request import (
    HostPhotoComplete,
    HostPhotoRequestError,
    PrepareHostPhoto,
    parse_host_photo_request,
)


def _prepare(**changes):
    return {"action_id": 7, "occurred_at": 0, **changes}


def _complete(**changes):
    return {
        "action_id": 7,
        "original_file_id": 31,
        "original_size_bytes": 4096,
        "original_sha256": "a" * 64,
        "preview_file_id": 32,
        "preview_size_bytes": 128,
        "preview_sha256": "b" * 64,
        "occurred_at": -1,
        **changes,
    }


def test_prepare_accepts_epoch_timestamp_and_exact_member_set():
    request = parse_host_photo_request(_prepare(), reason=1)
    assert request == PrepareHostPhoto(action_id=7, occurred_at=0)
    assert asdict(request) == {"action_id": 7, "occurred_at": 0}


def test_complete_accepts_pre_epoch_time_and_sqlite_boundary_values():
    request = parse_host_photo_request(
        _complete(action_id=MAX_OBJECT_ID, original_file_id=MAX_OBJECT_ID - 1,
                  preview_file_id=MAX_OBJECT_ID, original_size_bytes=MAX_OBJECT_ID,
                  preview_size_bytes=1),
        reason=2,
    )
    assert isinstance(request, HostPhotoComplete)
    assert request.occurred_at == -1


def test_json_request_parser_normalizes_mathematical_integer_decimal_values():
    prepare = parse_host_photo_request(
        _prepare(action_id=Decimal("7.0"), occurred_at=Decimal("0E0")), reason=1)
    complete = parse_host_photo_request(
        _complete(action_id=Decimal("7.0"), original_file_id=Decimal("31.0"),
                  original_size_bytes=Decimal("4096E0"), preview_file_id=Decimal("32E0"),
                  preview_size_bytes=Decimal("128.0"), occurred_at=Decimal("-1.0")),
        reason=2,
    )

    assert prepare == PrepareHostPhoto(action_id=7, occurred_at=0)
    assert isinstance(complete, HostPhotoComplete)
    assert all(type(value) is int for value in (
        complete.action_id, complete.original_file_id, complete.original_size_bytes,
        complete.preview_file_id, complete.preview_size_bytes, complete.occurred_at,
    ))


@pytest.mark.parametrize(
    "value",
    [None, [], 0, {"action_id": 7}, {"action_id": 7, "occurred_at": 0, "extra": 1}],
)
def test_prepare_requires_object_and_exact_members(value):
    with pytest.raises(HostPhotoRequestError):
        parse_host_photo_request(value, reason=1)


@pytest.mark.parametrize("bad", [True, 0, -1, MAX_OBJECT_ID + 1, 1.0, "7"])
@pytest.mark.parametrize("member", ["action_id", "original_file_id", "preview_file_id"])
def test_identity_fields_are_precise_positive_sqlite_integers(member, bad):
    request = _prepare() if member == "action_id" else _complete()
    request[member] = bad
    with pytest.raises(HostPhotoRequestError):
        parse_host_photo_request(request, reason=1 if member == "action_id" else 2)


@pytest.mark.parametrize("bad", [True, "0", 1.0, None])
def test_occurred_at_is_an_integer_timestamp_not_an_object_id(bad):
    with pytest.raises(HostPhotoRequestError):
        parse_host_photo_request(_prepare(occurred_at=bad), reason=1)


def test_request_dataclasses_keep_strict_python_integer_types():
    with pytest.raises(HostPhotoRequestError):
        PrepareHostPhoto(action_id=Decimal("7.0"), occurred_at=0)
    with pytest.raises(HostPhotoRequestError):
        PrepareHostPhoto(action_id=7, occurred_at=Decimal("0E0"))
    with pytest.raises(HostPhotoRequestError):
        HostPhotoComplete(**_complete(original_size_bytes=Decimal("4096E0")))


@pytest.mark.parametrize("member,bad", [
    ("original_size_bytes", 0),
    ("preview_size_bytes", -1),
    ("original_size_bytes", MAX_OBJECT_ID + 1),
    ("preview_size_bytes", True),
    ("original_size_bytes", 1.0),
])
def test_complete_sizes_are_positive_precise_sqlite_integers(member, bad):
    with pytest.raises(HostPhotoRequestError):
        parse_host_photo_request(_complete(**{member: bad}), reason=2)


@pytest.mark.parametrize("member,bad", [
    ("original_sha256", "A" * 64),
    ("preview_sha256", "b" * 63),
    ("original_sha256", "z" * 64),
    ("preview_sha256", None),
])
def test_complete_hashes_are_lowercase_sha256(member, bad):
    with pytest.raises(HostPhotoRequestError):
        parse_host_photo_request(_complete(**{member: bad}), reason=2)


def test_complete_requires_distinct_file_ids():
    with pytest.raises(HostPhotoRequestError, match="不同文件身份"):
        parse_host_photo_request(_complete(preview_file_id=31), reason=2)


@pytest.mark.parametrize("reason", [0, 3, True])
def test_only_create_and_complete_reasons_are_supported(reason):
    with pytest.raises(HostPhotoRequestError):
        parse_host_photo_request(_prepare(), reason=reason)
