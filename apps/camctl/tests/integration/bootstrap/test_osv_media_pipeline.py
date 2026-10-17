"""真实双视频媒体经过输入拷贝、历史恢复和生产修复适配器。"""
from decimal import Decimal
from pathlib import Path
import hashlib
import shutil
import subprocess

import pytest

from camctl.bootstrap.capture_assembly import HostMediaTools
from camctl.capture.media import MediaPolicy, RepairExecutionPhase
from camctl.capture.media_flow import DriverReadSessions, MediaFlow, load_processing_status, run_recording_media
from camctl.capture.media_facts import saved_output_probe
from camctl.host_files.models import FilePurpose
from camctl.host_files.tasks import FileTaskExecutor, FileTaskId, FileTaskResult
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.session.supervision import Supervisor
from ..capture.test_input_copy import pipeline
from ..capture.test_media_flow import ReadDriverDouble, _Digest
from ..host_files.test_media import _make_dual_video_input


class _Owner:
    async def take_over(self, task):
        await task.pending.wait()


class ObservedTools(HostMediaTools):
    def __init__(self, roots, *, pause=False):
        super().__init__(roots, FileTaskExecutor(Supervisor()), _Owner())
        self.pause, self.probes = pause, []

    async def probe(self, input):
        self.probes.append(input.purpose)
        return await super().probe(input)

    async def repair(self, input, output, **kwargs):
        if self.pause:
            return FileTaskResult(FileTaskId("pause-before-repair"), ran=False)
        return await super().repair(input, output, **kwargs)


def flow(owned, roots, content, tools):
    return MediaFlow(owned=owned, roots=roots,
        sessions=DriverReadSessions(owned, ReadDriverDouble(content), ticket=None),
        tools=tools, policy=MediaPolicy(Decimal("0")),
        occurred_at=lambda: 1_750_000_100_000_000,
        digest_supported=True, digest=_Digest(content))


@pytest.mark.asyncio
async def test_real_dual_video_repair_survives_restart_and_decodes_every_stream(pipeline, tmp_path):
    if shutil.which("ffprobe") is None:
        pytest.skip("需要真实 ffprobe")
    source = _make_dual_video_input(tmp_path)
    content = source.read_bytes()
    before = hashlib.sha256(content).hexdigest()
    owned, roots = pipeline[:2]
    owned.connection.execute("UPDATE device_files SET original_name='clip.OSV',media_type=NULL,size_bytes=? WHERE id=11", (len(content),))
    owned.connection.execute("UPDATE recording_processing SET check_basis_json=? WHERE id=1",
        ('{"reason":2,"target_duration_ms":500}',))
    owned.connection.commit()
    first_tools = ObservedTools(roots, pause=True)
    first = await run_recording_media(flow(owned, roots, content, first_tools), 1, 1, 11,
        target_extension="osv")
    assert first.phase is RepairExecutionPhase.TOOL_NOT_COMPLETED
    status = load_processing_status(owned, 1)
    assert status.repair_state == 4 and status.source_probe is not None
    assert [s.codec_type for s in status.source_probe.streams].count("video") == 3
    input_name = owned.connection.execute("SELECT relative_path FROM intermediate_files WHERE purpose=2").fetchone()[0]
    assert input_name.endswith(".osv")
    assert (roots.staging / input_name).read_bytes() == content
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    owned.connection.close()
    reopened = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        tools = ObservedTools(roots)
        result = await run_recording_media(flow(reopened, roots, content, tools), 1, 1, 11,
            target_extension="osv")
        assert result.phase is RepairExecutionPhase.SUCCEEDED, result
        assert tools.probes == [FilePurpose.REPAIR_OUTPUT]
        output = roots.staging / result.output_file.relative_path
        probe = saved_output_probe(reopened.connection, result.output_file.file_id)
        assert len(probe.streams) == 3
        assert [s.codec_type for s in probe.streams] == ["video", "video", "audio"]
        assert all(s.duration_s >= Decimal("0.5") for s in probe.streams)
        decoded = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-xerror",
            "-i", str(output), "-map", "0:v", "-map", "0:a", "-f", "null", "-"],
            capture_output=True, text=True)
        assert decoded.returncode == 0, decoded.stderr
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        assert hashlib.sha256((roots.staging / input_name).read_bytes()).hexdigest() == before
    finally:
        reopened.connection.close()
