"""普通原页端口使用已保存输入，不调用形成页的整组来源核验。"""
import sqlite3
from unittest.mock import create_autospec

from camctl.persistence import fact_reads
from camctl.persistence.repositories import result_pages


def test_saved_page_decodes_actual_event_without_replaying_page_formation(monkeypatch):
    connection = create_autospec(sqlite3.Connection, instance=True)
    record = create_autospec(fact_reads.EventRecord, instance=True)
    record.event = object()
    saved, raw = object(), {'attempt_id': 2}
    read = create_autospec(fact_reads.read_event, return_value=record)
    decode = create_autospec(result_pages._decode, return_value=(saved, raw))
    formation = create_autospec(result_pages._verify_page_relations)
    monkeypatch.setattr(result_pages, 'read_event', read)
    monkeypatch.setattr(result_pages, '_decode', decode)
    monkeypatch.setattr(result_pages, '_verify_page_relations', formation)
    assert result_pages.read_saved_page(connection, 7) == (saved, raw)
    read.assert_called_once_with(connection, 7)
    decode.assert_called_once_with(record.event)
    formation.assert_not_called()
