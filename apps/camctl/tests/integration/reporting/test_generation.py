"""报告生成编排：真实冻结依据驱动固定 H 的入选、恢复与分段写出。

真实受理、完成与冻结产生报告行；生成器只消费冻结依据与历史，写
出公共 Schema 合法的 staging 文件。同一冻结依据重复生成字节不变，
冻结后的业务变化不改变既有报告；生成输入与登记不符时拒绝且不
留下半成品。
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.capture.dispatch import dispatch_ready, ready_capture_actions
from camctl.contracts.schemas import validate_document
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.workflow_errors import item_error_id, registered_error
from camctl.operations.attempts import RetryWaitGate
from camctl.outputs.handoff import DeliveryDirectories
from camctl.outputs.obtain_flow import ObtainRuntime, advance_obtain
from camctl.outputs.sources import (
    ResolutionState,
    SelectionMode,
    SourceResolution,
    read_source_spec,
    select_outputs,
)
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.repositories.outputs import (
    FinishObtain,
    FixSelection,
    OutputsRepository,
    ResolveSources,
    StartObtainAction,
    load_selection_facts,
)
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository, StartActionRequest
from camctl.persistence.transaction import encode_json_value, row_facts
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.reporting.generation import GenerationSpec, generate_report_file
from camctl.reporting.policy import (
    ReportDecisionKind,
    ReportingRepository,
    register_report_guards,
    register_sync_guard,
)

from ..capture.test_capture_contract import ResultsDouble
from ..history.test_report_scope import (
    _Catalog,
    _finish_action,
    _past_schedule,
    _runtime,
    _submit,
    _submit_diagnostic,
)
from ..persistence.test_runtime import _create_valid_database

register_report_guards()
register_sync_guard()

pytestmark = pytest.mark.asyncio


@pytest.fixture
def environment(tmp_path):
    _create_valid_database(tmp_path / "state.db")
    owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    try:
        yield owned, tmp_path
    finally:
        owned.connection.close()


def _freeze(owned):
    outcome = ReportingRepository().freeze_report(
        new_operation_key(), owned, occurred_at=1)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.kind is ReportDecisionKind.GENERATE
    return outcome.value.report


def _spec(tmp_path, report, name="staging/report-1.json", **overrides):
    values = dict(
        db_path=tmp_path / "state.db",
        report_id=report.report_id,
        from_wm=report.from_wm,
        to_wm=report.to_wm,
        frozen_event_id=report.boundary.last_event_id,
        format_version=report.format_version,
        staging_path=tmp_path / name,
    )
    values.update(overrides)
    return GenerationSpec(**values)


class _CaptureAndObtainCatalog(_Catalog):
    def action_types(self):
        return super().action_types() | {"obtain_action_outputs"}


async def _save_failed_source_obtain(owned):
    """通过受理与历史事务建立失败拍摄来源和空目录取回。"""
    now = int(time.time() * 1_000_000)
    scheduled_at = _past_schedule()
    body = {
        "request_id": "880001",
        "created_at": "2026-01-15 08:00:00",
        "name": "source action failed",
        "actions": [
            {
                "name": "failed shoot",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": scheduled_at,
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            },
            {
                "name": "obtain failed source",
                "type": "obtain_action_outputs",
                "scheduled_at": scheduled_at,
                "params": {"source": {"action_name": "failed shoot"}},
            },
        ],
    }
    accepted = AcceptanceRepository().process_input(
        ProcessInput(
            ParsedInput("source-action-failed.json", body),
            _CaptureAndObtainCatalog(),
            CommandMode.RUN,
            now,
        ),
        new_operation_key(),
        owned,
    )
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    assert accepted.value.plan_id is not None, accepted.value
    actions = owned.connection.execute(
        "SELECT id,name FROM actions WHERE plan_id=? ORDER BY id",
        (accepted.value.plan_id,),
    ).fetchall()
    source_id = next(identity for identity, name in actions if name == "failed shoot")
    obtain_id = next(identity for identity, name in actions if name == "obtain failed source")
    started_capture = SchedulingRepository().start_action(
        StartActionRequest(
            action_id=source_id, trusted_wall_now=now, occurred_at=now
        ),
        new_operation_key(),
        owned,
    )
    assert started_capture.kind is DbOutcomeKind.COMPLETED, started_capture.error
    runtime = _runtime(owned)
    runtime.results = ResultsDouble({1: ()})
    await dispatch_ready(runtime, ready_capture_actions(owned.connection, now))
    source = row_facts(owned.connection, "actions", source_id)
    assert source["status"] == 4
    assert source["error_code"] is not None

    repository = OutputsRepository()
    started = repository.start_obtain_action(
        StartObtainAction(obtain_id, now), new_operation_key(), owned
    )
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    owner = row_facts(owned.connection, "actions", obtain_id)
    resolved = repository.resolve_sources(
        ResolveSources(
            obtain_id,
            read_source_spec(encode_json_value(owner["input_fields_json"])),
            now,
        ),
        new_operation_key(),
        owned,
    )
    assert resolved.kind is DbOutcomeKind.COMPLETED, resolved.error
    assert resolved.value.fixed is True
    selection_id, = owned.connection.execute(
        "SELECT id FROM obtain_source_selections WHERE dependency_id IN "
        "(SELECT id FROM action_dependencies WHERE action_id=?)",
        (obtain_id,),
    ).fetchone()
    facts = load_selection_facts(owned.connection, source_id)
    snapshot = select_outputs(
        SourceResolution(
            state=ResolutionState.FIXED,
            member_action_ids=(source_id,),
            source_plan_id=source["plan_id"],
        ),
        facts,
        SelectionMode.DEFAULT,
    )
    fixed = repository.fix_selection(
        FixSelection(selection_id, snapshot, now), new_operation_key(), owned
    )
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    assert fixed.value.snapshot.source_error_code == item_error_id(
        "obtain_source_selections", "source_action_failed"
    )
    finished = repository.finish_obtain(
        FinishObtain(obtain_id, now), new_operation_key(), owned
    )
    assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
    assert finished.value.action_status == 4
    return source_id, obtain_id, source["error_code"]


async def _save_admission_failed_source_obtain(owned, tmp_path, mode):
    """经正式受理与取回编排处理没有执行规格的失败来源。"""
    now = int(time.time() * 1_000_000)
    params = {"source": {"action_name": "failed admission"}}
    if mode is SelectionMode.PREVIEW:
        params["filter"] = "preview"
    body = {
        "request_id": "880003",
        "created_at": "2026-01-15 08:00:00",
        "name": "admission failed source",
        "actions": [
            {
                "name": "failed admission",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": "2026-02-30 08:00:00",
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            },
            {
                "name": "obtain admission failure",
                "type": "obtain_action_outputs",
                "scheduled_at": _past_schedule(),
                "params": params,
            },
        ],
    }
    accepted = AcceptanceRepository().process_input(
        ProcessInput(
            ParsedInput("failed-admission-source.json", body),
            _CaptureAndObtainCatalog(),
            CommandMode.RUN,
            now,
        ),
        new_operation_key(),
        owned,
    )
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    actions = owned.connection.execute(
        "SELECT id,name FROM actions WHERE plan_id=? ORDER BY id",
        (accepted.value.plan_id,),
    ).fetchall()
    source_id = next(identity for identity, name in actions if name == "failed admission")
    obtain_id = next(identity for identity, name in actions if name == "obtain admission failure")
    source = row_facts(owned.connection, "actions", source_id)
    assert source["status"] == 4
    assert source["execution_spec_json"] is None

    runtime = ObtainRuntime(
        owned=owned,
        devices={},
        directories=DeliveryDirectories(
            tmp_path / "staging", tmp_path / "ready", tmp_path / "processing"
        ),
        segment_size=1024,
        retry_gate=RetryWaitGate(),
        monotonic_ns=time.monotonic_ns,
        occurred_at=lambda: now,
        outputs=OutputsRepository(),
        operations=OperationRepository(),
    )
    await advance_obtain(runtime)
    return source_id, obtain_id, source["error_code"]


class TestGenerateReportFile:
    async def test_generation_writes_schema_valid_staging_file(self, environment):
        owned, tmp_path = environment
        await _submit(owned, tmp_path, "1")
        await _finish_action(owned)
        report = _freeze(owned)

        generated = generate_report_file(_spec(tmp_path, report))

        assert generated.path == tmp_path / "staging" / "report-1.json"
        payload = generated.path.read_bytes()
        assert generated.size_bytes == len(payload)
        assert generated.sha256 == hashlib.sha256(payload).hexdigest()
        document = json.loads(payload)
        validate_document("protocol/status-report.schema.json", document)
        assert document["report_id"] == str(report.report_id)
        assert (document["from_wm"], document["to_wm"]) == (
            report.from_wm, report.to_wm)
        plan = document["plans"][0]
        assert plan["plan_instance_id"] == "1"
        assert plan["status"] == "completed"
        action = plan["actions"][0]
        assert action["action_instance_id"] == "1"
        assert action["type"] == "camera_take_photo"
        assert action["status"] == "succeeded"
        output = action["outputs"][0]
        assert output["output_id"] == "1"
        assert isinstance(output["size"], int) and output["size"] > 0
        assert output["availability"] == "available"
        assert "checksum" in output

    async def test_failed_source_error_rebuilds_at_the_frozen_history_boundary(
        self, environment
    ):
        owned, tmp_path = environment
        source_id, obtain_id, source_action_error_id = await _save_failed_source_obtain(owned)
        report = _freeze(owned)

        first = generate_report_file(
            _spec(tmp_path, report, name="staging/source-failed-first.json")
        )
        document = json.loads(first.path.read_bytes())
        validate_document("protocol/status-report.schema.json", document)
        actions = {
            action["action_instance_id"]: action
            for plan in document["plans"]
            for action in plan["actions"]
        }
        source = actions[str(source_id)]
        obtain = actions[str(obtain_id)]
        assert source["error"]["code"] != "source_action_failed"
        assert (
            row_facts(owned.connection, "actions", source_id)["error_code"]
            == source_action_error_id
        )
        assert obtain["status"] == "failed"
        assert obtain["error"] == {
            "code": "obtain_items_failed",
            "stage": registered_error("obtain_items_failed")["stage"],
            "details": {},
        }
        assert obtain["result"]["failures"] == [
            {
                "source_action_instance_id": str(source_id),
                "error": {
                    "code": "source_action_failed",
                    "stage": "output_selection",
                    "details": {"source_action_instance_id": str(source_id)},
                },
            }
        ]

        await _submit(owned, tmp_path, "880002")
        repeated = generate_report_file(
            _spec(tmp_path, report, name="staging/source-failed-rebuilt.json")
        )
        assert repeated.path.read_bytes() == first.path.read_bytes()

    @pytest.mark.parametrize("mode", [SelectionMode.DEFAULT, SelectionMode.PREVIEW])
    async def test_admission_failed_source_flows_through_obtain_and_frozen_report(
        self, environment, mode
    ):
        owned, tmp_path = environment
        source_id, obtain_id, source_action_error_id = (
            await _save_admission_failed_source_obtain(owned, tmp_path, mode)
        )

        source_row = row_facts(owned.connection, "actions", source_id)
        obtain_row = row_facts(owned.connection, "actions", obtain_id)
        assert source_row["error_code"] == source_action_error_id
        assert source_row["execution_spec_json"] is None
        assert obtain_row["status"] == 4
        assert obtain_row["error_code"] == registered_error("obtain_items_failed")[
            "action_error_id"
        ]
        selection_id, = owned.connection.execute(
            "SELECT s.id FROM obtain_source_selections s"
            " JOIN action_dependencies d ON d.id=s.dependency_id"
            " WHERE d.action_id=?",
            (obtain_id,),
        ).fetchone()
        selection = row_facts(
            owned.connection, "obtain_source_selections", selection_id
        )
        assert selection["error_code"] == item_error_id(
            "obtain_source_selections", "source_action_failed"
        )
        assert selection["error_details_json"] == {
            "source_action_instance_id": str(source_id)
        }
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM obtain_items WHERE selection_id=?", (selection_id,)
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM deliveries WHERE action_id=?", (obtain_id,)
        ).fetchone() == (0,)

        report = _freeze(owned)
        first = generate_report_file(
            _spec(tmp_path, report, name="staging/admission-source-failed.json")
        )
        document = json.loads(first.path.read_bytes())
        validate_document("protocol/status-report.schema.json", document)
        actions = {
            action["action_instance_id"]: action
            for plan in document["plans"]
            for action in plan["actions"]
        }
        assert actions[str(source_id)]["error"]["code"] != "source_action_failed"
        assert actions[str(obtain_id)]["error"]["code"] == "obtain_items_failed"
        assert actions[str(obtain_id)]["result"]["failures"] == [
            {
                "source_action_instance_id": str(source_id),
                "error": {
                    "code": "source_action_failed",
                    "stage": "output_selection",
                    "details": {"source_action_instance_id": str(source_id)},
                },
            }
        ]

        await _submit(owned, tmp_path, "880004")
        repeated = generate_report_file(
            _spec(tmp_path, report, name="staging/admission-source-failed-again.json")
        )
        assert repeated.path.read_bytes() == first.path.read_bytes()

    async def test_regeneration_is_byte_identical_despite_later_changes(
        self, environment
    ):
        owned, tmp_path = environment
        await _submit(owned, tmp_path, "1")
        await _finish_action(owned)
        report = _freeze(owned)
        first = generate_report_file(_spec(tmp_path, report, name="staging/first.json"))

        # 冻结后的新计划在窗口外；读取批次与写出缓冲不改变报告字节。
        await _submit(owned, tmp_path, "2")
        second = generate_report_file(
            _spec(tmp_path, report, name="staging/second.json"))
        assert second.path.read_bytes() == first.path.read_bytes()
        third = generate_report_file(_spec(
            tmp_path, report, name="staging/third.json",
            entity_batch_size=1, event_batch_size=1), buffer_size=3)
        assert third.path.read_bytes() == first.path.read_bytes()

    async def test_window_diagnostics_are_written(self, environment):
        owned, tmp_path = environment
        await _submit(owned, tmp_path, "1")
        await _finish_action(owned)
        await _submit_diagnostic(owned, tmp_path, "d1")
        report = _freeze(owned)

        generated = generate_report_file(_spec(tmp_path, report))

        document = json.loads(generated.path.read_bytes())
        diagnostics = document["plan_file_diagnostics"]
        assert len(diagnostics) == 1
        assert diagnostics[0]["file_name"].endswith("d1.json")

    async def test_mismatched_generation_input_is_rejected(self, environment):
        owned, tmp_path = environment
        await _submit(owned, tmp_path, "1")
        await _finish_action(owned)
        report = _freeze(owned)
        staging = tmp_path / "staging" / "report-1.json"

        with pytest.raises(ConsistencyError):
            generate_report_file(
                _spec(tmp_path, report, from_wm=report.from_wm + 1))

        assert not staging.exists()

    async def test_unknown_report_is_rejected(self, environment):
        owned, tmp_path = environment
        await _submit(owned, tmp_path, "1")
        await _finish_action(owned)
        report = _freeze(owned)

        with pytest.raises(ConsistencyError):
            generate_report_file(
                _spec(tmp_path, report, report_id=report.report_id + 5))

        assert not (tmp_path / "staging" / "report-6.json").exists()
