"""Action6 有限覆盖判断经固定 H 投影为公共录像覆盖报告。"""

import json
from decimal import Decimal

import pytest

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.capture.handlers import capture_handler
from camctl.capture.result_inputs import RESULT_PAGE_CONTRACT
from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import EvidenceRegistry
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.schemas import validate_document
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.repositories.history import HistoryRepository
from camctl.reporting.generation import generate_report_file

from ..capture.result_consumer_fixtures import RESULT_EVIDENCE, consumer_world
from ..capture.test_action6_segmented_normal import (
    Action6NativeCatalog,
    Action6RecordCatalog,
    _entry,
)
from ..capture.test_capture_contract import _NOW
from ..capture.test_result_page_history import _actual
from .test_generation import _freeze, _spec

pytestmark = pytest.mark.asyncio


def _mixed_entries():
    entries = [
        _entry(f"DJI_001/DJI_20261022100000_{index:04d}_D.MP4")
        for index in (7, 8, 9)
    ]
    entries.extend(
        _entry(f"DJI_001/DJI_20261022100000_{index:04d}_D.LRF")
        for index in (7, 8)
    )
    entries.append(_entry("DJI_001/DJI_20261022100000_0007_D.JPG"))
    return sorted(entries, key=lambda entry: entry["identity"])


async def _complete_action6(tmp_path, action_type):
    if action_type == "camera_record":
        from ..support.outputs import segmented_output_source
        owned, _runtime, action_id = await segmented_output_source(
            tmp_path, catalog=Action6RecordCatalog(), entries=_mixed_entries())
        return owned, action_id

    state = {"wall": _NOW, "mono": 5_000_000_000}
    evidence = EvidenceRegistry((RESULT_PAGE_CONTRACT,
        *(RESULT_EVIDENCE.contract(name, 1) for name in (
            "operation_returned", "timelapse_sent", "results_returned")),
    ))

    class Results:
        async def list_results(self, request, _batch):
            return DeviceCallResult.from_outcome(
                _actual(request.ticket, entries=_mixed_entries(), finalized=True))

    results = DriverResultListing(Results(), DeviceBinding("cam-1", "camctl-adb"), evidence)
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "timelapse", catalog=Action6NativeCatalog(0), start=False,
        runtime_options={
            "results": results, "evidence": evidence,
            "wall_us": lambda: state["wall"], "monotonic_ns": lambda: state["mono"],
            "wait_config": lambda _action: CaptureWaitConfig(1000, 200, 0, 0),
            "check_config": AttemptConfig(1, Decimal("1"), Decimal(0)),
        })
    await capture_handler(handler)(action_id, runtime)
    state.update(wall=_NOW + 1_200_000, mono=6_200_000_000)
    await capture_handler(handler)(action_id, runtime)
    return owned, action_id


@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
async def test_action6_coverage_and_video_scope_survive_report_history(tmp_path, action_type):
    case_path = tmp_path / action_type
    case_path.mkdir()
    owned, action_id = await _complete_action6(case_path, action_type)
    closed = False
    try:
        report = _freeze(owned)
        original = generate_report_file(
            _spec(case_path, report, "staging/original.json")
        )
        document = json.loads(original.path.read_bytes())
        validate_document("protocol/status-report.schema.json", document)
        (action,) = document["plans"][0]["actions"]
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (action_id,)
        ).fetchone() == (3,)

        coverage = action["result"]["coverage"]
        expected_metric = "frames" if action_type == "camera_timelapse" else "duration_s"
        expected_target = 3 if expected_metric == "frames" else Decimal("12")
        expected_scope_ids = [
            str(row[0])
            for row in owned.connection.execute(
                "SELECT output_id FROM segment_scope_members "
                "WHERE output_id IS NOT NULL ORDER BY ordinal"
            )
        ]
        assert coverage == {
            "metric": expected_metric,
            "target": expected_target,
            "confirmed": None,
            "status": "not_performed",
            "checked_output_ids": [],
            "default_scope": {
                "state": "nonempty",
                "member_count": 3,
                "output_ids": expected_scope_ids,
            },
        }
        original_ids = {
            item["output_id"]
            for item in action["outputs"]
            if item["kind"] == "original"
        }
        assert len(original_ids) == 4
        assert len(expected_scope_ids) == 3
        assert set(expected_scope_ids) < original_ids

        owned.connection.close()
        closed = True
        rebuilt = generate_report_file(
            _spec(case_path, report, "staging/rebuilt.json"),
            repository=HistoryRepository(case_path / "state.db"),
            buffer_size=7,
        )
        assert rebuilt.path.read_bytes() == original.path.read_bytes()
    finally:
        if not closed:
            owned.connection.close()
