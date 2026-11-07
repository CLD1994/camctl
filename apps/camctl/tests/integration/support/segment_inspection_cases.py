"""由正式受理、拍摄及分段仓储准备检查所需的少量真实成员。"""
from dataclasses import replace
from decimal import Decimal

from camctl.host_files.media import MediaProbe, MediaStream
from camctl.host_files.tasks import FileTaskId, FileTaskResult

from camctl.capture.files import FileCompletionSave, FilePresenceSave, OwnershipFromResultPageSave
from camctl.capture.result_pages import ResultPageSave
from camctl.operations.models import EvidenceValue, Settlement, SettlementBasis
from camctl.operations.validation import validate_outcome
from camctl.contracts.values import new_operation_key
from camctl.persistence.repositories.segments import SegmentRepository

from ..capture.result_consumer_fixtures import consumer_world
from ..capture.test_result_page_history import _actual
from ..capture.test_record_file_completion_wait import _REGISTRY, _completion, _stop_context
from ..persistence.result_page_fixtures import begin_result_check
from ..persistence.test_segment_storage import _ok
from ..persistence.test_segment_result_sets import _finish_scan
from ..capture.test_action6_segmented_normal import Action6RecordCatalog

CONTENT = b"0123456789"


async def inspection_source(tmp_path, count=2):
    owned, runtime, action_id, _ = await consumer_world(
        tmp_path, 'record', catalog=Action6RecordCatalog(), record_control_elapsed_ns=60_000_000_000)
    try:
        ticket = begin_result_check(owned, runtime, action_id).ticket
        entries = []
        for index in range(1, count + 1):
            name = f'/DCIM/CAM_001/CAM_20261021090000_{index:04d}_D.MP4'
            entries.append({'identity': name, 'locator': {'path': name}, 'complete': True,
                'size_bytes': len(CONTENT), 'kind': 'video', 'format_id': 'mp4'})
        context = _stop_context(owned, action_id)
        actual_page = replace(_actual(ticket, entries=entries, finalized=True),
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('results_returned', 1,
                {'file_completion': _completion(context), 'file_completion_source_page_event_id': None})))
        saved = _ok(runtime.capture.save_result_page(ResultPageSave(ticket, 1, None,
            validate_outcome(ticket, actual_page, _REGISTRY), runtime.wall_us()), new_operation_key(), owned))
        page = runtime.capture.read_result_page(saved, owned)
        files = dict(page.file_ids)
        for entry in page.page.entries:
            file_id = files[entry.identity]
            for method, command in (
                (runtime.capture.save_ownership_from_result_page, OwnershipFromResultPageSave(saved, file_id, runtime.wall_us())),
                (runtime.capture.save_file_completion, FileCompletionSave(file_id, 3, runtime.wall_us(), basis=3,
                    observation=entry.evidence, size_bytes=entry.size_bytes, activity_id=int(context['activity_id']),
                    result_page_event_id=saved.event_id, stop_result_event_id=context['stop_result_event_id'])),
                (runtime.capture.save_file_presence, FilePresenceSave(file_id, 2, runtime.wall_us())),
            ):
                _ok(method(command, new_operation_key(), owned))
        _finish_scan(owned, runtime, page)
        repo = SegmentRepository()
        request = repo.prepare_result_set(action_id, runtime.wall_us(), owned)
        ref = _ok(repo.create_result_set(request, new_operation_key(), owned))
        actual = repo.read_result_member_inputs(ref, None, 128, owned)
        assert actual.next_cursor is None and len(actual.items) == count
        _ok(repo.append_result_members(ref, actual.items, runtime.wall_us(), new_operation_key(), owned))
        _ok(repo.fix_result_members(ref, runtime.wall_us(), new_operation_key(), owned))
        return owned, runtime, ref, repo.read_members(ref, None, 128, owned).items
    except BaseException:
        owned.connection.close()
        raise


class Digest:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.calls = 0

    async def read_digest(self):
        from camctl.outputs.copy import SourceDigest

        self.calls += 1
        return SourceDigest(digest=self.digest, error=None)


class FailedDigest:
    def __init__(self, error: str = "source SHA query failed") -> None:
        self.error = error
        self.calls = 0

    async def read_digest(self):
        from camctl.outputs.copy import SourceDigest

        self.calls += 1
        return SourceDigest(digest=None, error=self.error)


class Stream:
    def __init__(self, content: bytes, *, fail_after: int | None = None) -> None:
        self.content = content
        self.position = 0
        self.fail_after = fail_after
        self.read_calls = 0

    def read(self, limit: int) -> bytes:
        self.read_calls += 1
        if self.fail_after is not None and self.position >= self.fail_after:
            raise OSError("injected source read interruption")
        data = self.content[self.position:self.position + limit]
        self.position += len(data)
        return data

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None


class ReadDriver:
    def __init__(self, content: bytes, *, fail_after: int | None = None) -> None:
        self.content = content
        self.fail_after = fail_after
        self.offsets: list[int] = []
        self.streams: list[_Stream] = []
        self.chunk_calls: list[int] = []

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        from camctl.devices.read_session import ReadSession

        self.offsets.append(offset)
        stream = Stream(self.content[offset:], fail_after=self.fail_after)
        self.streams.append(stream)
        session = ReadSession(source, offset, stream, idle_timeout_s)
        original_read_chunk = session.read_chunk

        def read_chunk(limit: int):
            self.chunk_calls.append(limit)
            return original_read_chunk(limit)

        session.read_chunk = read_chunk
        return session


class SegmentTools:
    def __init__(self) -> None:
        self.requests: list[bool] = []
        self.probe = MediaProbe(
            duration_s=Decimal("1.5"),
            error=None,
            streams=(MediaStream(
                index=0,
                codec_type="video",
                codec_name="h264",
                width=1920,
                height=1080,
                duration_s=Decimal("1.5"),
                attached_pic=False,
                frame_count=45,
                fps=Decimal("30"),
            ),),
        )

    async def probe_segment(self, input, *, count_frames: bool) -> FileTaskResult:
        self.requests.append(count_frames)
        return FileTaskResult(FileTaskId("segment-probe"), ran=True, value=self.probe)

    async def probe(self, input) -> FileTaskResult:
        raise AssertionError("逐段检查必须调用显式主流探测入口")

    async def repair(self, input, output, *, trim_s):
        raise AssertionError("逐段检查不调用修复工具")
