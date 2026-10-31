"""来源预览读取只消费本边界内同一结果尝试的有限页。"""

from types import ModuleType, SimpleNamespace
import sqlite3
import sys
from unittest.mock import create_autospec

import pytest

from camctl.contracts.pages import Page
from camctl.operations.models import AttemptTicket
from camctl.persistence.repositories import preview_reads
from .test_original_selection import no_resource_reads
from .test_preview_facts import ORIGINALS, page


@pytest.fixture
def reader(monkeypatch):
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    # 仅提供当前分支使用的操作种类样本，不读取真实登记。
    monkeypatch.setattr(preview_reads, "enum_for", lambda key: SimpleNamespace(CHECK_CAPTURE_RESULTS=7))
    pages = ModuleType("camctl.persistence.repositories.result_pages")

    def read_pages(ticket, cursor, batch, owned):
        raise AssertionError("只有已保存且位于边界内的结果页才能读取")

    pages.read_pages = create_autospec(read_pages, side_effect=read_pages)
    monkeypatch.setitem(sys.modules, pages.__name__, pages)
    return SimpleNamespace(connection=connection, port=preview_reads.PreviewReads(connection), pages=pages)


SOURCE = {"id": 51, "last_event_id": 300, "execution_spec_json": {"video_result_mode": "single_file"}}


@pytest.mark.parametrize("originals,spec", [({}, SOURCE["execution_spec_json"]), (ORIGINALS, {})])
def test_no_missing_video_preview_does_not_read_history(reader, originals, spec):
    assert reader.port.unconfirmed({**SOURCE, "execution_spec_json": spec}, originals) == frozenset()
    reader.connection.execute.assert_not_called()


@pytest.mark.parametrize("attempt", [None, (79, 23, 2, None, None), (79, 23, 2, 200, 301)])
def test_no_complete_result_within_source_boundary_is_unconfirmed(reader, attempt):
    reader.connection.execute.return_value.fetchone.return_value = attempt

    assert reader.port.unconfirmed(SOURCE, ORIGINALS) == frozenset({41, 42})

    reader.pages.read_pages.assert_not_called()
    assert reader.connection.execute.call_args.args[1] == (51, 7, 301)


def test_consumes_same_attempt_pages_and_preserves_observed_preview(reader):
    reader.connection.execute.return_value.fetchone.return_value = (79, 23, 2, 200, 250)
    reader.pages.read_pages.side_effect = [
        Page((SimpleNamespace(page=page(complete=False, observations=({
            "preview_identity": "/DCIM/CAM_001/first.LRF", "state": "metadata_failed",
        },))),), 200),
        Page((SimpleNamespace(page=page()),), None),
    ]

    assert reader.port.unconfirmed(SOURCE, ORIGINALS) == frozenset({41})

    for call, cursor in zip(reader.pages.read_pages.call_args_list, (None, 200)):
        ticket, actual_cursor, batch, owned = call.args
        assert ticket == AttemptTicket(2, "result", "23", "results/23", 79)
        assert actual_cursor == cursor and 1 <= batch <= 128
        assert owned.connection is reader.connection


def test_saved_selection_does_not_consume_pages_after_its_boundary(reader):
    reader.connection.execute.return_value.fetchone.return_value = (79, 23, 2, 200, 250)

    assert reader.port.unconfirmed(SOURCE, ORIGINALS, before_event_id=220) == frozenset({41, 42})

    assert reader.connection.execute.call_args.args[1] == (51, 7, 220)
    reader.pages.read_pages.assert_not_called()
