"""检查副本复用经真实拍摄发布、取回工厂与文件读取验证。"""

from dataclasses import replace

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.outputs.segment_copy_reuse import bound_waiters_at_publication, read_bound_check_copy
from camctl.outputs.obtain_flow import advance_obtain
from camctl.outputs.work_files import (
    WorkFileContext, WorkFileLimits, WorkFileSingleOutcome, clean_one_work_file,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository

from ..support.bootstrap_consumers import TransactionFault
from ..support.output_segments import CONTENT as _CONTENT, checked_segment_world, paused_local_read, segment_world

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("mode", ["default", "preview"])
async def test_only_obtains_waiting_at_the_source_publication_boundary_are_bound(tmp_path, mode):
    async with segment_world(tmp_path, indexes=(1, 2)) as world:
        owned, source_action_id = world.owned, world.action_id
        early_action_id = await world.accept_obtain(mode, 2)
        await world.advance()
        early_selection_id = world.selection(early_action_id)
        await world.publish()
        published_event_id = owned.connection.execute(
            "SELECT published_event_id FROM segment_sets WHERE action_id=?"
            " AND published_event_id IS NOT NULL ORDER BY version DESC LIMIT 1",
            (source_action_id,),
        ).fetchone()[0]
        late_action_id = await world.accept_obtain("default", 3)
        await world.advance()
        late_selection_id = world.selection(late_action_id)
        waiters = tuple(bound_waiters_at_publication(source_action_id, published_event_id, owned))
        assert [(item.action_id, item.selection_id) for item in waiters] == [
            (early_action_id, early_selection_id)]
        assert (late_action_id, late_selection_id) not in {
            (item.action_id, item.selection_id) for item in waiters}
        assert owned.connection.execute("SELECT COUNT(*) FROM segment_inspections").fetchone()[0] == 0
        kind = enum_for("outputs.kind")
        selected_kind = kind.PREVIEW if mode == "preview" else kind.ORIGINAL
        source_file_id, = owned.connection.execute(
            "SELECT device_file_id FROM outputs WHERE source_action_id=? AND kind=?"
            " ORDER BY id LIMIT 1", (source_action_id, int(selected_kind)),
        ).fetchone()
        assert read_bound_check_copy(
            source_action_id=source_action_id, source_device_file_id=source_file_id,
            obtain_action_id=early_action_id, selection_id=early_selection_id,
            owned=owned) is None


@pytest.mark.parametrize("scenario", [
    "success", "fallback_unknown_before_commit", "fallback_unknown_after_commit", "device_unavailable",
])
async def test_bound_obtains_fall_back_to_same_device_copy_after_local_read_failure(
        tmp_path, capture_resources, monkeypatch, scenario):
    async with checked_segment_world(tmp_path, capture_resources, monkeypatch,
            early_waiters=(20, 21), unavailable=scenario == "device_unavailable") as world:
        owned = world.owned
        early = world.early
        input_file_id, source_file_id = world.input_file_id, world.source_file_id
        staging, directories = world.directories.staging, world.directories
        executor, obtain_runtime, device_reader = world.executor, world.obtain, world.driver
        fallback_keys = []
        save_fallback = OutputsRepository.fallback_read_source

        def fallback_with_unknown_receipt(repository, request, key, connection):
            fallback_keys.append(key)
            if scenario.startswith("fallback_unknown") and len(fallback_keys) == 1:
                fault = TransactionFault(connection.connection,
                    after_commit=scenario == "fallback_unknown_after_commit")
                outcome = save_fallback(repository, request, key, replace(connection, connection=fault))
                assert fault.commit_calls == 1 and outcome.kind is DbOutcomeKind.UNKNOWN
                return outcome
            return save_fallback(repository, request, key, connection)

        monkeypatch.setattr(OutputsRepository, "fallback_read_source", fallback_with_unknown_receipt)
        assert not OutputsRepository().load_work_file_state(
            input_file_id, owned).owner_finished

        # 真正的文件线程停在首段，授权和两个独立目标已经由正式流程保存。
        async with paused_local_read(world, monkeypatch, fail_after_one=True) as (gate, advancing):
            try:
                granted = owned.connection.execute(
                    "SELECT c.id,c.source_intermediate_file_id,c.source_device_file_id,"
                    " c.target_file_id,d.id FROM file_copies c"
                    " JOIN deliveries d ON d.id=c.delivery_id"
                    " WHERE d.action_id IN (?,?) ORDER BY d.action_id",
                    (early[0][0], early[1][0]),
                ).fetchall()
                assert len(granted) == 2
                assert all(row[1] == input_file_id and row[2] is None for row in granted)
                assert len({row[3] for row in granted}) == 2
                assert len({row[4] for row in granted}) == 2
            finally:
                gate.release.set()
            if scenario.startswith("fallback_unknown"):
                with pytest.raises(ConsistencyError, match="读取源切换事务"):
                    await advancing
                assert len(fallback_keys) == 1
                event_type = 22
                from camctl.history.events import reason_for_branch

                fallback_reason = reason_for_branch("COPY_CHANGED", "SOURCE_FALLBACK")
                fallback_events = owned.connection.execute(
                    "SELECT COUNT(*) FROM history_events WHERE event_type=?"
                    " AND json_extract(body_json,'$.reason')=?",
                    (event_type, fallback_reason),
                ).fetchone()[0]
                if scenario == "fallback_unknown_before_commit":
                    assert fallback_events == 0
                    assert owned.connection.execute(
                        "SELECT source_intermediate_file_id,source_device_file_id"
                        " FROM file_copies WHERE id=?", (granted[0][0],),
                    ).fetchone() == (input_file_id, None)
                else:
                    assert fallback_events == 1
                    assert owned.connection.execute(
                        "SELECT source_intermediate_file_id,source_device_file_id"
                        " FROM file_copies WHERE id=?", (granted[0][0],),
                    ).fetchone() == (None, source_file_id)
            else:
                await advancing
        for _ in range(4):
            await advance_obtain(obtain_runtime)
            statuses = [owned.connection.execute(
                "SELECT status FROM actions WHERE id=?", (action_id,),
            ).fetchone()[0] for action_id, _selection_id in early]
            if all(status != 2 for status in statuses):
                break
        assert device_reader.offsets == []
        switched_copy = owned.connection.execute(
            "SELECT source_intermediate_file_id,source_device_file_id,committed_bytes"
            " FROM file_copies WHERE id=?", (granted[0][0],),
        ).fetchone()
        assert switched_copy == (None, source_file_id, 4)
        retry_run = owned.connection.execute(
            "SELECT attempts_used,max_attempts_used,retry_wait_required"
            " FROM operation_runs WHERE copy_id=? AND kind=3", (granted[0][0],),
        ).fetchone()
        assert retry_run == (1, 3, 1)
        from camctl.history.events import reason_for_branch

        fallback_reason = reason_for_branch("COPY_CHANGED", "SOURCE_FALLBACK")
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM history_events WHERE event_type=22"
            " AND json_extract(body_json,'$.reason')=?", (fallback_reason,),
        ).fetchone()[0] == 1
        if scenario == "fallback_unknown_before_commit":
            assert len(fallback_keys) == 2
            assert fallback_keys[0] == fallback_keys[1]
        else:
            assert len(fallback_keys) == 1

        # 换源后仍遵守原 READ_FILE 重试间隔，到时沿原机会与尝试恢复。
        world.clock['mono'] += 5_000_000_000
        if scenario == "device_unavailable":
            for _ in range(4):
                await advance_obtain(obtain_runtime)
            assert device_reader.offsets == [4]
            waiting_run = owned.connection.execute(
                "SELECT status,attempts_used,max_attempts_used,retry_wait_required"
                " FROM operation_runs WHERE copy_id=? AND kind=3", (granted[0][0],),
            ).fetchone()
            assert waiting_run == (2, 2, 3, 1)
            world.clock['mono'] += 5_000_000_000
            for _ in range(4):
                await advance_obtain(obtain_runtime)
                statuses = [owned.connection.execute(
                    "SELECT status FROM actions WHERE id=?", (action_id,),
                ).fetchone()[0] for action_id, _selection_id in early]
                if all(status != 2 for status in statuses):
                    break
            action_status = enum_for("actions.status")
            assert statuses == [
                int(action_status.FAILED), int(action_status.SUCCEEDED)]
            run_status = enum_for("operation_runs.status")
            failed_run = owned.connection.execute(
                "SELECT status,attempts_used,max_attempts_used,retry_wait_required"
                " FROM operation_runs WHERE copy_id=? AND kind=3", (granted[0][0],),
            ).fetchone()
            assert failed_run == (int(run_status.FAILED), 3, 3, 0)
            assert device_reader.offsets == [4, 4]
            attempt_errors = owned.connection.execute(
                "SELECT a.status,json_extract(a.error_json,'$.code')"
                " FROM operation_attempts a"
                " JOIN operation_runs r ON r.id=a.run_id"
                " WHERE r.copy_id=? ORDER BY a.attempt_no", (granted[0][0],),
            ).fetchall()
            assert len(attempt_errors) == 3
            assert all(row[0] == 3 for row in attempt_errors)
            assert [row[1] for row in attempt_errors] == [
                "local_read_failed", "device_error", "device_error"]
            await advance_obtain(obtain_runtime)
            assert owned.connection.execute(
                "SELECT attempts_used FROM operation_runs WHERE copy_id=? AND kind=3",
                (granted[0][0],),
            ).fetchone()[0] == 3
            return

        for _ in range(4):
            await advance_obtain(obtain_runtime)
            statuses = [owned.connection.execute(
                "SELECT status FROM actions WHERE id=?", (action_id,),
            ).fetchone()[0] for action_id, _selection_id in early]
            if all(status == 3 for status in statuses):
                break
        assert all(status == 3 for status in statuses)

        deliveries = owned.connection.execute(
            "SELECT i.id,d.id,c.id,c.source_intermediate_file_id,"
            " c.source_device_file_id,c.target_file_id"
            " FROM obtain_items i JOIN deliveries d ON d.id=i.delivery_id"
            " JOIN file_copies c ON c.delivery_id=d.id"
            " WHERE i.selection_id IN (?,?) ORDER BY i.selection_id",
            (early[0][1], early[1][1]),
        ).fetchall()
        assert len(deliveries) == 2
        assert len({row[1] for row in deliveries}) == 2
        assert len({row[2] for row in deliveries}) == 2
        assert deliveries[0][3] is None and deliveries[0][4] == source_file_id
        assert deliveries[1][3] == input_file_id and deliveries[1][4] is None
        assert len({row[5] for row in deliveries}) == 2
        assert device_reader.offsets == [4]
        resumed_run = owned.connection.execute(
            "SELECT r.status,r.attempts_used,r.max_attempts_used,r.retry_wait_required"
            " FROM operation_runs r WHERE r.copy_id=? AND r.kind=3",
            (deliveries[0][2],),
        ).fetchone()
        assert resumed_run == (3, 2, 3, 0)
        device_delivery_name, = owned.connection.execute(
            "SELECT file_name FROM deliveries WHERE id=?",
            (deliveries[0][1],),
        ).fetchone()
        assert (directories.ready / device_delivery_name).read_bytes() == _CONTENT
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id JOIN file_copies c ON c.id=r.copy_id"
            " JOIN deliveries d ON d.id=c.delivery_id"
            " WHERE d.id=? AND a.status=3",
            (deliveries[0][1],),
        ).fetchone()[0] == 1

        late_action_id = await world.accept_obtain("default", 22)
        await world.advance()
        late_copy = owned.connection.execute(
            "SELECT c.source_intermediate_file_id,c.source_device_file_id FROM file_copies c"
            " JOIN deliveries d ON d.id=c.delivery_id WHERE d.action_id=?", (late_action_id,),
        ).fetchone()
        assert late_copy == (None, source_file_id)
        assert device_reader.offsets == [4, 0]

        facts = OutputsRepository().load_work_file_state(input_file_id, owned)
        assert facts.owner_finished
        assert facts.operations_stopped
        context = WorkFileContext(
            repository=OutputsRepository(),
            database=obtain_runtime.database,
            staging=staging,
            occurred_at=world.clock['wall'],
            limits=WorkFileLimits(batch_size=16, limit_per_run=16),
            executor=executor,
        )
        cleaned = await clean_one_work_file(input_file_id, context)
        assert cleaned.outcome is WorkFileSingleOutcome.DELETED
        assert not world.input_path.exists()
        retention, cleanup = owned.connection.execute(
            "SELECT retention_state,cleanup_state FROM intermediate_files WHERE id=?",
            (input_file_id,),
        ).fetchone()
        assert (retention, cleanup) == (2, 4)
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (late_action_id,),
        ).fetchone()[0] == 2
