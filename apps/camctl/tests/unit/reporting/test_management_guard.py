"""报告完整状态规则校验部分变更前后的事实。"""

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import EventContext, EventValidationError
from camctl.persistence.repositories.validation import build_business_rules


@pytest.fixture
def report_guard():
    return build_business_rules()["report"]


def _facts(**overrides):
    return {"status": 1, "size_bytes": None, "sha256": None,
            "publication_count": 0, "last_published_event_id": None,
            "last_error_json": None, **overrides}


@pytest.mark.parametrize("reason,facts,after", [
    (2, _facts(status=4, size_bytes=6, sha256="a" * 64, publication_count=1, last_published_event_id=8),
     {"status": 2}),
    (2, _facts(), {"size_bytes": 6, "sha256": "a" * 64}),
    (3, _facts(status=2, size_bytes=6, sha256="a" * 64), {"last_error_json": None}),
    (5, _facts(status=2, size_bytes=6, sha256="a" * 64), {"last_error_json": {"reason": "failure"}}),
    (6, _facts(), {"status": 2, "size_bytes": 6, "sha256": "a" * 64}),
])
def test_report_guard_validates_full_branch_states_when_changed_fields_are_partial(report_guard, reason, facts, after):
    event = EventEnvelope(10, 2, 28, 1, 1, 2, None, reason, {}, (
        RowChange("reports", 1, RowImage(True, {k: facts[k] for k in after}), RowImage(True, after)),
    ))
    context = EventContext(TransactionRange(2, 10, 10), {("reports", 1): ("report", 1)},
                           {"reports": {1: facts}})
    with pytest.raises(EventValidationError):
        report_guard(event, context)


def test_report_guard_accepts_status_only_failure_with_existing_error(report_guard):
    facts = _facts(status=3, size_bytes=6, sha256="a" * 64, last_error_json={"reason": "failure"})
    event = EventEnvelope(10, 2, 28, 1, 1, 2, None, 5, {}, (
        RowChange("reports", 1, RowImage(True, {"status": 3}), RowImage(True, {"status": 5})),
    ))
    context = EventContext(TransactionRange(2, 10, 10), {("reports", 1): ("report", 1)},
                           {"reports": {1: facts}})
    report_guard(event, context)
