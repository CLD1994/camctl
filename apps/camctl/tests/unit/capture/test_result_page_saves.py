"""原页实际调用的保存未知时，拥有者不能开始下一页或更换输入。"""
from dataclasses import replace
from unittest.mock import create_autospec

import pytest

from camctl.capture import result_pages
from camctl.capture.result_inputs import RESULT_PAGE_CONTRACT
from camctl.contracts.values import ConsistencyError
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.operations.models import EffectState, AttemptStatus, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from .test_result_pages import _TICKET, _CURSOR, _outcome

_REGISTRY = EvidenceRegistry((RESULT_PAGE_CONTRACT, EvidenceContract("results_returned", 1, "result", frozenset())))


def _request(*, error=None, page_no=1):
    actual = replace(_outcome(next_cursor=_CURSOR), effect=EffectState.CONFIRMED,
        status=AttemptStatus.SUCCEEDED if error is None else AttemptStatus.FAILED, error=error,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})))
    return result_pages.ResultPageSave(_TICKET, page_no, None,
        validate_outcome(_TICKET, actual, _REGISTRY), 1_750_000_000_000_000)


@pytest.mark.parametrize("kind", [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK])
def test_unknown_or_rolled_back_page_keeps_original_request_until_reliable_receipt(kind):
    owner = result_pages.ResultPageSaveOwner()
    request = _request()
    pending = owner.begin(request)
    repository = create_autospec(CaptureRepository, instance=True)
    response = result_pages.ResultPageRef(_TICKET, 1, 10, _CURSOR)
    repository.save_result_page.side_effect = [DbOutcome(kind), DbOutcome(DbOutcomeKind.COMPLETED, response)]
    assert owner.save(repository, object()).kind is kind
    with pytest.raises(ConsistencyError):
        owner.take()
    with pytest.raises(ConsistencyError):
        owner.begin(_request(page_no=2))
    assert owner.pending == pending
    assert owner.save(repository, object()).value == response
    assert owner.take() == response and owner.pending is None
    assert [call.args[:2] for call in repository.save_result_page.call_args_list] == [(request, pending.key)] * 2


def test_original_page_identity_distinguishes_json_boolean_from_number():
    owner = result_pages.ResultPageSaveOwner()
    pending = owner.begin(_request(error=ErrorValue("read_failed", "device", {"partial": 1})))
    with pytest.raises(ConsistencyError):
        owner.begin(_request(error=ErrorValue("read_failed", "device", {"partial": True})))
    assert owner.pending == pending


def test_owner_retains_actual_input_when_caller_changes_its_mapping():
    request = _request(error=ErrorValue("read_failed", "device", {"bytes": 5}))
    owner = result_pages.ResultPageSaveOwner()
    owner.begin(request)
    request.outcome.outcome.error.details["bytes"] = 6
    assert owner.pending.request.outcome.outcome.error.details == {"bytes": 5}


@pytest.mark.parametrize("response", [
    result_pages.ResultPageRef(_TICKET, 2, 10, _CURSOR),
    result_pages.ResultPageRef(replace(_TICKET, run_id=10), 1, 10, _CURSOR),
    result_pages.ResultPageRef(_TICKET, 1, 0, _CURSOR),
    result_pages.ResultPageRef(_TICKET, 1, 10, None),
])
def test_response_must_match_original_attempt_page_and_next_cursor(response):
    owner = result_pages.ResultPageSaveOwner()
    pending = owner.begin(_request())
    repository = create_autospec(CaptureRepository, instance=True)
    repository.save_result_page.return_value = DbOutcome(DbOutcomeKind.COMPLETED, response)
    with pytest.raises(ConsistencyError):
        owner.save(repository, object())
    assert owner.pending == pending
