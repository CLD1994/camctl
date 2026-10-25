"""候选筛选按原活动的固定基准逐批前进。"""

from dataclasses import replace
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap import capture_assembly
from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.capture.baseline_models import BaselineChunk, BaselineRef, BaselineState
from camctl.capture.result_reading import ResultReadingContext
from camctl.contracts.enums import enum_for
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError
from camctl.devices.file_identity import FileIdentity
from camctl.devices.ports import ResultDriver
from camctl.persistence.repositories.capture import CaptureRepository
from support.capture_database import capture_database
from test_result_round_adapter import _BINDING, _TICKET, _REGISTRY


pytestmark = pytest.mark.asyncio


_SCOPE = {"directories": ["/DCIM"]}


def _filter(monkeypatch, baseline):
    repository = create_autospec(CaptureRepository, instance=True)
    database = capture_database()
    ref = BaselineRef(71, BaselineState.FIXED,
        10 if baseline else None, 10 if baseline else None,
        1 if baseline else 0, len(baseline))
    repository.baseline_ref.return_value = ref
    repository.read_baseline.return_value = Page(
        (BaselineChunk(10, 1, tuple(baseline)),) if baseline else (), None)
    rows = {"device_activities": {"id": 71, "action_id": 3,
        "ownership_mode": int(enum_for("device_activities.ownership_mode").BASELINE_COMPARISON),
        "output_scope_json": _SCOPE},
        "actions": {"device_id": _BINDING.device_id, "driver_id": _BINDING.driver_id}}
    monkeypatch.setattr(capture_assembly, "row_facts", lambda db, table, row_id: rows[table])
    adapter = DriverResultListing(create_autospec(ResultDriver, instance=True),
        _BINDING, _REGISTRY, capture_repository=repository, database=database)
    context = ResultReadingContext(_TICKET, binding=_BINDING, directories=("/DCIM",))
    return adapter._result_candidates(_TICKET, _SCOPE, context), repository, context, rows


def _identity(index):
    return FileIdentity(_BINDING, f"/DCIM/{index:04}.mp4")


async def test_136_paths_select_only_ten_new_files_without_reloading_baseline(monkeypatch):
    paths = tuple(_identity(index) for index in range(136))
    new = paths[::14]
    callback, repository, context, _ = _filter(monkeypatch, tuple(path for path in paths if path not in new))

    batches = []
    for offset in range(0, 136, 32):
        batches.extend(await callback(paths[offset:offset + 32]))
    selected = tuple(batches)

    assert selected == new and len(selected) == 10
    repository.read_baseline.assert_called_once()
    assert context.baseline_chunk is None


@pytest.mark.parametrize("paths,baseline", [((), ()), ((1, 2), (1, 2)), ((1, 2), ())])
async def test_empty_and_entirely_old_pages_keep_exact_difference(monkeypatch, paths, baseline):
    callback, _, _, _ = _filter(monkeypatch, tuple(_identity(index) for index in baseline))
    assert await callback(tuple(_identity(index) for index in paths)) == tuple(
        _identity(index) for index in paths if index not in baseline)


async def test_multiple_baseline_chunks_are_read_once_as_candidates_advance(monkeypatch):
    callback, repository, _, _ = _filter(monkeypatch, (_identity(0),))
    repository.baseline_ref.return_value = BaselineRef(71, BaselineState.FIXED, 10, 20, 2, 4)
    repository.read_baseline.side_effect = [
        Page((BaselineChunk(10, 1, (_identity(0), _identity(2))),), 10),
        Page((BaselineChunk(20, 2, (_identity(4), _identity(6))),), None)]

    assert await callback((_identity(1), _identity(2))) == (_identity(1),)
    assert await callback((_identity(4), _identity(5), _identity(7))) == (_identity(5), _identity(7))
    assert [call.args[1] for call in repository.read_baseline.call_args_list] == [None, 10]


async def test_empty_nonfinal_baseline_page_does_not_hide_later_old_file(monkeypatch):
    callback, repository, _, _ = _filter(monkeypatch, (_identity(2),))
    repository.read_baseline.side_effect = [Page((), 9),
        Page((BaselineChunk(10, 1, (_identity(2),)),), None)]

    assert await callback((_identity(2), _identity(3))) == (_identity(3),)
    assert [call.args[1] for call in repository.read_baseline.call_args_list] == [None, 9]


@pytest.mark.parametrize("change", ["binding", "scope", "collecting", "changed_ref"])
async def test_mismatched_original_baseline_is_rejected(monkeypatch, change):
    callback, repository, _, rows = _filter(monkeypatch, (_identity(0),))
    if change == "binding":
        rows["actions"]["device_id"] = "other-camera"
    elif change == "scope":
        rows["device_activities"]["output_scope_json"] = {"directories": ["/other"]}
    elif change == "collecting":
        repository.baseline_ref.return_value = replace(repository.baseline_ref.return_value, state=BaselineState.COLLECTING)
    else:
        assert await callback((_identity(0),)) == ()
        repository.baseline_ref.return_value = replace(repository.baseline_ref.return_value, entry_count=2)

    with pytest.raises(ConsistencyError):
        await callback((_identity(1),))


async def test_repeated_or_foreign_candidate_cannot_advance_original_comparison(monkeypatch):
    callback, _, _, _ = _filter(monkeypatch, (_identity(0),))
    assert await callback((_identity(1),)) == (_identity(1),)
    with pytest.raises(ConsistencyError):
        await callback((_identity(1),))
