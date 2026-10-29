"""检查副本本地取回的重拷沿用原 READ_FILE 责任。"""

import hashlib
import json

import pytest

import camctl.outputs.copy as copy_module
from camctl.contracts.enums import enum_for
from camctl.devices.read_session import ReadChunk
from camctl.host_files.io import LocalSourceReader
from camctl.outputs.obtain_flow import advance_obtain
from ..support.output_segments import CONTENT as _CONTENT, bound_local_delivery

pytestmark = pytest.mark.asyncio


async def _inject_target_mismatch(monkeypatch, mismatch_count):
    original = copy_module.hash_target
    calls = 0

    def injected(ref, roots):
        nonlocal calls
        result = original(ref, roots)
        if result.error is None:
            calls += 1
            if calls <= mismatch_count:
                return type(result)(digest="0" * 64,
                                    size_bytes=result.size_bytes, error=None)
        return result

    monkeypatch.setattr(copy_module, "hash_target", injected)
    return lambda: calls


def _read_attempt_state(owned, copy_id):
    run = owned.connection.execute(
        "SELECT id,status,attempts_used,max_attempts_used,retry_wait_required,error_json"
        " FROM operation_runs WHERE copy_id=? AND kind=3", (copy_id,),
    ).fetchone()
    attempts = owned.connection.execute(
        "SELECT attempt_no,status,error_json FROM operation_attempts"
        " WHERE run_id=? ORDER BY attempt_no", (run[0],),
    ).fetchall()
    return run, attempts


async def _advance_until_terminal(world, owned, action_id, *, ticks=5):
    for index in range(ticks):
        world.clock['mono'] = (20 + index * 10) * 1_000_000_000
        await world.advance()
        status, = owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (action_id,),
        ).fetchone()
        if status != 2:
            break


@pytest.mark.parametrize("mismatch_count,expected", [(1, "ready"), (2, "exhausted")])
async def test_local_recopy_keeps_read_attempt_and_checksum_exhaustion_is_final(
        tmp_path, monkeypatch, mismatch_count, expected):
    async with bound_local_delivery(tmp_path, monkeypatch) as (world, action_id, copy_id, gate, advancing):
        owned, runtime = world.owned, world.obtain
        input_path, directories = world.input_path, world.directories
        digest_calls = await _inject_target_mismatch(monkeypatch, mismatch_count)

        # 首次目标摘要不一致后登记第二轮；原 READ_FILE 必须仍为同一个 RUNNING attempt。
        gate.release.set()
        await advancing
        first_copy = owned.connection.execute(
            "SELECT round,recopies_used,committed_bytes"
            " FROM file_copies WHERE id=?", (copy_id,),
        ).fetchone()
        assert first_copy[:3] == (2, 1, 0)
        assert digest_calls() == 1
        target_relative_path, = owned.connection.execute(
            "SELECT f.relative_path FROM file_copies c"
            " JOIN intermediate_files f ON f.id=c.target_file_id WHERE c.id=?",
            (copy_id,),
        ).fetchone()
        assert (directories.staging / target_relative_path).read_bytes() == _CONTENT
        run, attempts = _read_attempt_state(owned, copy_id)
        assert run[1:5] == (int(enum_for("operation_runs.status").ACTIVE), 1, 3, 0)
        assert [(row[0], row[1]) for row in attempts] == [
            (1, int(enum_for("operation_attempts.status").RUNNING))]

        await _advance_until_terminal(world, owned, action_id)
        run, attempts = _read_attempt_state(owned, copy_id)
        assert runtime.devices[next(iter(runtime.devices))].driver.offsets == []
        if expected == "ready":
            assert owned.connection.execute(
                "SELECT status FROM actions WHERE id=?", (action_id,),
            ).fetchone()[0] == int(enum_for("actions.status").SUCCEEDED)
            assert len(attempts) == 1
            assert attempts[0][1] == int(enum_for("operation_attempts.status").SUCCEEDED)
            assert run[1:5] == (int(enum_for("operation_runs.status").SUCCEEDED), 1, 3, 0)
            assert owned.connection.execute(
                "SELECT round,recopies_used,verification_state FROM file_copies WHERE id=?",
                (copy_id,),
            ).fetchone() == (2, 1, 3)
            assert digest_calls() == 2
        else:
            assert owned.connection.execute(
                "SELECT status FROM actions WHERE id=?", (action_id,),
            ).fetchone()[0] == int(enum_for("actions.status").FAILED)
            assert len(attempts) == 1
            assert attempts[0][1] == int(enum_for("operation_attempts.status").SUCCEEDED)
            assert run[1:5] == (int(enum_for("operation_runs.status").FAILED), 1, 3, 0)
            assert json.loads(run[5])["code"] == "checksum_mismatch"
            delivery_error, = owned.connection.execute(
                "SELECT error_json FROM deliveries WHERE action_id=?", (action_id,),
            ).fetchone()
            assert json.loads(delivery_error)["code"] == "checksum_mismatch"
            assert owned.connection.execute(
                "SELECT round,recopies_used,verification_state FROM file_copies WHERE id=?",
                (copy_id,),
            ).fetchone() == (2, 1, 4)
            assert digest_calls() == 2
        assert input_path.read_bytes() == _CONTENT


async def test_local_source_failure_after_recopy_falls_back_with_original_budget(
        tmp_path, monkeypatch):
    async with bound_local_delivery(tmp_path, monkeypatch) as (world, action_id, copy_id, gate, advancing):
        owned, runtime = world.owned, world.obtain
        input_path, directories = world.input_path, world.directories
        target_digest_calls = await _inject_target_mismatch(monkeypatch, 1)
        gate.release.set()
        await advancing
        assert target_digest_calls() == 1
        assert owned.connection.execute(
            "SELECT round,recopies_used,committed_bytes FROM file_copies WHERE id=?",
            (copy_id,),
        ).fetchone() == (2, 1, 0)
        read_identity, = owned.connection.execute(
            "SELECT a.id FROM operation_attempts a"
            " JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.copy_id=? AND r.kind=3 AND a.attempt_no=1", (copy_id,),
        ).fetchone()
        original_target, = owned.connection.execute(
            "SELECT target_file_id FROM file_copies WHERE id=?", (copy_id,),
        ).fetchone()
        assert owned.connection.execute(
            "SELECT status,copy_round FROM operation_attempts WHERE id=?",
            (read_identity,),
        ).fetchone() == (
            int(enum_for("operation_attempts.status").RUNNING), 1)

        original_read_chunk = LocalSourceReader.read_chunk
        failing_reader = None
        chunks_read = 0

        def fail_after_one_local_chunk(reader, limit):
            nonlocal failing_reader, chunks_read
            if failing_reader is None:
                failing_reader = reader
            if reader is failing_reader and chunks_read >= 1:
                return ReadChunk(data=None, error="injected recopy source failure")
            result = original_read_chunk(reader, limit)
            if reader is failing_reader and result.data is not None:
                chunks_read += 1
            return result

        monkeypatch.setattr(LocalSourceReader, "read_chunk", fail_after_one_local_chunk)
        await advance_obtain(runtime)

        source_device_id, = owned.connection.execute(
            "SELECT o.device_file_id FROM file_copies c"
            " JOIN deliveries d ON d.id=c.delivery_id"
            " JOIN outputs o ON o.id=d.output_id WHERE c.id=?", (copy_id,),
        ).fetchone()
        copy_row = owned.connection.execute(
            "SELECT source_intermediate_file_id,source_device_file_id,round,"
            " recopies_used,committed_bytes,target_file_id"
            " FROM file_copies WHERE id=?", (copy_id,),
        ).fetchone()
        assert copy_row[:5] == (None, source_device_id, 2, 1, 4)
        assert copy_row[5] == original_target
        assert input_path.read_bytes() == _CONTENT
        target_path, = owned.connection.execute(
            "SELECT relative_path FROM intermediate_files WHERE id=?",
            (copy_row[5],),
        ).fetchone()
        assert (directories.staging / target_path).read_bytes() == _CONTENT[:4]
        assert runtime.file_executor.unfinished_files() == ()

        run, attempts = _read_attempt_state(owned, copy_id)
        assert run[1:5] == (int(enum_for("operation_runs.status").ACTIVE), 1, 3, 1)
        assert run[0] == owned.connection.execute(
            "SELECT run_id FROM operation_attempts WHERE id=?", (read_identity,),
        ).fetchone()[0]
        assert len(attempts) == 1
        assert attempts[0][0:2] == (
            1, int(enum_for("operation_attempts.status").FAILED))
        assert owned.connection.execute(
            "SELECT copy_round FROM operation_attempts WHERE run_id=?"
            " ORDER BY attempt_no", (run[0],),
        ).fetchone()[0] == 1

        device_reader = runtime.devices[next(iter(runtime.devices))].driver
        assert device_reader.offsets == []
        world.clock['mono'] = 25_000_000_000
        for _ in range(5):
            await advance_obtain(runtime)
            status, = owned.connection.execute(
                "SELECT status FROM actions WHERE id=?", (action_id,),
            ).fetchone()
            if status != int(enum_for("actions.status").RUNNING):
                break

        assert device_reader.offsets == [4]
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (action_id,),
        ).fetchone()[0] == int(enum_for("actions.status").SUCCEEDED)
        run, attempts = _read_attempt_state(owned, copy_id)
        assert run[1:5] == (
            int(enum_for("operation_runs.status").SUCCEEDED), 2, 3, 0)
        assert [(row[0], row[1]) for row in attempts] == [
            (1, int(enum_for("operation_attempts.status").FAILED)),
            (2, int(enum_for("operation_attempts.status").SUCCEEDED)),
        ]
        assert owned.connection.execute(
            "SELECT id,source_intermediate_file_id,source_device_file_id,round,"
            "recopies_used,committed_bytes,target_file_id,verification_state"
            " FROM file_copies WHERE id=?", (copy_id,),
        ).fetchone() == (
            copy_id, None, source_device_id, 2, 1, len(_CONTENT), original_target,
            int(enum_for("file_copies.verification_state").MATCHED),
        )
        delivery_id, delivery_name, delivery_status = owned.connection.execute(
            "SELECT id,file_name,status FROM deliveries WHERE action_id=?",
            (action_id,),
        ).fetchone()
        assert delivery_status == int(enum_for("deliveries.status").PUBLISHED)
        published_path = directories.ready / delivery_name
        assert published_path.read_bytes() == _CONTENT
        assert hashlib.sha256(published_path.read_bytes()).hexdigest() == owned.connection.execute(
            "SELECT target_sha256 FROM file_copies WHERE id=?", (copy_id,),
        ).fetchone()[0]
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts a"
            " JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.copy_id=? AND a.id=?", (copy_id, read_identity),
        ).fetchone()[0] == 1
        assert delivery_id is not None
