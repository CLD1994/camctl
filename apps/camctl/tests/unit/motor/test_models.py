"""专属发送事实的合法状态组合与可靠未知分区。"""
from dataclasses import replace

import pytest

from camctl.motor import models


@pytest.mark.parametrize("outcome,intent,finished,written,error_number", [
    ("PENDING", 100, None, None, None),
    ("NOT_SENT", 100, 101, None, None),
    ("NOT_SENT", None, 101, None, None),
    ("WRITTEN", 100, 101, 80, None),
    ("FAILED", 100, 101, 0, 32),
    ("FAILED", 100, 101, 2, None),
    ("UNCONFIRMED", 100, 101, None, None),
])
def test_send_facts_accept_supported_combinations(outcome, intent, finished, written, error_number):
    assert hasattr(models, "SendFacts")
    facts = models.SendFacts(
        notification_id=1, action_id=2, intent_at=intent,
        intent_operation_key="op" if intent is not None else None,
        outcome=models.SendOutcome[outcome], written_bytes=written,
        errno=error_number, finished_at=finished,
    )
    assert facts.outcome.name == outcome


@pytest.mark.parametrize("updates", [
    {"intent_at": None}, {"intent_operation_key": None}, {"finished_at": 101},
    {"written_bytes": 0}, {"errno": 32},
])
def test_pending_facts_reject_unresolved_or_fabricated_result(updates):
    assert hasattr(models, "SendFacts")
    fields = dict(notification_id=1, action_id=2, intent_at=100,
                  intent_operation_key="op", outcome=models.SendOutcome.PENDING,
                  written_bytes=None, errno=None, finished_at=None)
    fields.update(updates)
    with pytest.raises(ValueError):
        models.SendFacts(**fields)


@pytest.mark.parametrize("outcome,written,error_number", [
    ("WRITTEN", None, None), ("WRITTEN", 0, None), ("WRITTEN", 80, 32),
    ("FAILED", None, None), ("FAILED", -1, None), ("FAILED", True, None),
    ("UNCONFIRMED", 0, None), ("UNCONFIRMED", None, 32),
    ("NOT_SENT", 0, None), ("NOT_SENT", None, 32),
])
def test_finished_facts_reject_inconsistent_write_facts(outcome, written, error_number):
    assert hasattr(models, "SendFacts")
    with pytest.raises(ValueError):
        models.SendFacts(1, 2, 100, "op", models.SendOutcome[outcome], written, error_number, 101)
