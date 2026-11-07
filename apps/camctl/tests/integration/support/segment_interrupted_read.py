"""独立进程在真实 READ 进行中等待，供父进程验证中断后的恢复边界。"""
import asyncio
import json
import hashlib
import os
from decimal import Decimal
from pathlib import Path
import sys
from threading import Event

from camctl.capture.media import MediaPolicy
from camctl.capture.media_flow import DriverReadSessions, MediaFlow
from camctl.capture.segment_inspections import SegmentInspectionRequest, inspect_segment
from camctl.contracts.video_coverage import CoverageMetric, CoverageTarget
from camctl.devices.read_session import ReadSession
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository, register_operation_guards
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories.segments import register_segment_guards
from camctl.host_files.models import BoundDirectories
from camctl.host_files.tasks import FileTaskExecutor
from camctl.outputs.qualification import OperationConfig
from camctl.session.supervision import Supervisor

from .database import business_database
from .segment_inspection_cases import Digest, FailedDigest, ReadDriver, CONTENT, SegmentTools


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


async def run(path, staging, action_id, member_id, occurred_at, marker, mode):
    register_capture_guards()
    register_operation_guards()
    register_outputs_guards()
    register_segment_guards()
    driver = BlockingDriver(marker)
    digest = Digest('0' * 64)
    if mode in ('finished', 'eof'):
        original = OperationRepository.finish_attempt
        def unavailable(repository, command, key, owned):
            if command.ticket.operation == 'read':
                return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=OSError('read finish unavailable'))
            return original(repository, command, key, owned)
        if mode == 'finished':
            OperationRepository.finish_attempt = unavailable
            digest = FailedDigest()
        else:
            from camctl.persistence.repositories import read_end
            def unavailable_end(request, key, owned):
                return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=OSError('read end unavailable'))
            read_end.save_read_end = unavailable_end
            digest = Digest(hashlib.sha256(CONTENT).hexdigest())
        driver = ReadDriver(CONTENT)
    async with business_database(path) as database:
        flow = MediaFlow(database=database, roots=BoundDirectories(staging=staging),
            sessions=DriverReadSessions(database, driver, ticket=None),
            tools=SegmentTools(), policy=MediaPolicy(repair_margin_s=Decimal('2')),
            occurred_at=lambda: occurred_at, digest_supported=True,
            digest=digest, segment_size=4, retry_interval_s=Decimal('0'),
            file_executor=FileTaskExecutor(Supervisor()))
        request = SegmentInspectionRequest(action_id, member_id,
            CoverageTarget(CoverageMetric.DURATION_S, 60),
            OperationConfig(3, Decimal('10'), Decimal('0')), occurred_at)
        try:
            await inspect_segment(flow, request)
        except Exception as error:
            if mode not in ('finished', 'eof'):
                raise
            marker.write_text(json.dumps({'error': str(error), 'offsets': driver.offsets,
                'digest_calls': digest.calls}))
            os._exit(0)
        raise AssertionError('进程应在原 READ 保存之前被终止')


if __name__ == '__main__':
    asyncio.run(run(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]),
        int(sys.argv[4]), int(sys.argv[5]), Path(sys.argv[6]), sys.argv[7] if len(sys.argv) > 7 else 'blocked'))
