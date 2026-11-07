"""独立进程在真实 READ 进行中等待，供父进程验证中断后的恢复边界。"""
import asyncio
from decimal import Decimal
from pathlib import Path
import sys
from threading import Event

from camctl.capture.media import MediaPolicy
from camctl.capture.media_flow import DriverReadSessions, MediaFlow
from camctl.capture.segment_inspections import SegmentInspectionRequest, inspect_segment
from camctl.contracts.video_coverage import CoverageMetric, CoverageTarget
from camctl.devices.read_session import ReadSession
from camctl.host_files.models import BoundDirectories
from camctl.host_files.tasks import FileTaskExecutor
from camctl.outputs.qualification import OperationConfig
from camctl.session.supervision import Supervisor

from .database import business_database
from .segment_inspection_cases import Digest, CONTENT, SegmentTools


class BlockingStream:
    def __init__(self, marker):
        self.marker = marker
        self.gate = Event()

    def read(self, limit):
        self.marker.write_text('read entered')
        self.gate.wait(30)
        raise RuntimeError('读取门闩未由父进程中断')

    def stop(self):
        self.gate.set()

    def close(self):
        self.gate.set()


class BlockingDriver:
    def __init__(self, marker):
        self.marker = marker

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        return ReadSession(source, offset, BlockingStream(self.marker), idle_timeout_s)


async def run(path, staging, action_id, member_id, occurred_at, marker):
    async with business_database(path) as database:
        flow = MediaFlow(database=database, roots=BoundDirectories(staging=staging),
            sessions=DriverReadSessions(database, BlockingDriver(marker), ticket=None),
            tools=SegmentTools(), policy=MediaPolicy(repair_margin_s=Decimal('2')),
            occurred_at=lambda: occurred_at, digest_supported=True,
            digest=Digest('0' * 64), segment_size=4, retry_interval_s=Decimal('0'),
            file_executor=FileTaskExecutor(Supervisor()))
        request = SegmentInspectionRequest(action_id, member_id,
            CoverageTarget(CoverageMetric.DURATION_S, 60),
            OperationConfig(3, Decimal('10'), Decimal('0')), occurred_at)
        await inspect_segment(flow, request)
        raise AssertionError('进程应在原 READ 返回之前被终止')


if __name__ == '__main__':
    asyncio.run(run(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]),
        int(sys.argv[4]), int(sys.argv[5]), Path(sys.argv[6])))
