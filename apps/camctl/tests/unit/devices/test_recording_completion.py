"""等待满足与实际等待来源分别校验，不将复制页解释为新等待。"""
from copy import deepcopy

import pytest

from camctl.devices import recording_completion


def _completion(**changes):
    return {"method": "stop_return_and_wait", "version": 1, "activity_id": "3",
        "stop_result_event_id": 7, "required_wait_ms": 5000,
        "observed_wait_ns": 5_000_000_000, "completed": True, **changes}


def _source(data, *, page_event_id=12, source_page=None):
    return recording_completion.completed_file_wait_source(data,
        page_event_id=page_event_id, activity_id="3", stop_result_event_id=7,
        required_wait_ms=5000, source_page=source_page)


@pytest.mark.parametrize("data", [
    {"file_completion": _completion()},
    {"file_completion": _completion(), "file_completion_source_page_event_id": None},
])
def test_actual_wait_page_is_its_own_source(data):
    assert _source(data) == 12


def test_reused_wait_keeps_the_original_actual_wait_page():
    completion = _completion()
    original = {"event_id": 10, "activity_id": "3", "data": {
        "file_completion": deepcopy(completion), "file_completion_source_page_event_id": None}}
    data = {"file_completion": completion, "file_completion_source_page_event_id": 10}

    assert _source(data, source_page=original) == 10


@pytest.mark.parametrize("page_event_id", [6, 7], ids=["before-stop", "same-as-stop"])
def test_actual_completed_wait_must_follow_the_original_stop_result(page_event_id):
    with pytest.raises(ValueError):
        _source({"file_completion": _completion()}, page_event_id=page_event_id)


@pytest.mark.parametrize("source_id", [6, 7], ids=["before-stop", "same-as-stop"])
def test_reused_completed_wait_source_must_follow_the_original_stop_result(source_id):
    original = {"event_id": source_id, "activity_id": "3", "data": {"file_completion": _completion()}}

    with pytest.raises(ValueError):
        _source({"file_completion": _completion(), "file_completion_source_page_event_id": source_id},
            source_page=original)


def test_unfinished_actual_wait_before_stop_has_no_completion_claim():
    assert _source({"file_completion": _completion(completed=False)}, page_event_id=6) is None


@pytest.mark.parametrize("completion", [None, _completion(completed=False),
    _completion(activity_id="4"), _completion(stop_result_event_id=8),
    _completion(required_wait_ms=4000), _completion(observed_wait_ns=4_999_999_999)])
def test_unconfirmed_or_different_wait_provides_no_source(completion):
    assert _source({"file_completion": completion}) is None


@pytest.mark.parametrize("completion", [None, _completion(completed=False),
    _completion(activity_id="4"), _completion(stop_result_event_id=8),
    _completion(required_wait_ms=4000), _completion(observed_wait_ns=4_999_999_999)])
def test_reused_wait_rejects_an_unconfirmed_or_different_current_fact(completion):
    original = {"event_id": 10, "activity_id": "3", "data": {"file_completion": _completion()}}

    with pytest.raises(ValueError):
        _source({"file_completion": completion, "file_completion_source_page_event_id": 10},
            source_page=original)


@pytest.mark.parametrize("reference", [True, "10", 0, -1, 12, 13])
def test_reused_wait_rejects_invalid_or_nonpreceding_source(reference):
    with pytest.raises(ValueError):
        _source({"file_completion": _completion(), "file_completion_source_page_event_id": reference})


@pytest.mark.parametrize("case", ["missing", "different_event", "different_activity",
    "copied_page", "different_elapsed", "different_stop", "unfinished"])
def test_reused_wait_rejects_unreliable_or_different_source(case):
    original = {"event_id": 10, "activity_id": "3", "data": {
        "file_completion": _completion(), "file_completion_source_page_event_id": None}}
    if case == "missing":
        original = None
    elif case == "different_event":
        original["event_id"] = 9
    elif case == "different_activity":
        original["activity_id"] = "4"
    elif case == "copied_page":
        original["data"]["file_completion_source_page_event_id"] = 9
    elif case == "different_elapsed":
        original["data"]["file_completion"]["observed_wait_ns"] += 1
    elif case == "different_stop":
        original["data"]["file_completion"]["stop_result_event_id"] = 8
    else:
        original["data"]["file_completion"]["completed"] = False

    with pytest.raises(ValueError):
        _source({"file_completion": _completion(), "file_completion_source_page_event_id": 10},
            source_page=original)
