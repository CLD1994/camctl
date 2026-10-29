"""显式分段未知项沿实际票据保存 QUERY v2 后解析或失败。"""

from decimal import Decimal
from dataclasses import replace
from sqlite3 import OperationalError

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.evidence import (
    DeviceObservation,
    EvidenceContract,
    EvidenceRegistry,
)
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import (
    AttemptConfig,
    AttemptIntent,
    AttemptTarget,
    OperationKind,
)
from camctl.operations.recovery import RecoveryBoundary
from camctl.operations.models import (
    AttemptStatus,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.outputs.obtain_flow import advance_obtain
from ..support.outputs import obtain_factory, output_drivers
from ..support.capture import capture_database
from camctl.devices.drivers.registry import DriverRegistry
from .test_cleanup_query_commit_recovery import _CommitFaultConnection
from camctl.persistence.models import DbOutcomeKind

from ..operations.test_obtain_check_identity import _environment, _NOW


_EVIDENCE = EvidenceRegistry(
    (
        EvidenceContract("query_returned", 1, "query", frozenset()),
        EvidenceContract("file_presence", 1, "query", frozenset({"cleanup_item_id", "present"}), identity_field="cleanup_item_id"),
        EvidenceContract("adb_foreground_recovery", 1, "query", frozenset()),
        EvidenceContract(
            "file_presence",
            2,
            "query",
            frozenset({"obtain_item_id", "present"}),
            identity_field="obtain_item_id",
        ),
    )
)


async def _world(tmp_path, values):
    _, owned = _environment(tmp_path, host=False)
    db = owned.connection
    db.execute(
        "UPDATE actions SET status=3,execution_spec_json=? WHERE id=1",
        ('{"result_policy":"osmo360ii_record_segments_v1"}',),
    )
    db.execute(
        "UPDATE actions SET source_resolution_state=2,resolved_source_plan_id=1 WHERE id=2"
    )
    db.execute("UPDATE obtain_source_selections SET status=2")
    db.execute("UPDATE device_files SET presence_state=1")
    db.commit()

    class Query:
        def __init__(self):
            self.calls = []

        async def open_read(self, source, offset, ticket, *, idle_timeout_s):
            raise AssertionError("查询存储边界不得继续读取文件")

        async def query_file(self, request):
            self.calls.append(request)
            present = values[len(self.calls) - 1]
            observation = (
                ()
                if present is None
                else (
                    DeviceObservation(
                        "file_presence",
                        2,
                        {
                            "obtain_item_id": request.ticket.target_id,
                            "present": present,
                        },
                    ),
                )
            )
            return DeviceCallResult.from_outcome(
                CallOutcome(
                    status=AttemptStatus.FAILED
                    if present is None
                    else AttemptStatus.SUCCEEDED,
                    effect=EffectState.UNKNOWN
                    if present is None
                    else EffectState.CONFIRMED,
                    error=ErrorValue("query_unconfirmed", "device_file_access", {})
                    if present is None
                    else None,
                    settlement=Settlement(
                        SettlementBasis.OBSERVED, EvidenceValue("query_returned", 1, {})
                    ),
                    observations=observation,
                )
            )

    query = Query()
    runtime = await _query_runtime(owned, query)
    return owned, runtime, query


class _QuerySettled(Exception):
    """查询观察和成员结果已经完整保存，本专项在文件读取之前结束。"""


@pytest.fixture(autouse=True)
def query_storage_boundary(monkeypatch):
    from camctl.outputs import segment_query
    original = segment_query.settle_queried_item
    def saved(request, key, owned):
        result = original(request, key, owned)
        if result.kind is DbOutcomeKind.COMPLETED:
            return replace(result, handoff_error=_QuerySettled())
        return result
    monkeypatch.setattr(segment_query, "settle_queried_item", saved)


async def _query_runtime(owned, query, *, recovery_boundary=RecoveryBoundary.UNCONFIRMED,
                         recovery_max_event_id=None):
    registry = output_drivers(query, _EVIDENCE, read=True, query=True)
    entry = registry.entry("camctl-adb")
    registry = DriverRegistry((replace(entry, declaration=replace(entry.declaration,
        adb_foreground_recovery_operations=frozenset({"query"}))),))
    from pathlib import Path
    staging = Path(owned.metadata.staging_path)
    factory = obtain_factory(devices={"cam-1": {"driver": "camctl-adb", "cleanup": {
        "query_timeout_s": Decimal(1), "query_retry_interval_s": Decimal(0)}}},
        drivers=registry, staging=staging, ready=Path(owned.metadata.ready_path),
        processing=Path(owned.metadata.processing_path), max_query_attempts=2,
        occurred_at=lambda: _NOW + 20, monotonic_ns=lambda: 0,
        recovery_boundary=recovery_boundary,
        recovery_max_event_id=None if recovery_max_event_id is None else lambda: recovery_max_event_id)
    return factory(await capture_database(owned))


async def _round(runtime):
    try:
        await advance_obtain(runtime)
    except _QuerySettled:
        return


@pytest.mark.asyncio
@pytest.mark.parametrize("present", [True, False])
async def test_actual_v2_resolves_original_identity_or_missing_failure(
    tmp_path, present
):
    owned, runtime, query = await _world(tmp_path, [present])
    try:
        await _round(runtime)
        assert len(query.calls) == 1
        assert query.calls[0].ticket.responsibility_key == "obtain-exists/1"
        assert owned.connection.execute(
            "SELECT output_id,status FROM obtain_items"
        ).fetchone() == (1, 2 if present else 4)
        assert owned.connection.execute(
            "SELECT availability FROM outputs"
        ).fetchone() == (1 if present else 4,)
        assert owned.connection.execute(
            "SELECT presence_state FROM device_files"
        ).fetchone() == (2 if present else 3,)
        await _round(runtime)
        assert len(query.calls) == 1
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_unknown_receipt_reuses_original_result_then_remaining_budget(tmp_path):
    owned, runtime, query = await _world(tmp_path, [None, True])
    original = runtime.operations.finish_attempt
    requests = []

    def lost_once(request, key, owned):
        requests.append((request, key))
        if len(requests) == 1:
            fault = _CommitFaultConnection(owned.connection, after_commit=True)
            fault.error = OperationalError("实际已提交，回执丢失")
            saved = original(request, key, replace(owned, connection=fault))
            assert fault.actual_commits == 1
            return saved
        return original(request, key, owned)

    runtime.operations.finish_attempt = lost_once
    try:
        with pytest.raises(ConsistencyError, match="回执丢失"):
            await _round(runtime)
        await _round(runtime)
        assert requests[0] == requests[1]
        assert len(query.calls) == 2
        assert [call.ticket.attempt_id for call in query.calls] == [1, 2]
        assert owned.connection.execute(
            "SELECT output_id,status FROM obtain_items"
        ).fetchone() == (1, 2)
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_unconfirmed_budget_finishes_item_without_fabricating_missing(tmp_path):
    owned, runtime, query = await _world(tmp_path, [None, None])
    try:
        await _round(runtime)
        await _round(runtime)
        assert len(query.calls) == 2
        assert owned.connection.execute(
            "SELECT output_id,status FROM obtain_items"
        ).fetchone() == (1, 4)
        assert owned.connection.execute(
            "SELECT availability FROM outputs"
        ).fetchone() == (5,)
        assert owned.connection.execute(
            "SELECT presence_state FROM device_files"
        ).fetchone() == (1,)
        assert owned.connection.execute(
            "SELECT status FROM operation_runs"
        ).fetchone() == (6,)
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_cold_recovery_settles_original_ticket_then_uses_remaining_query_budget(
    tmp_path, capture_resources,
):
    owned, runtime, query = await _world(tmp_path, [True])
    try:
        started = runtime.operations.begin_attempt(
            AttemptIntent(
                "query",
                2,
                OperationKind.CHECK_FILE_EXISTS,
                AttemptTarget(obtain_item_id=1),
                None,
                AttemptConfig(2, Decimal(1), Decimal(0)),
                _NOW,
            ),
            new_operation_key(),
            owned,
        )
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        with pytest.raises(ConsistencyError, match="恢复依据"):
            await _round(runtime)
        assert not query.calls
        assert owned.connection.execute(
            "SELECT status,result_event_id FROM operation_attempts"
        ).fetchone() == (1, None)
        horizon, = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()
        await capture_resources.restart_database(owned)
        runtime = await _query_runtime(owned, query,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=horizon)
        await _round(runtime)
        attempts = owned.connection.execute(
            "SELECT attempt_no,status,result_event_id FROM operation_attempts ORDER BY attempt_no"
        ).fetchall()
        assert [row[:2] for row in attempts] == [(1, 4), (2, 2)]
        assert all(row[2] is not None for row in attempts)
        assert len(query.calls) == 1 and query.calls[0].ticket.attempt_id == 2
        assert owned.connection.execute(
            "SELECT output_id,status FROM obtain_items"
        ).fetchone() == (1, 2)
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_committed_projection_receipt_is_verified_before_settling_item(
    tmp_path, monkeypatch
):
    from camctl.outputs import segment_query

    owned, runtime, query = await _world(tmp_path, [True])
    original = segment_query.observe_queried_output
    requests = []

    def lost_once(request, key, owned):
        requests.append((request, key))
        if len(requests) == 1:
            fault = _CommitFaultConnection(owned.connection, after_commit=True)
            fault.error = OperationalError("产物观察回执丢失")
            saved = original(request, key, replace(owned, connection=fault))
            assert fault.actual_commits == 1
            return saved
        return original(request, key, owned)

    monkeypatch.setattr(segment_query, "observe_queried_output", lost_once)
    try:
        with pytest.raises(ConsistencyError, match="回执丢失"):
            await _round(runtime)
        assert owned.connection.execute(
            "SELECT status FROM obtain_items"
        ).fetchone() == (1,)
        await _round(runtime)
        assert requests[0] == requests[1]
        assert len(query.calls) == 1
        assert owned.connection.execute(
            "SELECT output_id,status FROM obtain_items"
        ).fetchone() == (1, 2)
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_single_original_source_does_not_enter_segment_query(tmp_path):
    owned, runtime, query = await _world(tmp_path, [True])
    try:
        owned.connection.execute(
            "UPDATE actions SET execution_spec_json=? WHERE id=1",
            ('{"result_policy":null}',),
        )
        owned.connection.commit()
        await _round(runtime)
        assert not query.calls
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts"
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT status FROM obtain_items"
        ).fetchone() == (1,)
    finally:
        owned.connection.close()
