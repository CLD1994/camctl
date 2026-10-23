"""真实分段发布经固定 H 报告保留完整缺口、范围及产物身份。"""

import json

import pytest

from camctl.capture.files import FileChecksumSave, FilePresenceSave
from camctl.contracts.enums import enum_for
from camctl.contracts.values import new_operation_key
from camctl.contracts.workflow_errors import validate_public_json
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.history import HistoryRepository
from camctl.reporting.generation import generate_report_file

from ..outputs.test_segment_default_scope import _source, _obtain
from ..outputs.test_cleanup_query_save_recovery import _start_cleanup
from camctl.persistence.repositories.segment_selections import (
    FixSegmentSelection,
    fix_segment_selection,
)
from .test_generation import _freeze, _spec

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("consumer", ["record", "timelapse"])
async def test_expired_prepared_source_without_segment_set_reports_selection_failure(
    tmp_path, consumer,
):
    from camctl.persistence.repositories.scheduling import ExpireActionRequest
    from camctl.persistence.repositories.outputs import FinishObtain, OutputsRepository
    from ..capture.result_consumer_fixtures import consumer_world
    from ..capture.test_action6_segmented_normal import (
        Action6NativeCatalog, Action6RecordCatalog,
    )
    from ..capture.test_capture_contract import _NOW

    catalog = Action6RecordCatalog() if consumer == "record" else Action6NativeCatalog(0)
    owned, runtime, source_id, _ = await consumer_world(
        tmp_path, consumer, catalog=catalog, start=False,
    )
    try:
        now = _NOW + 6_000_000
        expired = runtime.scheduling.expire_action(
            ExpireActionRequest(source_id, now, now), new_operation_key(), owned,
        )
        assert expired.kind is DbOutcomeKind.COMPLETED, expired.error
        assert runtime.action(source_id)["status"] == 5
        assert runtime.action(source_id)["execution_started"] == 1
        assert owned.connection.execute("SELECT COUNT(*) FROM segment_sets").fetchone() == (0,)
        action_id, selection = _obtain(owned, runtime, source_id, "preview", 2)
        fixed = fix_segment_selection(
            FixSegmentSelection(selection, now), new_operation_key(), owned,
        )
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
        finished = OutputsRepository().finish_obtain(
            FinishObtain(action_id, now), new_operation_key(), owned,
        )
        assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
        report = _freeze(owned)
        generated = generate_report_file(_spec(tmp_path, report))
        before = generated.path.read_bytes()
        doc = json.loads(before)
        validate_public_json("", doc)
        actions = {action["action_instance_id"]: action
                   for plan in doc["plans"] for action in plan["actions"]}
        assert actions[str(source_id)]["status"] == "expired"
        assert actions[str(source_id)]["expiration_reason"] == "window_exhausted"
        failure = actions[str(action_id)]["result"]["failures"][0]
        assert failure["error"]["code"] == "default_segment_scope_unconfirmed"
        assert failure["error"]["details"]["retained_output_ids"] == []
        rebuilt = generate_report_file(_spec(tmp_path, report, "staging/rebuilt-expired.json"))
        assert rebuilt.path.read_bytes() == before
        assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)
    finally:
        owned.connection.close()


async def test_gap_report_preserves_retained_outputs_and_default_prefix(tmp_path):
    owned, runtime, aid = await _source(tmp_path, (7, 8, 10))
    try:
        report = _freeze(owned)
        generated = generate_report_file(_spec(tmp_path, report))
        doc = json.loads(generated.path.read_bytes())
        validate_public_json("", doc)
        action = doc["plans"][0]["actions"][0]
        details = action["error"]["details"]
        originals = [item for item in action["outputs"] if item["kind"] == "original"]
        assert len(originals) == 3 and len(action["outputs"]) == 6
        assert details["retained_output_ids"] == [
            item["output_id"] for item in originals
        ]
        assert (
            details["default_scope"]["output_ids"] == details["retained_output_ids"][:2]
        )
        assert details["gap_count"] == len(details["gaps"]) == 1
        gap = details["gaps"][0]
        assert gap["start_key"] == gap["end_key"] == {"directory_number": 1, "index": 9}
        assert gap["left"]["output_id"] == details["retained_output_ids"][1]
        assert gap["right"]["output_id"] == details["retained_output_ids"][2]
        assert details["first_gap"] == gap
        assert "output_id" not in gap and "deliveries" not in action
    finally:
        owned.connection.close()


async def test_segment_report_rebuilds_identical_bytes_after_cleanup(tmp_path):
    owned, runtime, aid = await _source(tmp_path, (7, 8, 10))
    try:
        report = _freeze(owned)
        original = generate_report_file(_spec(tmp_path, report))
        before = original.path.read_bytes()
        output_id, file_id = owned.connection.execute(
            "SELECT id,device_file_id FROM outputs WHERE kind=1 ORDER BY id LIMIT 1"
        ).fetchone()
        _start_cleanup(owned, output_id, "902")
        for method, request in (
            (
                "save_file_checksum",
                FileChecksumSave(
                    file_id,
                    int(enum_for("device_files.checksum_support").SUPPORTED),
                    runtime.wall_us(),
                    "a" * 64,
                ),
            ),
            (
                "save_file_presence",
                FilePresenceSave(
                    file_id,
                    int(enum_for("device_files.presence_state").ABSENT),
                    runtime.wall_us(),
                ),
            ),
        ):
            receipt = getattr(runtime.capture, method)(
                request, new_operation_key(), owned
            )
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        action, selection = _obtain(owned, runtime, aid, "default", 2)
        fixed = fix_segment_selection(
            FixSegmentSelection(selection, runtime.wall_us()),
            new_operation_key(),
            owned,
        )
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
        repo = HistoryRepository(tmp_path / "state.db")
        rebuilt = generate_report_file(
            _spec(tmp_path, report, "staging/rebuilt.json"), repository=repo
        )
        assert rebuilt.path.read_bytes() == before
        assert (rebuilt.size_bytes, rebuilt.sha256) == (
            original.size_bytes,
            original.sha256,
        )
    finally:
        owned.connection.close()
