"""报告更新守卫的状态和事实边界；不访问文件或状态库。"""

from __future__ import annotations

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import EventContext, EventValidationError
from camctl.reporting.policy import _report_guard
from camctl.reporting.policy import validate_report_publication_result
from camctl.contracts.values import ConsistencyError
from camctl.host_files.handoff import PublishResult, PublishStage
from camctl.host_files.io import DirectorySyncStage


def _facts(**overrides):
    return {"status": 1, "size_bytes": None, "sha256": None,
            "publication_count": 0, "last_published_event_id": None,
            "last_error_json": None, **overrides}


def _guard(reason, facts, after):
    event = EventEnvelope(10, 2, 28, 1, 1, 2, None, reason, {}, (
        RowChange("reports", 1, RowImage(True, {k: facts[k] for k in after}),
                  RowImage(True, after)),
    ))
    context = EventContext(TransactionRange(2, 10, 10), {("reports", 1): ("report", 1)},
                           {"reports": {1: facts}})
    _report_guard(event, context)


@pytest.mark.parametrize("field,bad", [
    ("size_bytes", True), ("size_bytes", -1), ("size_bytes", 1.5),
    ("size_bytes", "1"), ("size_bytes", 2**63),
    ("sha256", None), ("sha256", "a" * 63), ("sha256", "A" * 64),
    ("sha256", "g" * 64), ("sha256", True),
])
def test_prepare_guard_rejects_unreliable_bytes(field, bad):
    after = {"status": 2, "size_bytes": 6, "sha256": "a" * 64, "last_error_json": None}
    after[field] = bad
    with pytest.raises(EventValidationError):
        _guard(2, _facts(), after)


@pytest.mark.parametrize("bad", [None, "error", [], {"reason": float("nan")}])
def test_failure_guard_requires_persistable_error_object(bad):
    with pytest.raises(EventValidationError):
        _guard(5, _facts(), {"status": 5, "last_error_json": bad})


@pytest.mark.parametrize("after", [
    {"status": 4, "publication_count": 2, "last_published_event_id": 10, "last_error_json": None},
    {"status": 4, "publication_count": 1, "last_published_event_id": 9, "last_error_json": None},
    {"status": 4, "publication_count": True, "last_published_event_id": 10, "last_error_json": None},
])
def test_publish_guard_requires_single_success_at_current_event(after):
    with pytest.raises(EventValidationError):
        _guard(4, _facts(status=3, size_bytes=6, sha256="a" * 64), after)


@pytest.mark.parametrize("facts", [
    _facts(status=True), _facts(status=99), _facts(size_bytes=6),
    _facts(publication_count=True), _facts(publication_count=-1),
    _facts(publication_count=1), _facts(last_published_event_id=1),
    _facts(publication_count=1, last_published_event_id=1),
])
def test_guard_rejects_invalid_original_management_facts(facts):
    with pytest.raises(EventValidationError):
        _guard(5, facts, {"status": 5, "last_error_json": {"reason": "failure"}})


@pytest.mark.parametrize("field,value", [("size_bytes", 7), ("sha256", "b" * 64)])
def test_prepare_guard_keeps_first_bytes_after_failure(field, value):
    facts = _facts(status=5, size_bytes=6, sha256="a" * 64, last_error_json={"reason": "sync failure"})
    after = {"status": 2, "size_bytes": 6, "sha256": "a" * 64, "last_error_json": None}
    after[field] = value
    with pytest.raises(EventValidationError):
        _guard(2, facts, after)


def test_publish_guard_accepts_next_success_after_previous_publication():
    _guard(4, _facts(status=3, size_bytes=6, sha256="a" * 64,
                    publication_count=1, last_published_event_id=8, last_error_json={"reason": "old failure"}),
           {"status": 4, "publication_count": 2, "last_published_event_id": 10, "last_error_json": None})


def test_intent_does_not_clear_error_before_handoff_success():
    with pytest.raises(EventValidationError):
        _guard(3, _facts(status=5, size_bytes=6, sha256="a" * 64, last_error_json={"reason": "old failure"}),
               {"status": 3, "last_error_json": None})


@pytest.mark.parametrize("old,new", [(True, 1), (False, 0), (1, True), (0, False)])
def test_intent_guard_cannot_change_error_json_types(old, new):
    with pytest.raises(EventValidationError):
        _guard(3, _facts(status=5, size_bytes=6, sha256="a" * 64,
                        last_error_json={"context": [{"value": old}]}),
               {"status": 3, "last_error_json": {"context": [{"value": new}]}})


@pytest.mark.parametrize("result", [
    None,
    PublishResult(PublishStage.NOT_MOVED, DirectorySyncStage.NOT_ATTEMPTED, False, "missing"),
    PublishResult(PublishStage.UNKNOWN, DirectorySyncStage.NOT_ATTEMPTED, False, "unknown"),
    PublishResult(PublishStage.MOVED, DirectorySyncStage.FAILED, True, "sync failed"),
    PublishResult(PublishStage.MOVED, DirectorySyncStage.NOT_ATTEMPTED, True, None),
    PublishResult(PublishStage.MOVED, DirectorySyncStage.SYNCED, False, None),
    PublishResult(PublishStage.MOVED, DirectorySyncStage.SYNCED, 1, None),
    PublishResult(PublishStage.MOVED, DirectorySyncStage.SYNCED, True, ""),
    PublishResult("moved", DirectorySyncStage.SYNCED, True, None),
    PublishResult(PublishStage.MOVED, "synced", True, None),
])
def test_publication_result_requires_complete_handoff(result):
    with pytest.raises(ConsistencyError):
        validate_report_publication_result(result)


@pytest.mark.parametrize("directory", [DirectorySyncStage.SYNCED, DirectorySyncStage.UNSUPPORTED])
def test_publication_result_accepts_success_under_existing_platform_semantics(directory):
    validate_report_publication_result(PublishResult(PublishStage.MOVED, directory, True, None))


def test_publication_result_rejects_non_enum_directory_with_equality_behavior():
    from unittest.mock import ANY
    with pytest.raises(ConsistencyError):
        validate_report_publication_result(PublishResult(PublishStage.MOVED, ANY, True, None))


def test_freeze_cannot_invent_file_processing_error():
    after = {**_facts(), "frozen_event_id": 1, "from_wm": 0, "to_wm": 2,
             "format_version": 1, "last_error_json": {"reason": "no file work yet"}}
    event = EventEnvelope(10, 2, 28, 1, 1, 2, None, 1, {}, (
        RowChange("reports", 1, RowImage(False, {}), RowImage(True, after)),
    ))
    context = EventContext(TransactionRange(2, 10, 10), {("reports", 1): ("report", 1)}, {})
    with pytest.raises(EventValidationError):
        _report_guard(event, context)


@pytest.mark.parametrize("status", [4, 5])
def test_management_validation_keeps_status_rules_after_enum_reconstruction(monkeypatch, status):
    from enum import IntEnum
    from unittest.mock import create_autospec
    from camctl.reporting import models

    rebuilt = IntEnum("RebuiltReportStatus", {member.name: member.value for member in models.ReportStatus})
    decoder = create_autospec(models.decode_member, return_value=rebuilt(status))
    monkeypatch.setattr(models, "decode_member", decoder)
    with pytest.raises(ConsistencyError):
        models.validate_report_management(_facts(status=status, size_bytes=6, sha256="a" * 64))


def test_intent_guard_converts_json_comparison_recursion_error(monkeypatch):
    from unittest.mock import create_autospec
    from camctl.reporting import policy

    comparator = create_autospec(policy.json_equal, side_effect=RecursionError("comparison depth"))
    monkeypatch.setattr(policy, "json_equal", comparator)
    with pytest.raises(EventValidationError) as raised:
        _guard(3, _facts(status=5, size_bytes=6, sha256="a" * 64,
                        last_error_json={"reason": "old failure"}),
               {"status": 3, "last_error_json": {"reason": "old failure"}})
    assert isinstance(raised.value.__cause__, RecursionError)
