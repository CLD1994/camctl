"""延时拍摄的集合结论消费真实异步可靠页接口。"""

import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import ListingPhase, ListingRound, _confirm_timelapse_results
from camctl.capture.models import ResultSetPhase
from camctl.capture.result_pages import ResultPageRef, SavedResultPage
from camctl.capture.result_scans import SavedResultEntries
from camctl.capture.results import CaptureAssessment, FileKind
from camctl.contracts.pages import Page
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from support.capture_database import capture_database

from .test_result_pages import _CURSOR, _TICKET, _outcome, _page


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False], ids=["complete", "explicitly-unmet"])
async def test_paged_timelapse_conclusion_preserves_first_and_last_reliable_page(mocker, complete):
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    connection.execute.return_value.fetchone.return_value = (71,)
    owned = OwnedConnection(connection,
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    repository = create_autospec(CaptureRepository, instance=True, spec_set=True)
    first_ref = ResultPageRef(_TICKET, 1, 10, _CURSOR)
    last_ref = ResultPageRef(_TICKET, 2, 11, None)
    first_page = SavedResultPage(first_ref, _page(_outcome(next_cursor=_CURSOR)), 1000)
    repository.read_result_pages.return_value = Page((first_page,), 10)
    database = capture_database(owned)
    entries = SavedResultEntries(repository, database, last_ref)
    completion = {"type": "capture_completed", "version": 1,
                  "data": {"activity_id": "71"}}
    actual_page = _page(_outcome(finalized=True, completion=completion))
    listing = ListingRound(ListingPhase.LISTED, entries, _TICKET, actual_page.outcome,
        1001, 2000, scan_complete=True, set_finalized=True,
        completion_evidence=actual_page.completion_evidence, completion_page=last_ref)
    assessment = CaptureAssessment(complete, not complete,
        () if complete else (FileKind.VIDEO,), (), (), (), file_count=2 if complete else 0)
    finish = mocker.patch("camctl.capture.handlers._finish_listing_result", autospec=True)
    runtime = SimpleNamespace(database=database, capture=repository)

    await _confirm_timelapse_results(runtime, 21, assessment, entries, None, listing)

    finish.assert_awaited_once()
    command = finish.call_args.kwargs["result_set"]
    assert command.phase is (ResultSetPhase.COMPLETE if complete else ResultSetPhase.UNSATISFIED)
    assert command.contract == "result_files_listed/v2"
    assert command.observation["first_page_event_id"] == 10
    assert command.observation["last_page_event_id"] == 11
    assert command.observation["file_count"] == (2 if complete else 0)
    if complete:
        assert command.evidence["observation"]["result_page_event_id"] == 11
    else:
        assert command.error["details"] == {"activity_id": "71", "reason": "no_outputs"}
    repository.read_result_pages.assert_called_once_with(_TICKET, None, 1, owned)
