"""分段选择的诊断回执只分页读取原已提交事务。"""

import sqlite3

import pytest

from camctl.contracts.values import new_operation_key
from camctl.contracts.history_values import TransactionRange
from camctl.contracts.workflow_errors import item_error_id
from camctl.history.events import load_event_registry
from camctl.outputs.sources import ItemBasis, ItemStatus, SelectedItem
from camctl.persistence.repositories.outputs import _item_values
from camctl.persistence.repositories.segment_selections import (
    SegmentSelectionDiagnosticRef, read_original_selection_error_page,
)
from camctl.persistence.transaction import _encode_body, event_envelope, row_change, update_change

from .test_sources import _seed_environment


@pytest.mark.asyncio
async def test_original_error_receipt_crosses_page_boundary_without_current_selection_reads(tmp_path):
    _, owned = _seed_environment(tmp_path)
    try:
        requested = list(range(990001, 990130))
        key = new_operation_key()
        ref = SegmentSelectionDiagnosticRef(key, TransactionRange(2, 2, 4), 17, 11)
        event_spec = load_event_registry()["events"]["TARGETS_FIXED"]
        owned.connection.execute("BEGIN IMMEDIATE")
        owned.connection.execute("INSERT INTO history_transactions(id,operation_key,first_event_id,last_event_id)"
                                 " VALUES(2,?,2,4)", (key,))
        for event_id, identities in ((2, requested[:128]), (3, requested[128:])):
            rows = tuple(row_change("obtain_items", identity, _item_values(17, SelectedItem(
                ItemBasis.EXPLICIT, ItemStatus.FAILED, requested_output_id=identity,
                error_code=item_error_id("obtain_items", "output_not_found"),
                error_details={"output_id": str(identity)}))) for identity in identities)
            event = event_envelope(event_id, 2, event_spec["id"], event_spec["branches"]["OBTAIN"]["reason"],
                                   rows, 123)
            owned.connection.execute("INSERT INTO history_events VALUES(?,?,?,?,?,?,?,?)",
                (event_id, 2, event.event_type, 1, 123, 2, None, _encode_body(event)))
        final = event_envelope(4, 2, event_spec["id"], event_spec["branches"]["OBTAIN"]["reason"],
            (update_change("obtain_source_selections", 17,
                {"status": 1, "error_code": None, "error_details_json": None},
                {"status": 2, "error_code": None, "error_details_json": None}),), 123)
        owned.connection.execute("INSERT INTO history_events VALUES(?,?,?,?,?,?,?,?)",
            (4, 2, final.event_type, 1, 123, 2, None, _encode_body(final)))
        owned.connection.commit()
        def authorize(operation, table, _column, _database, _trigger):
            if operation == sqlite3.SQLITE_READ and table in ("obtain_items", "obtain_source_selections"):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        owned.connection.set_authorizer(authorize)
        cursor = None
        counts, failures = [], []
        while True:
            page = read_original_selection_error_page(ref, cursor, owned)
            counts.append(len(page.items))
            failures.extend(page.items)
            if page.exhausted:
                break
            cursor = page.next_cursor
        assert counts == [128, 1, 0]
        assert len({failure["id"] for failure in failures}) == 129
        assert {failure["error"].code for failure in failures} == {"output_not_found"}
        assert [int(failure["error"].details["output_id"]) for failure in failures] == requested
    finally:
        owned.connection.set_authorizer(None)
        owned.connection.close()
