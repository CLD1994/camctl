"""已取得的清理实际结果按原键恢复，不重做查询与删除。"""

from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from camctl.outputs.work_files import (
    WorkFileCleanupError, WorkFileSingleOutcome, clean_one_work_file, clean_work_files,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..bootstrap.test_binding_transactions import _CommitFailure
from ..support.outputs import SqliteOperationFault
from ..support.database import business_database
from .test_work_files import (
    _cancel_public_delivery, _context, _file_state, _release_ok, _work_path,
    public_file_env, public_work_env,  # noqa: F401
)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["projection", "commit_before", "commit_after"])
@pytest.mark.parametrize("delete_failed", [False, True])
@pytest.mark.parametrize("historical", [False, True])
async def test_reopen_preserves_actual_cleanup_result_and_original_key(
        public_file_env, monkeypatch, phase, delete_failed, historical):
    owned, roots, qualification = public_file_env
    file_id = qualification.target_file_id
    _cancel_public_delivery(owned, qualification)
    _release_ok(owned, file_id)
    database = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    async with business_database(database) as original_database:
        context = _context(original_database, roots)
        original_occurred_at = context.occurred_at
        target = _work_path(roots, owned, file_id)
        observe, remove = Path.stat, Path.unlink
        observations, removals, saves = [], [], []

        def stat(path, *args, **kwargs):
            if path == target:
                observations.append(path)
            return observe(path, *args, **kwargs)

        def unlink(path, *args, **kwargs):
            if path == target:
                removals.append(path)
                if delete_failed:
                    raise OSError("original unlink refused")
            return remove(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat)
        monkeypatch.setattr(Path, "unlink", unlink)
        save = context.repository.save_cleanup_result

        def fault_once(request, key, current):
            saves.append((request, key))
            if len(saves) == 1:
                connection = (SqliteOperationFault(current.connection, sqlite3.SQLITE_UPDATE, "intermediate_files")
                              if phase == "projection" else _CommitFailure(
                                  current.connection, phase == "commit_after"))
                return save(request, key, replace(current, connection=connection))
            return save(request, key, current)

        monkeypatch.setattr(context.repository, "save_cleanup_result", fault_once)

        async def clean(current):
            return (await clean_work_files(current) if historical
                    else await clean_one_work_file(file_id, current))

        try:
            with pytest.raises(WorkFileCleanupError) as error:
                await clean(context)
            assert error.value.stage == "cleanup_result_failed"
            assert len(observations) == len(removals) == len(saves) == 1
        finally:
            owned.connection.close()
    reopened = open_existing(database, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        async with business_database(database) as resumed_database:
            context = replace(context, database=resumed_database,
                              occurred_at=original_occurred_at + 1)

            result = await clean(context)

            expected = WorkFileSingleOutcome.FAILED if delete_failed else WorkFileSingleOutcome.DELETED
            if historical:
                assert (result.checked, result.cleaned, result.failed) == (1, int(not delete_failed), int(delete_failed))
                assert reopened.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (file_id,)
            else:
                assert result.outcome is expected
            assert saves == [saves[0], saves[0]], "原实际结果、时刻与操作键必须完整复用"
            request, key = saves[0]
            assert request.file_id == file_id
            assert request.occurred_at == original_occurred_at
            assert request.occurred_at != context.occurred_at
            assert reopened.connection.execute(
                "SELECT COUNT(*), MIN(e.occurred_at), MAX(e.occurred_at)"
                " FROM history_events e JOIN history_transactions t ON t.id=e.transaction_id"
                " WHERE t.operation_key=? AND e.event_type=26"
                " AND json_extract(e.body_json, '$.reason')=4", (str(key),)
            ).fetchone() == (1, request.occurred_at, request.occurred_at)
            assert len(observations) == len(removals) == 1
            assert _file_state(reopened, file_id)[:2] == (2, 5 if delete_failed else 4)
            before = tuple(reopened.connection.iterdump())
            assert (await clean_one_work_file(file_id, context)).outcome is expected
            assert tuple(reopened.connection.iterdump()) == before
    finally:
        reopened.connection.close()
