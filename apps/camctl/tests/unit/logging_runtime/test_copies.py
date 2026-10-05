"""故障标记与日志副本交付的规则边界；文件与通道由替身提供。

覆盖标记存在性与内容分别判断、创建结果四分支、交付编排的证据顺
序（标记先行、锁内写入并复制、发布、进度更新失败不阻止）、失败
清理只作用于本次副本，以及启动清理的对象分类；真实文件、队列与
多进程轮换由集成测试验证。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from camctl.logging_runtime.copies import (
    COPY_PREFIX,
    MARKER_NAME,
    CopyOutcome,
    CopyRequest,
    FailureLogService,
    MarkerPresence,
    MarkerStage,
    MarkerStore,
    SaveOutcome,
    copy_file_name,
    deliver_failure_copy,
    is_copy_file_name,
    is_marker_temp_name,
    marker_temp_file_name,
    startup_sweep,
)
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord
from camctl.logging_runtime.clh_adapter import WriteResult


# ---- 专用命名规则 ----------------------------------------------------


def test_copy_names_use_dedicated_prefix():
    name = copy_file_name("ab12")
    assert name == "log-copy-ab12.log"
    assert is_copy_file_name(name)
    assert not is_marker_temp_name(name)


@pytest.mark.parametrize("name", [
    "", "log-copy-.log", "log-copy-x.txt", "log-copy-x.log.bak",
    "failure-marker.json", "failure-marker.json.ab12.tmp", "notes.txt",
    "log-copy-a/b.log", "LOG-COPY-x.log",
])
def test_copy_name_classification_rejects_malformed(name):
    assert not is_copy_file_name(name)


def test_marker_temp_names_are_distinguished_from_marker():
    temp = marker_temp_file_name("ab12")
    assert temp.startswith(MARKER_NAME + ".") and temp.endswith(".tmp")
    assert is_marker_temp_name(temp)
    assert not is_marker_temp_name(MARKER_NAME)
    assert not is_marker_temp_name("failure-marker.json.tmpx")
    assert not is_copy_file_name(temp)


# ---- 标记存在性与内容 ------------------------------------------------


class FakeFiles:
    """记录文件操作并按路径返回可编程结果。"""

    def __init__(self, monkeypatch, order: list | None = None) -> None:
        from camctl.logging_runtime import copies
        self.order = order if order is not None else []
        self.calls: list[tuple] = []
        self.files: dict[str, bytes] = {}
        self.errors: dict[str, Exception] = {}
        self.listing: dict[str, list[str]] = {}
        self.fail_unlink: set[str] = set()
        monkeypatch.setattr(copies, "_exists", self._exists)
        monkeypatch.setattr(copies, "_read_bytes", self._read_bytes)
        monkeypatch.setattr(copies, "_write_synced", self._write_synced)
        monkeypatch.setattr(copies, "_replace", self._replace)
        monkeypatch.setattr(copies, "_fsync_directory", self._fsync_directory)
        monkeypatch.setattr(copies, "_unlink", self._unlink)
        monkeypatch.setattr(copies, "_listdir", self._listdir)

    def _exists(self, path):
        self.calls.append(("exists", str(path)))
        if str(path) in self.errors:
            raise self.errors[str(path)]
        return str(path) in self.files

    def _read_bytes(self, path):
        self.calls.append(("read", str(path)))
        if str(path) in self.errors:
            raise self.errors[str(path)]
        return self.files[str(path)]

    def _write_synced(self, path, data):
        self.calls.append(("write", str(path)))
        self.order.append(("marker-write", Path(path).name))
        if str(path) in self.errors:
            raise self.errors[str(path)]
        self.files[str(path)] = bytes(data)

    def _replace(self, source, target):
        self.calls.append(("replace", str(source), str(target)))
        if str(target) in self.errors:
            raise self.errors[str(target)]
        self.files[str(target)] = self.files.pop(str(source), b"")

    def _fsync_directory(self, path):
        self.calls.append(("fsync", str(path)))
        if str(path) in self.errors:
            raise self.errors[str(path)]

    def _unlink(self, path):
        self.calls.append(("unlink", str(path)))
        if str(path) in self.errors or str(path) in self.fail_unlink:
            error = self.errors.get(str(path), OSError("busy"))
            raise error
        self.files.pop(str(path), None)

    def _listdir(self, path):
        self.calls.append(("listdir", str(path)))
        if str(path) in self.errors:
            raise self.errors[str(path)]
        return tuple(self.listing.get(str(path), ()))


def _logs_dir() -> Path:
    return Path("staging") / "logs"


def _marker_bytes(round_id="r1", name="log-copy-r1.log", stage="triggered"):
    import json
    return json.dumps({
        "round_id": round_id, "copy_name": name, "stage": stage,
    }).encode()


def test_check_reports_absent_without_marker(monkeypatch):
    files = FakeFiles(monkeypatch)
    store = MarkerStore(_logs_dir())
    check = store.check()
    assert check.presence is MarkerPresence.ABSENT
    assert check.state is None


def test_check_reports_state_for_valid_marker(monkeypatch):
    files = FakeFiles(monkeypatch)
    files.files[str(_logs_dir() / MARKER_NAME)] = _marker_bytes(stage="published")
    check = MarkerStore(_logs_dir()).check()
    assert check.presence is MarkerPresence.EXISTS
    assert check.state.stage is MarkerStage.PUBLISHED
    assert check.state.copy_name == "log-copy-r1.log"


def test_check_keeps_existence_when_content_invalid(monkeypatch):
    files = FakeFiles(monkeypatch)
    files.files[str(_logs_dir() / MARKER_NAME)] = b"{not json"
    check = MarkerStore(_logs_dir()).check()
    assert check.presence is MarkerPresence.EXISTS
    assert check.state is None


@pytest.mark.parametrize("payload", [
    b'{"round_id": 5}',                     # 身份不是字符串
    b'{"round_id": "r", "copy_name": "x", "stage": "nope"}',  # 未知阶段
    b'{"round_id": "r", "copy_name": 7, "stage": "triggered"}',
    b'[]',
])
def test_check_treats_wrong_schema_as_invalid(monkeypatch, payload):
    files = FakeFiles(monkeypatch)
    files.files[str(_logs_dir() / MARKER_NAME)] = payload
    check = MarkerStore(_logs_dir()).check()
    assert check.presence is MarkerPresence.EXISTS
    assert check.state is None


def test_check_error_is_not_absence(monkeypatch):
    files = FakeFiles(monkeypatch)
    files.errors[str(_logs_dir() / MARKER_NAME)] = PermissionError("denied")
    check = MarkerStore(_logs_dir()).check()
    assert check.presence is MarkerPresence.CHECK_ERROR
    assert check.error is not None


# ---- 标记创建与更新 --------------------------------------------------


def test_create_saves_via_temp_then_atomic_replace(monkeypatch):
    files = FakeFiles(monkeypatch)
    store = MarkerStore(_logs_dir())
    save = store.create("r1", "log-copy-r1.log")
    assert save.outcome is SaveOutcome.SAVED
    written = [c for c in files.calls if c[0] == "write"]
    replaced = [c for c in files.calls if c[0] == "replace"]
    assert len(written) == 1 and is_marker_temp_name(Path(written[0][1]).name)
    assert len(replaced) == 1 and replaced[0][2].endswith(MARKER_NAME)
    # 临时文件已被放置消费；正式内容可读回。
    assert store.check().state.round_id == "r1"


def test_create_failure_before_placement_is_not_created(monkeypatch):
    files = FakeFiles(monkeypatch)
    files.errors[str(_logs_dir() / "failure-marker.json.any.tmp")] = OSError("disk full")
    store = MarkerStore(_logs_dir())
    # 占位：具体临时名由实现生成，用 write 钩子统一失败。
    monkeypatch.setattr(
        "camctl.logging_runtime.copies._write_synced",
        lambda path, data: (_ for _ in ()).throw(OSError("disk full")))
    save = store.create("r1", "log-copy-r1.log")
    assert save.outcome is SaveOutcome.FAILED_NOT_CREATED
    assert store.check().presence is MarkerPresence.ABSENT


def test_create_placed_but_directory_sync_unconfirmed(monkeypatch):
    files = FakeFiles(monkeypatch)
    files.errors[str(_logs_dir())] = OSError("sync failed")
    store = MarkerStore(_logs_dir())
    save = store.create("r1", "log-copy-r1.log")
    assert save.outcome is SaveOutcome.PLACED_UNSYNCED
    assert store.check().presence is MarkerPresence.EXISTS


def test_create_unknown_result_stays_unknown(monkeypatch):
    files = FakeFiles(monkeypatch)
    files.errors[str(_logs_dir() / MARKER_NAME)] = OSError("replace unknown")
    store = MarkerStore(_logs_dir())
    save = store.create("r1", "log-copy-r1.log")
    assert save.outcome is SaveOutcome.UNKNOWN


def test_update_replaces_atomically_and_keeps_identity(monkeypatch):
    files = FakeFiles(monkeypatch)
    store = MarkerStore(_logs_dir())
    store.create("r1", "log-copy-r1.log")
    save = store.update_stage(MarkerStage.PUBLISHED)
    assert save.outcome is SaveOutcome.SAVED
    state = store.check().state
    assert state.round_id == "r1" and state.stage is MarkerStage.PUBLISHED


def test_update_without_existing_marker_fails(monkeypatch):
    store = MarkerStore(_logs_dir())
    save = store.update_stage(MarkerStage.COPIED)
    assert save.outcome is SaveOutcome.FAILED_NOT_CREATED


def test_delete_removes_marker_and_syncs_directory(monkeypatch):
    files = FakeFiles(monkeypatch)
    store = MarkerStore(_logs_dir())
    store.create("r1", "log-copy-r1.log")
    outcome = store.delete()
    assert outcome.outcome.value == "deleted"
    assert ("unlink", str(_logs_dir() / MARKER_NAME)) in files.calls
    assert ("fsync", str(_logs_dir())) in files.calls
    assert store.check().presence is MarkerPresence.ABSENT


def test_delete_reports_absent_marker(monkeypatch):
    outcome = MarkerStore(_logs_dir()).delete()
    assert outcome.outcome.value == "not_present"


def test_delete_failure_keeps_error(monkeypatch):
    files = FakeFiles(monkeypatch)
    files.files[str(_logs_dir() / MARKER_NAME)] = _marker_bytes()
    files.fail_unlink.add(str(_logs_dir() / MARKER_NAME))
    outcome = MarkerStore(_logs_dir()).delete()
    assert outcome.outcome.value == "failed"
    assert outcome.error is not None


# ---- 交付编排 --------------------------------------------------------


class FakeChannel:
    def __init__(self, order: list | None = None) -> None:
        self.order = order if order is not None else []
        self.copies: list[tuple] = []
        self.write_result = WriteResult(appended=True, rotated=False)
        self.copy_error: str | None = None

    def copy_with_write(self, record, destination):
        self.order.append(("copy", Path(destination).name))
        self.copies.append((record, Path(destination)))
        return self.write_result, self.copy_error


class FakePublisher:
    def __init__(self, order: list | None = None) -> None:
        self.order = order if order is not None else []
        self.calls: list[tuple] = []
        self.result = None  # PublishResult 或异常

    async def __call__(self, source, directories, target):
        from camctl.host_files.handoff import ReadyName
        self.order.append(("publish", Path(source).name))
        name = target.name if hasattr(target, "name") else str(target)
        self.calls.append((Path(source), directories, ReadyName(name).name))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _request(monkeypatch, *, channel=None, marker_dir=None):
    from camctl.logging_runtime.copies import MarkerStore
    return CopyRequest(
        trigger=LogRecord(level=LogLevel.ERROR, message="report failed"),
        staging_root=Path("staging"),
        ready_dir=Path("ready"),
        channel=channel or FakeChannel(),
        marker=MarkerStore(marker_dir or _logs_dir()),
    )


def _deliver(request, publisher):
    return asyncio.run(
        deliver_failure_copy(request, publish=publisher))


def _moved():
    from camctl.host_files.handoff import (
        HandoffDirectories, PublishResult, PublishStage)
    from camctl.host_files.io import DirectorySyncStage
    return PublishResult(PublishStage.MOVED, DirectorySyncStage.SYNCED, True, None)


def test_full_delivery_publishes_and_updates_progress(monkeypatch):
    order: list = []
    files = FakeFiles(monkeypatch, order)
    channel, publisher = FakeChannel(order), FakePublisher(order)
    publisher.result = _moved()
    receipt = _deliver(_request(monkeypatch, channel=channel), publisher)
    assert receipt.outcome is CopyOutcome.PUBLISHED
    assert is_copy_file_name(receipt.copy_name)
    # 标记先行：创建成功后才开始锁内写入并复制，最后发布并保存进度。
    sequence = [e[0] for e in order]
    assert sequence.index("marker-write") < sequence.index("copy")
    assert sequence.index("copy") < sequence.index("publish")
    assert MarkerStore(_logs_dir()).check().state.stage is MarkerStage.PUBLISHED
    assert channel.copies[0][1].parent == Path("staging") / "logs"
    assert publisher.calls and publisher.calls[0][0].name == receipt.copy_name


def test_existing_marker_suppresses_new_copy(monkeypatch):
    files = FakeFiles(monkeypatch)
    files.files[str(_logs_dir() / MARKER_NAME)] = _marker_bytes(stage="copied")
    channel, publisher = FakeChannel(), FakePublisher()
    receipt = _deliver(_request(monkeypatch, channel=channel), publisher)
    assert receipt.outcome is CopyOutcome.SUPPRESSED
    assert receipt.copy_name == "log-copy-r1.log"
    assert channel.copies == [] and publisher.calls == []


def test_invalid_marker_content_still_suppresses(monkeypatch):
    files = FakeFiles(monkeypatch)
    files.files[str(_logs_dir() / MARKER_NAME)] = b"not-json"
    channel, publisher = FakeChannel(), FakePublisher()
    receipt = _deliver(_request(monkeypatch, channel=channel), publisher)
    assert receipt.outcome is CopyOutcome.SUPPRESSED
    assert channel.copies == []


def test_marker_check_error_stops_delivery(monkeypatch):
    files = FakeFiles(monkeypatch)
    files.errors[str(_logs_dir() / MARKER_NAME)] = PermissionError("denied")
    channel, publisher = FakeChannel(), FakePublisher()
    receipt = _deliver(_request(monkeypatch, channel=channel), publisher)
    assert receipt.outcome is CopyOutcome.MARKER_UNAVAILABLE
    assert channel.copies == []


def test_marker_create_failure_stops_before_copy(monkeypatch):
    monkeypatch.setattr(
        "camctl.logging_runtime.copies._write_synced",
        lambda path, data: (_ for _ in ()).throw(OSError("disk full")))
    channel, publisher = FakeChannel(), FakePublisher()
    receipt = _deliver(_request(monkeypatch, channel=channel), publisher)
    assert receipt.outcome is CopyOutcome.MARKER_CREATE_FAILED
    assert channel.copies == [] and publisher.calls == []


def test_copy_write_failure_cleans_staging_copy(monkeypatch):
    files = FakeFiles(monkeypatch)
    channel, publisher = FakeChannel(), FakePublisher()
    channel.write_result = WriteResult(appended=False, rotated=False,
                                       append_error="append failed")
    receipt = _deliver(_request(monkeypatch, channel=channel), publisher)
    assert receipt.outcome is CopyOutcome.COPY_FAILED
    # 标记保留（故障仍持续），本次副本清理。
    assert MarkerStore(_logs_dir()).check().presence is MarkerPresence.EXISTS
    assert any(c[0] == "unlink" and is_copy_file_name(Path(c[1]).name)
               for c in files.calls)
    assert publisher.calls == []


def test_copy_file_error_cleans_staging_copy(monkeypatch):
    files = FakeFiles(monkeypatch)
    channel, publisher = FakeChannel(), FakePublisher()
    channel.copy_error = "copy failed"
    receipt = _deliver(_request(monkeypatch, channel=channel), publisher)
    assert receipt.outcome is CopyOutcome.COPY_FAILED
    assert any(c[0] == "unlink" for c in files.calls)


def test_publish_not_moved_cleans_staging_copy(monkeypatch):
    from camctl.host_files.handoff import PublishResult, PublishStage
    from camctl.host_files.io import DirectorySyncStage
    files = FakeFiles(monkeypatch)
    channel, publisher = FakeChannel(), FakePublisher()
    publisher.result = PublishResult(
        PublishStage.NOT_MOVED, DirectorySyncStage.NOT_ATTEMPTED, False,
        "target_exists")
    receipt = _deliver(_request(monkeypatch, channel=channel), publisher)
    assert receipt.outcome is CopyOutcome.COPY_FAILED
    assert any(c[0] == "unlink" and is_copy_file_name(Path(c[1]).name)
               for c in files.calls)
    assert MarkerStore(_logs_dir()).check().presence is MarkerPresence.EXISTS


def test_publish_unknown_keeps_facts_without_cleanup(monkeypatch):
    from camctl.host_files.handoff import PublishResult, PublishStage
    from camctl.host_files.io import DirectorySyncStage
    files = FakeFiles(monkeypatch)
    channel, publisher = FakeChannel(), FakePublisher()
    publisher.result = PublishResult(
        PublishStage.UNKNOWN, DirectorySyncStage.NOT_ATTEMPTED, False,
        "move_unknown")
    receipt = _deliver(_request(monkeypatch, channel=channel), publisher)
    assert receipt.outcome is CopyOutcome.COPY_UNKNOWN
    assert not any(c[0] == "unlink" for c in files.calls)


def test_publish_moved_with_sync_failure_keeps_ready_file(monkeypatch):
    from camctl.host_files.handoff import PublishResult, PublishStage
    from camctl.host_files.io import DirectorySyncStage
    files = FakeFiles(monkeypatch)
    channel, publisher = FakeChannel(), FakePublisher()
    publisher.result = PublishResult(
        PublishStage.MOVED, DirectorySyncStage.FAILED, True, "sync failed")
    receipt = _deliver(_request(monkeypatch, channel=channel), publisher)
    # 已移入 ready 的文件保持不撤回；未同步成功不宣称发布完成。
    assert receipt.outcome is CopyOutcome.COPY_UNKNOWN
    assert not any(c[0] == "unlink" for c in files.calls)


def test_progress_update_failure_does_not_block_delivery(monkeypatch):
    files = FakeFiles(monkeypatch)
    # 首次创建成功；后续更新（published 进度）失败。
    original_write = files._write_synced
    state = {"count": 0}

    def flaky_write(path, data):
        state["count"] += 1
        if state["count"] > 1:
            raise OSError("progress lost")
        return original_write(path, data)

    monkeypatch.setattr(
        "camctl.logging_runtime.copies._write_synced", flaky_write)
    channel, publisher = FakeChannel(), FakePublisher()
    publisher.result = _moved()
    receipt = _deliver(_request(monkeypatch, channel=channel), publisher)
    assert receipt.outcome is CopyOutcome.PUBLISHED
    # 标记保留原触发进度，身份不变。
    assert MarkerStore(_logs_dir()).check().state.stage is MarkerStage.TRIGGERED


# ---- 同运行暂停服务 --------------------------------------------------


def test_service_suspends_after_marker_unavailable(monkeypatch):
    files = FakeFiles(monkeypatch)
    files.errors[str(_logs_dir() / MARKER_NAME)] = PermissionError("denied")
    service = FailureLogService(MarkerStore(_logs_dir()))
    first = asyncio.run(service.on_report_failure(_request(monkeypatch)))
    assert first.outcome is CopyOutcome.MARKER_UNAVAILABLE
    # 同次运行内重复失败不再尝试检查。
    files.errors.clear()
    second = asyncio.run(service.on_report_failure(_request(monkeypatch)))
    assert second.outcome is CopyOutcome.SUPPRESSED


def test_service_recovery_deletes_marker_and_lifts_suspension(monkeypatch):
    files = FakeFiles(monkeypatch)
    store = MarkerStore(_logs_dir())
    store.create("r1", "log-copy-r1.log")

    async def fake_deliver(request):
        return await deliver_failure_copy(request, publish=publisher)

    channel, publisher = FakeChannel(), FakePublisher()
    publisher.result = _moved()
    service = FailureLogService(store, deliver=fake_deliver)
    asyncio.run(service.on_report_recovered())
    assert store.check().presence is MarkerPresence.ABSENT
    # 解除暂停后新故障可再次尝试（此处创建成功走完整流程）。
    request = CopyRequest(
        trigger=LogRecord(level=LogLevel.ERROR, message="again"),
        staging_root=Path("staging"), ready_dir=Path("ready"),
        channel=channel, marker=store)
    receipt = asyncio.run(service.on_report_failure(request))
    assert receipt.outcome is CopyOutcome.PUBLISHED


def test_service_suppressed_failure_does_not_suspend(monkeypatch):
    files = FakeFiles(monkeypatch)
    files.files[str(_logs_dir() / MARKER_NAME)] = _marker_bytes()
    service = FailureLogService(MarkerStore(_logs_dir()))
    asyncio.run(service.on_report_failure(_request(monkeypatch)))
    # 抑制不是错误：恢复后正常删除标记。
    asyncio.run(service.on_report_recovered())
    assert MarkerStore(_logs_dir()).check().presence is MarkerPresence.ABSENT


# ---- 启动清理 --------------------------------------------------------

def _sweep(monkeypatch, listing):
    files = FakeFiles(monkeypatch)
    files.listing[str(_logs_dir())] = listing
    result = startup_sweep(_logs_dir(), active_names=())
    return files, result


def test_startup_sweep_removes_old_copies_and_temp(monkeypatch):
    old_copy = "log-copy-old.log"
    temp = marker_temp_file_name("old")
    files = FakeFiles(monkeypatch)
    files.listing[str(_logs_dir())] = [
        old_copy, temp, MARKER_NAME, "log-copy-mine.log", "other.txt"]
    result = startup_sweep(_logs_dir(), active_names=("log-copy-mine.log",))
    removed = {c[1] for c in files.calls if c[0] == "unlink"}
    assert removed == {str(_logs_dir() / old_copy), str(_logs_dir() / temp)}


def test_startup_sweep_excludes_active_copy(monkeypatch):
    files = FakeFiles(monkeypatch)
    files.listing[str(_logs_dir())] = ["log-copy-mine.log"]
    result = startup_sweep(_logs_dir(), active_names=("log-copy-mine.log",))
    assert not any(c[0] == "unlink" for c in files.calls)


def test_startup_sweep_keeps_directory_check_error(monkeypatch):
    files = FakeFiles(monkeypatch)
    files.errors[str(_logs_dir())] = OSError("unreadable")
    result = startup_sweep(_logs_dir())
    assert result.error is not None
    assert not any(c[0] == "unlink" for c in files.calls)


def test_startup_sweep_continues_after_single_delete_failure(monkeypatch):
    files = FakeFiles(monkeypatch)
    files.listing[str(_logs_dir())] = ["log-copy-a.log", "log-copy-b.log"]
    files.fail_unlink.add(str(_logs_dir() / "log-copy-a.log"))
    result = startup_sweep(_logs_dir())
    removed = {c[1] for c in files.calls if c[0] == "unlink"}
    assert str(_logs_dir() / "log-copy-b.log") in removed
    assert result.failures and "log-copy-a.log" in result.failures[0][0]
