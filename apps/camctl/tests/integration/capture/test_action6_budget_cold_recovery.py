"""Action6 检查预算从 JSON 冷恢复，并沿原部分拷贝接续。"""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.media_flow import DriverReadSessions, MediaFlow
from camctl.contracts.enums import enum_for
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.segment_inspections import (
    SegmentInspectionRepository,
    read_inspection_for_member,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..bootstrap.test_media_assembly import _config
from .test_action6_segmented_recovery import (
    _entries,
    _overrun_recording,
    _publish_ordinary_result,
)
from .test_capture_contract import _NOW
from ..support.capture import capture_database
from .test_input_copy import _CONTENT
from .test_segment_inspections import _ReadDriver

pytestmark = pytest.mark.asyncio


class _NoDeviceResults:
    async def list_page(self, *args, **kwargs):
        raise AssertionError("冷恢复应消费可靠结果页，不能重新列举设备")


async def _cold_runtime(runtime, owned, driver, *, now, resources):
    tools = runtime.media.tools
    tools.owned = owned
    ns = [100_000_000_000]
    database = await resources.restart_database(owned)
    resumed = await resources.runtime(database, driver=runtime.driver, evidence=runtime.evidence,
        results=_NoDeviceResults(), wall_us=lambda: now, monotonic_ns=lambda: ns[0])
    resumed.media = MediaFlow(database=database, roots=runtime.media.roots,
        sessions=DriverReadSessions(database, driver, ticket=None), tools=tools,
        policy=runtime.media.policy, occurred_at=lambda: now,
        digest_supported=True, digest=runtime.media.digest,
        segment_size=runtime.media.segment_size, monotonic_ns=lambda: ns[0])
    return resumed, ns


async def test_action6_cold_recovery_decodes_saved_budget_and_continues_same_copy(
    tmp_path, monkeypatch, capture_resources,
):
    owned, runtime, action_id, handler, tools = await _overrun_recording(
        tmp_path, (Decimal("70"),)
    )
    first_driver = _ReadDriver(_CONTENT, fail_after=4)
    active_owned = owned
    runtime.media = replace(
        runtime.media,
        sessions=DriverReadSessions(await capture_database(owned), first_driver, ticket=None),
        segment_size=4,
    )
    try:
        await _publish_ordinary_result(
            runtime, owned, action_id, handler, _entries(1)
        )
        assert first_driver.offsets == [0]

        (member_id,) = owned.connection.execute(
            "SELECT member_id FROM segment_inspections WHERE action_id=?",
            (action_id,),
        ).fetchone()
        first_facts = read_inspection_for_member(action_id, member_id, owned)
        assert first_facts.inspection is not None
        assert first_facts.inspection.state == int(
            enum_for("segment_inspections.state").PENDING
        )
        assert first_facts.copy is not None
        original_copy_id = first_facts.copy.copy_id
        original_offset = first_facts.copy.committed_bytes
        assert 0 < original_offset < first_facts.copy.source_size
        assert first_facts.inspection.budget_json == {
            "max_attempts": 3,
            "timeout_s": 10,
            "retry_interval_s": 3,
        }
        assert type(first_facts.inspection.budget_json["timeout_s"]) is int
        assert type(first_facts.inspection.budget_json["retry_interval_s"]) is int
        assert tools.calls == []

        budget_rows = owned.connection.execute(
            "SELECT max_attempts_used, timeout_s_json, retry_interval_s_json"
            " FROM operation_runs WHERE copy_id=?",
            (original_copy_id,),
        ).fetchall()
        assert budget_rows == [(3, "10", "3")]

        owned.connection.close()
        reopened = open_existing(
            Path(_config(tmp_path).paths.state_db),
            DbOpenMode.EXISTING_RW,
            DbConfig(),
        )
        active_owned = reopened
        resumed_driver = _ReadDriver(_CONTENT)
        runtime, ns = await _cold_runtime(
            runtime, reopened, resumed_driver, now=_NOW + 90_000_000, resources=capture_resources
        )

        original_save = SegmentInspectionRepository.save_result

        def commit_then_report_unknown(repository, request, key, connection):
            outcome = original_save(repository, request, key, connection)
            assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
            return DbOutcome(
                kind=DbOutcomeKind.UNKNOWN,
                error=OSError("injected result commit receipt loss"),
            )

        monkeypatch.setattr(
            SegmentInspectionRepository, "save_result", commit_then_report_unknown
        )
        await capture_handler(handler)(action_id, runtime)

        waiting = read_inspection_for_member(action_id, member_id, reopened)
        assert waiting.inspection is not None
        assert waiting.inspection.state == int(
            enum_for("segment_inspections.state").PENDING
        )
        assert resumed_driver.offsets == []

        ns[0] = 104_000_000_000
        await capture_handler(handler)(action_id, runtime)

        after_read = read_inspection_for_member(action_id, member_id, reopened)
        assert after_read.inspection is not None
        assert after_read.inspection.state == int(
            enum_for("segment_inspections.state").COMPLETED
        )
        assert after_read.copy is not None
        assert after_read.copy.copy_id == original_copy_id
        assert after_read.copy.committed_bytes == after_read.copy.source_size
        assert resumed_driver.offsets == [original_offset]
        assert len(tools.calls) == 1
        assert budget_rows == reopened.connection.execute(
            "SELECT max_attempts_used, timeout_s_json, retry_interval_s_json"
            " FROM operation_runs WHERE copy_id=?",
            (original_copy_id,),
        ).fetchall()
        assert after_read.inspection.budget_json == first_facts.inspection.budget_json

        reopened.connection.close()
        cold_reopened = open_existing(
            Path(_config(tmp_path).paths.state_db),
            DbOpenMode.EXISTING_RW,
            DbConfig(),
        )
        active_owned = cold_reopened
        final_driver = _ReadDriver(_CONTENT)
        runtime, _ns = await _cold_runtime(
            runtime, cold_reopened, final_driver, now=_NOW + 100_000_000, resources=capture_resources
        )
        monkeypatch.setattr(
            SegmentInspectionRepository, "save_result", original_save
        )
        await capture_handler(handler)(action_id, runtime)

        final_facts = read_inspection_for_member(action_id, member_id, cold_reopened)
        assert final_facts.inspection is not None
        assert final_facts.copy is not None
        assert final_facts.copy.copy_id == original_copy_id
        assert tools.calls == [(final_facts.copy.target_file_id, False)]
        assert final_driver.offsets == []
        runtime.driver.control.assert_awaited_once()
        assert (await runtime.action(action_id))["status"] == 3
    finally:
        active_owned.connection.close()
