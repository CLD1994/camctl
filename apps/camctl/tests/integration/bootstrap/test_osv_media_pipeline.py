"""真实双视频由C1生产输入，冷恢复只沿原探测与输出继续修复。"""

from decimal import Decimal
import hashlib
import shutil
import subprocess

import pytest

from camctl.capture.media import RepairExecutionPhase
from camctl.capture.media_flow import load_processing_status, run_recording_media
from camctl.capture.media_facts import saved_output_probe

from ..host_files.test_media import _make_dual_video_input
from ..support.bootstrap_osv_recovery import osv_world


@pytest.mark.asyncio
async def test_real_dual_video_repair_survives_restart_and_decodes_every_stream(tmp_path, capture_resources, monkeypatch):
    if shutil.which("ffprobe") is None:
        pytest.skip("需要真实 ffprobe")
    source = _make_dual_video_input(tmp_path)
    content = source.read_bytes()
    before = hashlib.sha256(content).hexdigest()
    async with osv_world(tmp_path, capture_resources, content, monkeypatch) as world:
        first = await run_recording_media(world.runtime.media, 1, world.processing_id,
            world.source_id, target_extension="osv")
        assert first.phase is RepairExecutionPhase.PREPARED
        paused = await run_recording_media(world.runtime.media, 1, world.processing_id,
            world.source_id, target_extension="osv")
        assert paused.phase is RepairExecutionPhase.TOOL_NOT_COMPLETED
        assert world.withdrawn
        status = await world.database.read(lambda owned: load_processing_status(owned, world.processing_id))
        assert status.repair_state == 4 and status.source_probe is not None
        assert [stream.codec_type for stream in status.source_probe.streams].count("video") == 3
        input_name = world.owned.connection.execute(
            "SELECT relative_path FROM intermediate_files WHERE purpose=2").fetchone()[0]
        assert input_name.endswith(".osv")
        assert (world.roots.staging / input_name).read_bytes() == content
        assert world.input_probe_log.read_text().splitlines() == [str(world.roots.staging / input_name)]
        await world.reconnect()
        runtime, _executor = await world.factory()
        result = await run_recording_media(runtime.media, 1, world.processing_id,
            world.source_id, target_extension="osv")
        assert result.phase is RepairExecutionPhase.SUCCEEDED, result
        output = world.roots.staging / result.output_file.relative_path
        assert world.probe_logs[-1].read_text().splitlines() == [str(output)]
        probe = saved_output_probe(world.owned.connection, result.output_file.file_id)
        assert len(probe.streams) == 3
        assert [stream.codec_type for stream in probe.streams] == ["video", "video", "audio"]
        assert all(stream.duration_s >= Decimal("0.5") for stream in probe.streams)
        decoded = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-xerror",
            "-i", str(output), "-map", "0:v", "-map", "0:a", "-f", "null", "-"],
            capture_output=True, text=True)
        assert decoded.returncode == 0, decoded.stderr
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        assert hashlib.sha256((world.roots.staging / input_name).read_bytes()).hexdigest() == before
        assert [kind for kind, _request in world.driver.calls].count("read") == 1
