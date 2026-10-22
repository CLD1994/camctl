"""Action6 固定视频范围和独立照片经真实来源选择共同保留。"""

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.segment_selections import FixSegmentSelection, fix_segment_selection

from ..capture.test_action6_segmented_normal import _entry, _stopped_action6_record
from ..capture.test_record_file_completion_wait import _completed_result, _result_port, _stop_context
from .test_segment_default_scope import _obtain


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default", "preview"])
async def test_video_scope_selection_keeps_independent_photos_as_originals(tmp_path, mode):
    owned, runtime, action_id, handler, _ = await _stopped_action6_record(tmp_path)
    try:
        context = _stop_context(owned, action_id)
        entries = sorted([
            *(_entry(f"DJI_001/DJI_20261022100000_{index:04d}_D.{suffix}")
              for index in (7, 8) for suffix in ("MP4", "LRF")),
            _entry("DJI_001/DJI_20261022100000_0999_D.JPG"),
        ], key=lambda entry: entry["identity"])
        for entry in entries:
            if entry["identity"].endswith(".LRF"):
                entry["paired_identity"] = entry["identity"][:-4] + ".MP4"

        async def read(request, batch):
            return _completed_result(request.ticket, context, entries=entries, finalized=True)

        _result_port(runtime, read)
        await capture_handler(handler)(action_id, runtime)
        assert runtime.action(action_id)["status"] == 3
        _, selection = _obtain(owned, runtime, action_id, mode, 2)
        saved = fix_segment_selection(FixSegmentSelection(selection, runtime.wall_us()),
                                      new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        selected = owned.connection.execute(
            "SELECT json_extract(d.identity_key,'$[2]'),o.kind,i.status FROM obtain_items i"
            " JOIN outputs o ON o.id=i.output_id JOIN device_files d ON d.id=o.device_file_id"
            " WHERE i.selection_id=? ORDER BY i.id", (selection,),
        ).fetchall()
        assert len(selected) == 3
        assert all(status == 2 for _, _, status in selected)
        assert sum(name.endswith(".JPG") and kind == 1 for name, kind, _ in selected) == 1
        suffix = ".MP4" if mode == "default" else ".LRF"
        kind = 1 if mode == "default" else 3
        assert sum(name.endswith(suffix) and actual_kind == kind
                   for name, actual_kind, _ in selected) == 2
        assert owned.connection.execute("SELECT COUNT(*) FROM file_copies").fetchone() == (0,)
    finally:
        owned.connection.close()
