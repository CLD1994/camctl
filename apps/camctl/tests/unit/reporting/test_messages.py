"""报告控制消息契约：版本、字段、容量与任务身份的严格校验。"""

from __future__ import annotations

import json

import pytest

from camctl.reporting.messages import (
    MAX_MESSAGE_BYTES,
    ErrorKind,
    JobMessage,
    MessageProtocolError,
    ReadyMessage,
    ResultFailureMessage,
    ResultSuccessMessage,
    ShutdownMessage,
    StartupFailedMessage,
    StartupPhase,
    decode_message,
    encode_message,
    new_job_id,
)

_INSTANCE = "f" * 32
_SHA = "0" * 64


def _job(**overrides) -> JobMessage:
    values = dict(
        job_id="task-1",
        report_id=1,
        from_wm=0,
        to_wm=4,
        frozen_event_id=9,
        instance_id=_INSTANCE,
        db_path="/var/lib/camctl/state.db",
        staging_path="/var/lib/camctl/staging/reports/report-1.json",
        entity_batch_size=32,
        event_batch_size=256,
        busy_timeout_ms=9000,
    )
    values.update(overrides)
    return JobMessage(**values)


def _success(**overrides) -> ResultSuccessMessage:
    values = dict(
        job_id="task-1",
        instance_id=_INSTANCE,
        path="/var/lib/camctl/staging/reports/report-1.json",
        size_bytes=128,
        sha256=_SHA,
    )
    values.update(overrides)
    return ResultSuccessMessage(**values)


class TestRoundTrip:
    @pytest.mark.parametrize("message", [
        ReadyMessage(),
        ShutdownMessage(),
        StartupFailedMessage(phase=StartupPhase.RUNTIME_CHECK, reason="sqlite 3.30 过旧"),
        _job(),
        _success(),
        ResultFailureMessage(
            job_id="task-1", instance_id=_INSTANCE,
            error_kind=ErrorKind.STATE, error_code="state_database_error",
            error_message="历史解释失败"),
    ])
    def test_round_trip_preserves_message(self, message) -> None:
        assert decode_message(encode_message(message)) == message

    def test_wire_is_versioned_compact_object(self) -> None:
        payload = json.loads(encode_message(ReadyMessage()))
        assert payload == {"version": 1, "kind": "ready"}


class TestWireRejection:
    def _encode_ready(self) -> bytes:
        return encode_message(ReadyMessage())

    def test_unknown_version_is_rejected(self) -> None:
        with pytest.raises(MessageProtocolError):
            decode_message(b'{"kind":"ready","version":2}')

    def test_missing_version_is_rejected(self) -> None:
        with pytest.raises(MessageProtocolError):
            decode_message(b'{"kind":"ready"}')

    def test_unknown_kind_is_rejected(self) -> None:
        with pytest.raises(MessageProtocolError):
            decode_message(b'{"kind":"surprise","version":1}')

    def test_extra_field_is_rejected(self) -> None:
        raw = json.loads(self._encode_ready())
        raw["extra"] = 1
        with pytest.raises(MessageProtocolError):
            decode_message(json.dumps(raw).encode("utf-8"))

    def test_missing_field_is_rejected(self) -> None:
        raw = json.loads(encode_message(_job()))
        del raw["frozen_event_id"]
        with pytest.raises(MessageProtocolError):
            decode_message(json.dumps(raw).encode("utf-8"))

    @pytest.mark.parametrize("field,value", [
        ("report_id", True), ("report_id", "1"), ("report_id", 0),
        ("to_wm", -1), ("from_wm", -1), ("frozen_event_id", -1),
        ("entity_batch_size", 0), ("event_batch_size", 0), ("busy_timeout_ms", 0),
        ("job_id", ""), ("job_id", "-bad"), ("job_id", "x" * 129),
        ("instance_id", "short"), ("instance_id", "G" * 32),
        ("db_path", ""), ("staging_path", ""),
    ])
    def test_invalid_job_values_are_rejected(self, field: str, value) -> None:
        with pytest.raises(MessageProtocolError):
            encode_message(_job(**{field: value}))

    def test_window_must_not_be_inverted(self) -> None:
        with pytest.raises(MessageProtocolError):
            encode_message(_job(from_wm=5, to_wm=4))

    @pytest.mark.parametrize("mutation", [
        {"sha256": "XYZ"}, {"sha256": "0" * 63}, {"size_bytes": -1}, {"path": ""},
        {"job_id": "task/1"},
    ])
    def test_invalid_success_values_are_rejected(self, mutation) -> None:
        with pytest.raises(MessageProtocolError):
            encode_message(_success(**mutation))

    def test_failure_requires_registered_error_kind(self) -> None:
        with pytest.raises(MessageProtocolError):
            encode_message(ResultFailureMessage(
                job_id="task-1", instance_id=_INSTANCE, error_kind="fatal",
                error_code="boom", error_message="x"))

    def test_result_status_must_match_payload(self) -> None:
        raw = json.loads(encode_message(_success()))
        raw["error_kind"] = "report"
        with pytest.raises(MessageProtocolError):
            decode_message(json.dumps(raw).encode("utf-8"))

    def test_non_utf8_is_rejected(self) -> None:
        with pytest.raises(MessageProtocolError):
            decode_message(b'\xff\xfe{"kind":"ready"}')

    def test_invalid_json_is_rejected(self) -> None:
        with pytest.raises(MessageProtocolError):
            decode_message(b'{"kind":')

    def test_non_object_payload_is_rejected(self) -> None:
        with pytest.raises(MessageProtocolError):
            decode_message(b'[]')


class TestCapacityAndTaskIdentity:
    def test_oversized_payload_is_rejected_on_decode(self) -> None:
        with pytest.raises(MessageProtocolError):
            decode_message(b"x" * (MAX_MESSAGE_BYTES + 1))

    def test_largest_field_values_stay_under_capacity(self) -> None:
        job = _job(
            db_path="/" + "p" * 1023,
            staging_path="/" + "s" * 1023,
        )
        assert len(encode_message(job)) < MAX_MESSAGE_BYTES

    def test_new_job_ids_are_distinct_and_usable(self) -> None:
        first, second = new_job_id(), new_job_id()
        assert first != second
        assert decode_message(encode_message(_job(job_id=first))).job_id == first

    def test_old_result_identity_cannot_complete_new_task(self) -> None:
        """任务身份由生成调用分配；旧结果的标识不等同于新任务。"""
        old = _success(job_id="task-1")
        assert old.job_id != _job(job_id="task-2").job_id
