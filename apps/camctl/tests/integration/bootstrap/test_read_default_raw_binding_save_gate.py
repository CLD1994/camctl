"""真实完整 READ 缺必要摘要时，原绑定收场保存失败仍阻止业务推进。"""

from dataclasses import replace
from pathlib import Path
import os
from types import SimpleNamespace

import pytest

from camctl.capture.media_flow import run_recording_media
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import check_binding
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.transactions import saved_transaction_events

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_input_copy import _CONTENT
from ..support.bootstrap_consumers import consumer_world, StorageGate, TransactionFault
from .test_read_default_consumers import _snapshot

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("entrance", ["normal", "residual"])
@pytest.mark.parametrize("branch", ["finish_unknown", "child_rollback"])
async def test_raw_binding_save_gate_preserves_original_requests(pipeline, capture_resources, monkeypatch, entrance, branch):
    world = await consumer_world(pipeline, capture_resources)
    stage = "finish" if branch == "finish_unknown" else "child"
    hashes = []
    actual_open = os.open

    def local_digest_unavailable(path, flags, *args, **kwargs):
        if isinstance(path, (str, Path)) and flags & os.O_ACCMODE == os.O_RDONLY:
            target = Path(path)
            if target.is_relative_to(world.roots.staging) and target.is_file():
                hashes.append(target)
                raise OSError("实际 READ 已结束，本地摘要文件暂不可读")
        return actual_open(path, flags, *args, **kwargs)

    try:
        ended_at = world.wall[0]
        with monkeypatch.context() as fault:
            fault.setattr(os, "open", local_digest_unavailable)
            with pytest.raises(ConsistencyError):
                await run_recording_media(world.runtime.media, 1, 1, world.source_id)
        assert len(hashes) == len(world.reader.requests) == 1
        ticket = world.reader.requests[0][0]
        copy_id = int(ticket.target_id)
        assert len(world.ends) >= 1 and all(end.stopped and end.error is None and end.bytes_read == len(_CONTENT)
                                            for end in world.ends)
        assert hashes[0].read_bytes() == _CONTENT
        progress = world.owned.connection.execute(
            "SELECT committed_bytes,source_size,round,recopies_used,source_sha256,target_sha256,verification_state,slot_device_id"
            " FROM file_copies WHERE id=?", (copy_id,)).fetchone()
        assert progress[:2] == (len(_CONTENT), len(_CONTENT))
        assert progress[4:6] == (None, None)
        assert progress[6:] == (int(enum_for("file_copies.verification_state").NOT_PERFORMED), "cam-1")
        assert world.owned.connection.execute(
            "SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (1, None)
        assert world.owned.connection.execute(
            "SELECT checksum_support FROM device_files WHERE id=?", (world.source_id,)).fetchone() == (
                int(enum_for("device_files.checksum_support").SUPPORTED),)
        assert not any(name == "digest" for name, _ in world.driver.calls) and world.tools() == []
        await world.reconnect()
        world.wall[0] += 1000
        formed_at = world.wall[0]
        world.prime_clock()
        finishes, children, receipts = [], [], []
        actual_finish, actual_child = OperationRepository.finish_binding_read, CaptureRepository.finish_binding_failure

        def submit(actual, request, key, owned, operation, inputs):
            inputs.append((request, key))
            if stage == operation and len(inputs) == 1:
                fault = TransactionFault(owned.connection, after_commit=stage == "finish")
                result = actual(request, key, replace(owned, connection=fault))
                assert result.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
                receipts.append(result)
                return result
            return actual(request, key, owned)

        def finish(repository, request, key, owned, *, current_identity=None):
            assert request.ticket == ticket
            return submit(lambda *args: actual_finish(repository, *args, current_identity=current_identity), request, key,
                owned, "finish", finishes)

        def child(repository, request, key, owned):
            assert saved_transaction_events(owned.connection, finishes[0][1]) is not None
            return submit(lambda *args: actual_child(repository, *args), request, key,
                owned, "child", children)

        monkeypatch.setattr(OperationRepository, "finish_binding_read", finish)
        monkeypatch.setattr(CaptureRepository, "finish_binding_failure", child)
        binding = lambda saved: check_binding(saved, SimpleNamespace(devices={}))
        await world.replay(entrance, failure=True, binding=binding)
        assert len(receipts) == 1 and len(finishes) == 1
        assert len(children) == (1 if stage == "child" else 0)
        assert finishes[0][0].occurred_at == ended_at and formed_at > ended_at
        if children:
            assert children[0][0].occurred_at == formed_at
        inputs = finishes if stage == "finish" else children
        original = inputs[0]
        gate = StorageGate(original_key=stage == "finish", unknown=stage == "finish",
            table="recording_processing", column="check_state")
        gate.install(monkeypatch, original[1])
        before = {table: _snapshot(world.owned, table, identity) for table, identity in (
            ("actions", 1), ("recording_processing", 1), ("file_copies", copy_id),
            ("operation_runs", ticket.run_id), ("device_files", world.source_id))}
        history = world.owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        calls = tuple(world.driver.calls)
        for iteration in range(2):
            # 申请形成端口的时刻推进；会话接纳钟保持已检查的同会话读数。
            world.wall[0] += 1000
            await world.replay(entrance, failure=True, binding=binding)
            assert len(gate.receipts) == iteration + 1
            expected = DbOutcomeKind.UNKNOWN if stage == "finish" else DbOutcomeKind.ROLLED_BACK
            assert gate.receipts[-1].kind is expected
            assert gate.hits == gate.rollbacks == iteration + 1
            assert len(inputs) == iteration + 2 and all(pair == original for pair in inputs)
            assert (saved_transaction_events(world.owned.connection, original[1]) is not None) is (stage == "finish")
            assert before == {table: _snapshot(world.owned, table, identity) for table, identity in (
                ("actions", 1), ("recording_processing", 1), ("file_copies", copy_id),
                ("operation_runs", ticket.run_id), ("device_files", world.source_id))}
            assert world.owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall() == history
            assert tuple(world.driver.calls) == calls and len(world.reader.requests) == 1 and world.tools() == []
            assert hashes[0].read_bytes() == _CONTENT
            assert world.owned.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?",
                (copy_id,)).fetchone() == ("cam-1",)
            if stage == "finish":
                assert len(children) == 0
            else:
                assert len(finishes) == 1
        gate.enabled = False
        await world.replay(entrance, binding=binding)
        assert inputs[-1] == original and len(inputs) == 4
        assert children[-1][0].occurred_at == formed_at and finishes[0][0].occurred_at == ended_at
        assert world.owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (4,)
        assert len(world.reader.requests) == 1 and tuple(world.driver.calls) == calls and world.tools() == []
    finally:
        await world.close()
        world.owned.connection.close()
