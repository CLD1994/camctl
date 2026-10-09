"""有限核实原申请恢复：录像空响应与集合完整响应分别解释。"""

from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import PendingResultCheckClose, resume_result_check_closes
from camctl.capture.models import ResultRunClose, ResultSetPhase, ResultSetSave
from camctl.contracts.values import ConsistencyError, OperationKey
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    ResultSetDisposition,
    ResultSetOutcome,
)

_ACTION_ID = 3
_OTHER_ID = 8
_DECIDED_AT = 1_750_000_000_000_007
_KEY = OperationKey("a" * 32)
_OTHER_KEY = OperationKey("b" * 32)


def _request(kind, action_id=_ACTION_ID):
    if kind == "record":
        return ResultRunClose(action_id=action_id, occurred_at=_DECIDED_AT)
    # 本分区不携带采集结果和公共错误，因此不需要读取包资源。
    return ResultSetSave(
        action_id=action_id, occurred_at=_DECIDED_AT,
        phase=ResultSetPhase.UNCONFIRMED, contract="task_scope_files",
        observation={"reason": "attempts_exhausted"},
    )


def _repository():
    repository = create_autospec(CaptureRepository, instance=True, spec_set=True)
    # 两个真实端口都提供合法 typed 返回；错误路由由原申请协议断言发现。
    repository.close_unconfirmed_result_run.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    repository.close_result_check_unconfirmed.return_value = _set_response()
    return repository


def _set_response(disposition=ResultSetDisposition.SAVED):
    return DbOutcome(
        DbOutcomeKind.COMPLETED,
        value=ResultSetOutcome(disposition, result_set_state=4, completion_basis=1),
    )


def _assert_original_write(repository, kind, holder, owned):
    """仓储副作用必须沿原端口、完整申请、key 和 Owned。"""
    if kind == "record":
        called = repository.close_unconfirmed_result_run
        repository.close_result_check_unconfirmed.assert_not_called()
    else:
        called = repository.close_result_check_unconfirmed
        repository.close_unconfirmed_result_run.assert_not_called()
    called.assert_called_once_with(holder.request, holder.key, owned)
    request, key, actual_owned = called.call_args.args
    assert request is holder.request
    assert key is holder.key
    assert actual_owned is owned
    assert request.occurred_at == _DECIDED_AT


def test_record_completed_without_value_releases_original_holder():
    owned = object()
    repository = _repository()
    repository.close_unconfirmed_result_run.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    holder = PendingResultCheckClose(_KEY, _request("record"))
    pending = {_ACTION_ID: holder}

    resume_result_check_closes(owned, pending_result_closes=pending, capture=repository)

    assert pending == {}
    _assert_original_write(repository, "record", holder, owned)


@pytest.mark.parametrize("kind", [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK])
def test_record_unreliable_save_preserves_same_holder_and_original_request(kind):
    owned = object()
    repository = _repository()
    receipt = DbOutcome(kind, error=ConsistencyError("原保存尚未可靠"))
    repository.close_unconfirmed_result_run.return_value = receipt
    repository.close_result_check_unconfirmed.return_value = receipt
    holder = PendingResultCheckClose(_KEY, _request("record"))
    pending = {_ACTION_ID: holder}

    with pytest.raises(ConsistencyError):
        resume_result_check_closes(owned, pending_result_closes=pending, capture=repository)

    assert pending == {_ACTION_ID: holder}
    assert pending[_ACTION_ID] is holder
    _assert_original_write(repository, "record", holder, owned)


@pytest.mark.parametrize("disposition", [ResultSetDisposition.SAVED, ResultSetDisposition.ALREADY])
def test_result_set_complete_response_releases_original_holder(disposition):
    owned = object()
    repository = _repository()
    repository.close_result_check_unconfirmed.return_value = _set_response(disposition)
    holder = PendingResultCheckClose(_KEY, _request("set"))
    pending = {_ACTION_ID: holder}

    resume_result_check_closes(owned, pending_result_closes=pending, capture=repository)

    assert pending == {}
    _assert_original_write(repository, "set", holder, owned)


def test_result_set_completed_without_required_value_keeps_holder_and_rejects():
    owned = object()
    repository = _repository()
    repository.close_result_check_unconfirmed.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    holder = PendingResultCheckClose(_KEY, _request("set"))
    pending = {_ACTION_ID: holder}

    with pytest.raises(ConsistencyError):
        resume_result_check_closes(owned, pending_result_closes=pending, capture=repository)

    assert pending[_ACTION_ID] is holder
    _assert_original_write(repository, "set", holder, owned)


@pytest.mark.parametrize("kind", [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK])
def test_result_set_unreliable_save_preserves_same_holder(kind):
    owned = object()
    repository = _repository()
    repository.close_result_check_unconfirmed.return_value = DbOutcome(
        kind, error=ConsistencyError("原集合申请尚未可靠"))
    holder = PendingResultCheckClose(_KEY, _request("set"))
    pending = {_ACTION_ID: holder}

    with pytest.raises(ConsistencyError):
        resume_result_check_closes(owned, pending_result_closes=pending, capture=repository)

    assert pending[_ACTION_ID] is holder
    _assert_original_write(repository, "set", holder, owned)


@pytest.mark.parametrize("kind", ["record", "set"])
def test_action_filter_replays_only_the_selected_original_request(kind):
    owned = object()
    repository = _repository()
    repository.close_unconfirmed_result_run.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    repository.close_result_check_unconfirmed.return_value = _set_response()
    selected = PendingResultCheckClose(_KEY, _request(kind))
    other = PendingResultCheckClose(_OTHER_KEY, _request(kind, _OTHER_ID))
    pending = {_OTHER_ID: other, _ACTION_ID: selected}

    resume_result_check_closes(
        owned, pending_result_closes=pending, action_id=_ACTION_ID, capture=repository)

    assert pending == {_OTHER_ID: other}
    assert pending[_OTHER_ID] is other
    _assert_original_write(repository, kind, selected, owned)


@pytest.mark.parametrize("kind", ["record", "set"])
def test_original_request_identity_mismatch_rejects_before_any_write(kind):
    owned = object()
    repository = _repository()
    holder = PendingResultCheckClose(_KEY, _request(kind))
    pending = {_OTHER_ID: holder}

    with pytest.raises(ConsistencyError):
        resume_result_check_closes(
            owned, pending_result_closes=pending, action_id=_OTHER_ID, capture=repository)

    assert pending[_OTHER_ID] is holder
    assert repository.mock_calls == []


def test_action_filter_without_matching_holder_does_not_write():
    owned = object()
    repository = _repository()
    holder = PendingResultCheckClose(_KEY, _request("record"))
    pending = {_ACTION_ID: holder}

    resume_result_check_closes(
        owned, pending_result_closes=pending, action_id=_OTHER_ID, capture=repository)

    assert pending[_ACTION_ID] is holder
    assert repository.mock_calls == []


def test_shared_collection_routes_record_and_result_set_requests_independently():
    owned = object()
    repository = _repository()
    repository.close_unconfirmed_result_run.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    repository.close_result_check_unconfirmed.return_value = _set_response()
    record = PendingResultCheckClose(_KEY, _request("record"))
    result_set = PendingResultCheckClose(_OTHER_KEY, _request("set", _OTHER_ID))
    pending = {_ACTION_ID: record, _OTHER_ID: result_set}

    resume_result_check_closes(owned, pending_result_closes=pending, capture=repository)

    assert pending == {}
    repository.close_unconfirmed_result_run.assert_called_once_with(record.request, record.key, owned)
    repository.close_result_check_unconfirmed.assert_called_once_with(result_set.request, result_set.key, owned)
