"""报告生成编排：真实冻结依据驱动固定 H 的入选、恢复与分段写出。

真实受理、完成与冻结产生报告行；生成器只消费冻结依据与历史，写
出公共 Schema 合法的 staging 文件。同一冻结依据重复生成字节不变，
冻结后的业务变化不改变既有报告；生成输入与登记不符时拒绝且不
留下半成品。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from camctl.contracts.schemas import validate_document
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.reporting.generation import GenerationSpec, generate_report_file
from camctl.reporting.policy import (
    ReportDecisionKind,
    ReportingRepository,
    register_report_guards,
    register_sync_guard,
)

from ..history.test_report_scope import _finish_action, _submit, _submit_diagnostic
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
        staging_path=tmp_path / name,
    )
    values.update(overrides)
    return GenerationSpec(**values)


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
