"""以真实拍摄来源和读取结果准备独立的普通交付。"""

import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from camctl.capture.handlers import capture_handler
from camctl.capture.result_inputs import ObservedFile, RESULT_PAGE_CONTRACT
from camctl.capture.results import FileKind
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ObjectId
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.devices.read_session import ReadSession
from camctl.host_files.models import BoundDirectories
from camctl.outputs.handoff import DeliveryDirectories
from camctl.outputs.obtain_flow import advance_obtain
from camctl.persistence.runtime import verify_directory_binding

from ..bootstrap.test_media_assembly import _config
from ..capture.result_consumer_fixtures import RESULT_EVIDENCE, ResultCatalog, consumer_world
from ..capture.test_capture_contract import ResultsDouble
from .capture import capture_database
from .outputs import obtain_factory, output_drivers, prepare_obtain_selection


class _Catalog(ResultCatalog):
    def action_types(self):
        return super().action_types() | {"obtain_action_outputs"}


class _Stream(BytesIO):
    def cancel(self):
        pass


class _ReadDriver:
    def __init__(self, content):
        self.content = content
        self.reads = []
        self.digests = []

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        self.reads.append(ticket)
        return ReadSession(source, offset, _Stream(self.content[offset:]), idle_timeout_s)

    async def digest(self, request):
        self.digests.append(request)
        return DeviceCallResult((DeviceObservation("file_digest", 1, {
            "file_id": request.params["file_id"],
            "sha256": hashlib.sha256(self.content).hexdigest(),
        }),), None)


async def prepare_public_delivery(tmp_path, *, content, now, missing_output_id=None):
    """执行真实读取；可同时通过精确请求准备一个不存在的产物。"""
    cfg = _config(tmp_path)
    staging = Path(cfg.paths.staging)
    ready, processing = Path(cfg.paths.ready), Path(cfg.paths.processing)

    def capture_options(owned, _driver, _defaults):
        action_id, = owned.connection.execute("SELECT id FROM actions").fetchone()
        entry = ObservedFile(
            identity="delivery-source", locator={"path": "/DCIM/source.jpg"},
            evidence={"listing": "source"}, complete=True, size_bytes=len(content),
            kind=FileKind.PHOTO, original_name="source.jpg", media_type="image/jpeg")
        return {"staging": staging, "results": ResultsDouble({action_id: (entry,)}),
                "evidence": RESULT_EVIDENCE.with_contract(RESULT_PAGE_CONTRACT)}

    owned, capture, source_id, handler = await consumer_world(
        tmp_path, "photo", catalog=_Catalog(), start=False,
        now_us=now, runtime_options=capture_options)
    try:
        verify_directory_binding(owned.metadata, cfg.paths.staging,
                                 cfg.paths.ready, cfg.paths.processing)
        await capture_handler(handler)(source_id, capture)
        output_id, = owned.connection.execute(
            "SELECT id FROM outputs WHERE source_action_id=?", (source_id,)).fetchone()
        mode = "default"
        if missing_output_id is not None:
            missing_output_id = ObjectId(missing_output_id)
            assert owned.connection.execute(
                "SELECT 1 FROM outputs WHERE id=?", (missing_output_id,)).fetchone() is None
            mode = [output_id, missing_output_id]
        action_id, _ = prepare_obtain_selection(
            owned, catalog=_Catalog(), now=now, source_id=source_id,
            mode=mode, request_id=2)
        for directory in (staging, ready, processing):
            directory.mkdir(parents=True, exist_ok=True)
        driver = _ReadDriver(content)
        evidence = EvidenceRegistry((
            EvidenceContract("read_returned", 1, "read", frozenset()),
            EvidenceContract("file_digest", 1, "digest", frozenset({"file_id", "sha256"}),
                             identity_field="file_id"),
        ))
        factory = obtain_factory(
            devices={"cam-1": {"driver": "camctl-adb"}},
            drivers=output_drivers(driver, evidence, read=True, digest=True),
            staging=staging, ready=ready, processing=processing,
            occurred_at=lambda: now + 1, monotonic_ns=lambda: 0, segment_size=len(content))
        await advance_obtain(factory(await capture_database(owned)))
        delivery_id, copy_id, target_id, status, size, committed, source_hash, target_hash, verification, dependency = owned.connection.execute(
            "SELECT d.id,c.id,c.target_file_id,d.status,c.source_size,c.committed_bytes,"
            " c.source_sha256,c.target_sha256,c.verification_state,i.source_dependency FROM deliveries d"
            " JOIN file_copies c ON c.delivery_id=d.id JOIN obtain_items i ON i.delivery_id=d.id"
            " WHERE d.action_id=?", (action_id,),
        ).fetchone()
        expected = hashlib.sha256(content).hexdigest()
        assert (status, size, committed, source_hash, target_hash) == (3, len(content), len(content), expected, expected)
        assert verification == int(enum_for("file_copies.verification_state").MATCHED)
        assert dependency == 0
        assert len(driver.reads) == len(driver.digests) == 1
        run_status, attempts, attempt_status, result_event = owned.connection.execute(
            "SELECT r.status,r.attempts_used,a.status,a.result_event_id FROM operation_runs r"
            " JOIN operation_attempts a ON a.run_id=r.id AND a.attempt_no=r.attempts_used"
            " WHERE r.copy_id=? AND r.kind=?", (copy_id, int(enum_for("operation_runs.kind").READ_FILE)),
        ).fetchone()
        assert (run_status, attempts, attempt_status) == (
            int(enum_for("operation_runs.status").SUCCEEDED), 1,
            int(enum_for("operation_attempts.status").SUCCEEDED))
        assert result_event is not None
        relative_path, = owned.connection.execute(
            "SELECT relative_path FROM intermediate_files WHERE id=?", (target_id,)).fetchone()
        assert (staging / relative_path).read_bytes() == content
        assert not tuple(ready.iterdir()) and not tuple(processing.iterdir())
        qualification = SimpleNamespace(delivery_id=delivery_id, copy_id=copy_id,
                                        target_file_id=target_id, action_id=action_id,
                                        output_id=output_id)
        return owned, BoundDirectories(staging=staging), DeliveryDirectories(
            staging=staging, ready=ready, processing=processing), qualification
    except BaseException:
        owned.connection.close()
        raise
