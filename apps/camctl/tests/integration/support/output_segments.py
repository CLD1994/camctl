"""分段副本消费者通过正式拍摄与取回工厂组合真实数据库和文件任务。"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import threading

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.capture.handlers import capture_handler
from camctl.capture.result_inputs import RESULT_PAGE_CONTRACT
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryRead
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.devices.read_session import ReadChunk, ReadSession
from camctl.contracts.pages import Page
from camctl.host_files.io import LocalSourceReader
from camctl.host_files.media import ProbeRequest
from camctl.host_files.tasks import FileTaskExecutor
from camctl.outputs.obtain_flow import advance_obtain
from camctl.session.supervision import Supervisor

from .outputs import obtain_factory, output_drivers, submit_output_document
from ..capture.result_consumer_fixtures import consumer_world, returned
from ..capture.test_action6_segmented_normal import Action6RecordCatalog
from ..capture.test_capture_contract import _NOW
from ..capture.test_record_file_completion_wait import _REGISTRY, _completed_result, _stop_context
from ..persistence.test_segment_file_pairing import _entry

CONTENT = b"0123456789"


class ReadStream:
    def __init__(self, content):
        self.content, self.position = content, 0

    def read(self, limit):
        data = self.content[self.position:self.position + limit]
        self.position += len(data)
        return data

    def stop(self):
        pass

    def close(self):
        pass


class ReadDriver:
    def __init__(self, content=CONTENT, *, unavailable=False):
        self.content, self.unavailable, self.offsets = content, unavailable, []

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        self.offsets.append(offset)
        if self.unavailable:
            raise OSError("injected device source unavailable")
        return ReadSession(source, offset, ReadStream(self.content[offset:]), idle_timeout_s)

    async def digest(self, request):
        return DeviceCallResult(observations=(DeviceObservation("file_digest", 1, {
            "file_id": request.params["file_id"],
            "sha256": hashlib.sha256(self.content).hexdigest(),
        }),), error=None)


class SegmentCatalog(Action6RecordCatalog):
    def action_types(self):
        return super().action_types() | {"obtain_action_outputs"}


READ_EVIDENCE = EvidenceRegistry((
    EvidenceContract("read_returned", 1, "read", frozenset()),
    EvidenceContract("file_digest", 1, "digest", frozenset({"file_id", "sha256"}),
                     identity_field="file_id"),
))


@dataclass
class SegmentWorld:
    owned: object
    capture: object
    action_id: int
    clock: dict
    obtain: object
    driver: ReadDriver
    directories: object
    executor: FileTaskExecutor
    indexes: tuple
    input_file_id: int | None = None
    input_path: Path | None = None
    source_file_id: int | None = None

    async def advance(self):
        await advance_obtain(self.obtain)

    async def accept_obtain(self, mode, request_id):
        instant = datetime.fromtimestamp(self.clock['wall'] // 1_000_000,
                                        timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        params = {"source": {"action_instance_id": str(self.action_id)}}
        if mode == "preview":
            params['filter'] = 'preview'
        accepted = await submit_output_document(self.capture.database,
            catalog=SegmentCatalog(), occurred_at=self.clock['wall'], body={
                "request_id": str(request_id), "created_at": instant, "name": "分段取回",
                "actions": [{"name": "取回", "type": "obtain_action_outputs",
                             "scheduled_at": instant, "params": params}],
            })
        assert accepted.plan_id is not None, accepted
        return self.owned.connection.execute(
            "SELECT id FROM actions WHERE plan_id=?", (accepted.plan_id,)).fetchone()[0]

    def selection(self, action_id):
        return self.owned.connection.execute(
            "SELECT s.id FROM obtain_source_selections s JOIN action_dependencies d"
            " ON d.id=s.dependency_id WHERE d.action_id=?", (action_id,)).fetchone()[0]

    async def publish(self):
        self.clock.update(wall=_NOW + 12_000_000, mono=17_000_000_000)
        await capture_handler('camera_record')(self.action_id, self.capture)
        assert self.owned.connection.execute('SELECT status FROM actions WHERE id=?',
            (self.action_id,)).fetchone() == (3,)
        published = self.owned.connection.execute(
            'SELECT target_duration_ms,published_event_id,assessment_json FROM segment_sets'
            ' WHERE action_id=? AND published_event_id IS NOT NULL',
            (self.action_id,)).fetchone()
        assert published is not None and published[0] == 12_000
        assessment = json.loads(published[2])
        assert assessment['target'] == 12
        assert assessment['coverage_status'] == 'not_performed'
        assert assessment['checked_count'] == 0
        assert assessment['default_scope']['member_count'] == len(self.indexes)
        self.clock.update(wall=_NOW + 23_000_000, mono=20_000_000_000)


@asynccontextmanager
async def segment_world(tmp_path, *, indexes=(1,), unavailable=False):
    from camctl.persistence.repositories.file_batches import register_file_batch_guards

    register_file_batch_guards()
    world = {}
    clock = {'wall': _NOW, 'mono': 5_000_000_000}
    class CaptureDriver(ReadDriver):
        async def control(self, request):
            return DeviceCallResult.from_outcome(returned('operation', 'start_confirmed', int(request.ticket.target_id)))
        async def stop(self, request):
            activity, = world['owned'].connection.execute(
                'SELECT id FROM device_activities WHERE action_id=?', (world['action'],)).fetchone()
            return DeviceCallResult.from_outcome(returned('stop', 'stop_confirmed', activity))
        async def read_directory(self, request, *, stop=None):
            return DirectoryRead(None, None, Page((), None))
    class Results:
        async def list_results(self, request, _batch):
            entries=[]
            for index in indexes:
                stem=f'DJI_001/DJI_20261022100000_{index:04d}_D.'
                entries.extend((_entry(stem+'MP4'), _entry(stem+'LRF')))
            return _completed_result(request.ticket, _stop_context(world['owned'], world['action']),
                entries=sorted(entries,key=lambda item:item['identity']), finalized=True)
    evidence=EvidenceRegistry((RESULT_PAGE_CONTRACT,)+tuple(_REGISTRY.contract(name,1) for name in (
        'results_returned','adb_foreground_recovery','operation_returned',
        'start_confirmed','stop_returned','stop_confirmed')) +
        (READ_EVIDENCE.contract('read_returned',1),READ_EVIDENCE.contract('file_digest',1)))
    driver=CaptureDriver()
    registry=DriverRegistry((DriverEntry('camctl-adb',driver,DriverDeclaration(
        control_supported=True,stop_supported=True,query_supported=False,result_supported=False,
        read_supported=True,digest_supported=True,delete_supported=False,directory_supported=True),
        evidence,DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
    probe=tmp_path/'probe-tool'
    probe.write_text('#!'+sys.executable+'\nimport json,sys\nfrom pathlib import Path\nassert Path(sys.argv[-1]).read_bytes()=='+repr(CONTENT)+'\nprint(json.dumps('+repr({
        'format': {'duration':'1.5'},'streams':[{'index':0,'codec_type':'video','codec_name':'h264',
        'width':1920,'height':1080,'duration':'1.5','nb_read_frames':'45','r_frame_rate':'30/1',
        'disposition':{'attached_pic':0}}]})+'))\n')
    probe.chmod(0o755)
    supervisor=Supervisor();executor=FileTaskExecutor(supervisor)
    owned,capture,action_id,_=await consumer_world(tmp_path,'record',catalog=Action6RecordCatalog(),start=False,
        runtime_options=dict(drivers=registry,devices={'cam-1':{'driver':'camctl-adb','copy':{
            'max_read_attempts':3,'read_idle_timeout_s':Decimal(10),'retry_interval_s':Decimal(5)}}},
            evidence=evidence,results=DriverResultListing(Results(),DeviceBinding('cam-1','camctl-adb'),evidence),
            probe_request=ProbeRequest(ffprobe=str(probe)),wall_us=lambda:clock['wall'],
            monotonic_ns=lambda:clock['mono']))
    world.update(owned=owned,action=action_id)
    staging=Path(capture.database.metadata.staging_path)
    for name in ('recording-inputs','derived','deliveries'):(staging/name).mkdir(parents=True,exist_ok=True)
    device_reader=ReadDriver(unavailable=unavailable)
    factory=obtain_factory(devices={'cam-1':{'driver':'camctl-adb','copy':{
        'max_read_attempts':3,'read_idle_timeout_s':Decimal(10),'retry_interval_s':Decimal(5)}}},
        drivers=output_drivers(device_reader,READ_EVIDENCE,read=True,digest=True),
        staging=staging,ready=tmp_path/'ready',processing=tmp_path/'processing',segment_size=4,
        occurred_at=lambda:clock['wall'],monotonic_ns=lambda:clock['mono'],file_executor=executor)
    obtain=factory(capture.database)
    for path in (obtain.directories.ready,obtain.directories.processing):path.mkdir(exist_ok=True)
    scenario=SegmentWorld(owned,capture,action_id,clock,obtain,device_reader,obtain.directories,executor,indexes)
    try:
        await capture_handler('camera_record')(action_id,capture)
        yield scenario
    finally:
        result=await supervisor.drain_required()
        owned.connection.close()
        assert not result.failed,result.failed


@asynccontextmanager
async def checked_segment_world(tmp_path, capture_resources, monkeypatch, *,
                                early_waiters, unavailable=False):
    """异常录像经正式检查发布后，为已等待的取回保留其检查副本。"""
    from types import SimpleNamespace
    from camctl.persistence.repositories.file_batches import register_file_batch_guards
    from .bootstrap_read_cases import segment_input_case

    register_file_batch_guards()
    driver = ReadDriver(unavailable=unavailable)
    source = await segment_input_case(
        tmp_path, capture_resources, monkeypatch, (Decimal("1.5"),),
        early_waiters=early_waiters, read_retry_s=Decimal(5), segment_size=4,
        source_target_s=Decimal(12), expected_source_status=4,
        obtain_max_read_attempts=3,
        obtain_drivers=output_drivers(driver, READ_EVIDENCE, read=True, digest=True),
        driver_id="camctl-adb", result_entries=[
            _entry('DJI_001/DJI_20261022100000_0001_D.LRF'),
            _entry('DJI_001/DJI_20261022100000_0001_D.MP4')])
    try:
        await source.publish()
        row = source.owned.connection.execute(
            "SELECT target_duration_ms,assessment_json FROM segment_sets"
            " WHERE action_id=? AND published_event_id IS NOT NULL", (source.source_id,)).fetchone()
        assessment = json.loads(row[1])
        assert row[0] == 12_000 and assessment['target'] == 12
        assert assessment['coverage_status'] == 'confirmed_short'
        assert assessment['confirmed'] == 1.5 and assessment['checked_count'] == 1
        assert len(source.inputs) == 1
        input_file_id, _relative, _state = source.inputs[0]
        input_path = source.paths[input_file_id]
        assert input_path.read_bytes() == CONTENT
        source_file_id, device_id, driver_id, locator, target, result = source.owned.connection.execute(
            "SELECT c.source_device_file_id,a.device_id,a.driver_id,f.locator_json,"
            " i.target_json,i.result_json FROM file_copies c"
            " JOIN segment_inspections i ON i.id=c.inspection_id"
            " JOIN actions a ON a.id=i.action_id"
            " JOIN device_files f ON f.id=c.source_device_file_id"
            " WHERE c.target_file_id=?", (input_file_id,)).fetchone()
        assert (device_id, driver_id) == ('cam-1', 'camctl-adb')
        assert json.loads(locator) == {'path': '/DCIM/DJI_001/DJI_20261022100000_0001_D.MP4'}
        assert json.loads(target) == {'metric': 'duration_s', 'value': 12}
        assert json.loads(result)['metric']['value'] == 1.5
        obtain = source.obtain_factory(source.database)

        class Clock:
            def __getitem__(self, key):
                return (source.wall if key == 'wall' else source.mono)[0]

            def __setitem__(self, key, value):
                (source.wall if key == 'wall' else source.mono)[0] = value

        async def advance():
            await advance_obtain(obtain)

        async def accept_obtain(mode, request_id):
            action, _selection = await source.add_waiter(request_id, mode, start_only=True)
            return action

        yield SimpleNamespace(
            owned=source.owned, action_id=source.source_id, clock=Clock(), obtain=obtain,
            driver=driver, directories=obtain.directories, executor=source.executor,
            input_file_id=input_file_id, input_path=input_path, source_file_id=source_file_id,
            early=source.early, advance=advance, accept_obtain=accept_obtain)
    finally:
        await source.close()


class LocalReadGate:
    """真实文件线程到达首段后等待；可让原读者在已保存一段后报错。"""
    def __init__(self, monkeypatch, *, fail_after_one=False):
        self.entered,self.release=threading.Event(),threading.Event()
        self.reader=None;self.chunks=0;self.fail_after_one=fail_after_one
        original=LocalSourceReader.read_chunk
        def read(reader,limit):
            if self.reader is None:
                self.reader=reader;self.entered.set()
                if not self.release.wait(20):raise TimeoutError('本地读取门闩未释放')
            if reader is self.reader:
                if self.fail_after_one and self.chunks>=1:
                    return ReadChunk(data=None,error='injected local source failure')
                result=original(reader,limit)
                if result.data is not None:self.chunks+=1
                return result
            return original(reader,limit)
        monkeypatch.setattr(LocalSourceReader,'read_chunk',read)

    async def wait(self, task):
        for _ in range(2000):
            if self.entered.is_set():return
            if task.done():await task;raise AssertionError('正式取回未到达本地读取')
            await asyncio.sleep(.01)
        self.release.set()
        await task
        raise AssertionError('本地读取未在20秒内开始')


@asynccontextmanager
async def paused_local_read(world, monkeypatch, *, fail_after_one=False):
    """文件门闩与该轮正式推进一起收场，断言失败也先等真实任务结束。"""
    gate = LocalReadGate(monkeypatch, fail_after_one=fail_after_one)
    advancing = asyncio.create_task(world.advance())
    try:
        await gate.wait(advancing)
        yield gate, advancing
    finally:
        gate.release.set()
        if not advancing.done():
            await advancing


@asynccontextmanager
async def bound_local_delivery(tmp_path, capture_resources, monkeypatch):
    """真实取回已开始首段；调用方可在文件线程门闩处注入摘要结果。"""
    async with checked_segment_world(tmp_path, capture_resources, monkeypatch,
                                     early_waiters=(40,)) as world:
        action_id, _selection = world.early[0]
        async with paused_local_read(world, monkeypatch) as (gate, advancing):
            copy_id, source, device, target = world.owned.connection.execute(
                'SELECT c.id,c.source_intermediate_file_id,c.source_device_file_id,c.target_file_id'
                ' FROM file_copies c JOIN deliveries d ON d.id=c.delivery_id WHERE d.action_id=?',
                (action_id,)).fetchone()
            assert source == world.input_file_id and device is None
            assert target is not None
            yield world, action_id, copy_id, gate, advancing
