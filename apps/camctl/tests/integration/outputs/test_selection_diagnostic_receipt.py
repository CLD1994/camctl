"""原分段选择诊断跨真实事务分页，读取期间不依赖当前选择投影。"""

import sqlite3

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.segment_selections import FixSegmentSelection, fix_segment_selection, read_original_selection_error_page

from .test_segment_default_scope import _source, _obtain


@pytest.mark.asyncio
async def test_original_error_receipt_crosses_page_boundary_without_current_selection_reads(tmp_path):
    owned, capture, source = await _source(tmp_path, indexes=(7,))
    try:
        requested = list(range(990001, 990130))
        action, selection = _obtain(owned, capture, source, requested, 2)
        key = new_operation_key()
        fixed = fix_segment_selection(FixSegmentSelection(selection, capture.wall_us()), key, owned)
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
        ref = fixed.value.diagnostic_ref
        assert ref.operation_key == key and ref.selection_id == selection and ref.action_id == action
        assert owned.connection.execute('SELECT COUNT(*) FROM obtain_items WHERE selection_id=?', (selection,)).fetchone() == (129,)
        def authorize(operation, table, _column, _database, _trigger):
            if operation == sqlite3.SQLITE_READ and table in ('obtain_items', 'obtain_source_selections'):
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
        assert len({failure['id'] for failure in failures}) == 129
        assert {failure['error'].code for failure in failures} == {'output_not_found'}
        assert [int(failure['error'].details['requested_output_id']) for failure in failures] == requested
    finally:
        owned.connection.set_authorizer(None)
        owned.connection.close()
