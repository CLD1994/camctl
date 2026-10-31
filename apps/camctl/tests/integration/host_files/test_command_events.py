"""媒体公共入口、文件任务和真实工具调用共用日志上下文。"""

import json
from unittest.mock import create_autospec

import pytest

from camctl.host_files.media import ProbeRequest, RepairRequest
from camctl.logging_runtime.events import event_logging, log_context
from camctl.logging_runtime.service import LogChannel
from media_helpers import probe_media, repair_media
from .test_media import _setup, _tool, _PROBE_BODY, _REPAIR_BODY

pytestmark = pytest.mark.asyncio


async def test_probe_and_repair_commands_keep_file_ids_and_parent_work_identity(tmp_path):
    input_ref, output_ref, roots, _, output_path = _setup(tmp_path)
    probe_tool = _tool(tmp_path, "probe", _PROBE_BODY)
    repair_tool = _tool(tmp_path, "repair", _REPAIR_BODY)
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with event_logging(channel), log_context(activity_id="7", copy_id=31):
        probe = await probe_media(input_ref, roots, ProbeRequest(ffprobe=probe_tool))
        repair = await repair_media(input_ref, output_ref, roots, RepairRequest(ffmpeg=repair_tool))
    assert probe.error is None and repair.error is None and output_path.is_file()
    records = [record for call in channel.slog.call_args_list
               if (record := json.loads(call.args[0].message))["stage"] == "external_tool"]
    assert [record["event"] for record in records] == ["started", "returned", "started", "returned"]
    assert all(record["activity_id"] == "7" and record["copy_id"] == 31 for record in records)
    assert all(record["tool_argv"][0] == probe_tool and record["file_id"] == input_ref.file_id
               for record in records[:2])
    assert all(record["tool_argv"][0] == repair_tool and record["file_id"] == output_ref.file_id
               and record["input_file_id"] == input_ref.file_id for record in records[2:])
    assert records[0]["tool_call_id"] == records[1]["tool_call_id"]
    assert records[2]["tool_call_id"] == records[3]["tool_call_id"]
    assert records[0]["tool_call_id"] != records[2]["tool_call_id"]
