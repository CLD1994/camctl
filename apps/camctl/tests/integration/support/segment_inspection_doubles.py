"""分段检查使用的契约驱动、摘要及探测替身。"""
from decimal import Decimal

from camctl.host_files.media import MediaProbe, MediaStream
from camctl.host_files.tasks import FileTaskId, FileTaskResult

CONTENT = b"0123456789"


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
