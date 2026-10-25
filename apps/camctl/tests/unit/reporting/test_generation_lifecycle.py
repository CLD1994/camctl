"""报告生成拥有读取作用域，并在写入失败前关闭流式读取。"""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.contracts.history_values import HistoryBoundary
from camctl.reporting import bounded_generation, generation


class _Repository(generation.HistoryRepository):
    """隔离真实仓储和文件，保留生产仓储的同步作用域契约。"""

    def __init__(self, events):
        self.events = events
        self.active = False

    @contextmanager
    def report_read_scope(self):
        assert not self.active
        self.active = True
        self.events.append("read-open")
        try:
            yield
        finally:
            self.events.append("read-close")
            self.active = False

    def frozen_registration(self, report_id):
        assert self.active, "冻结依据也必须使用报告拥有的读取作用域"
        self.events.append("frozen")
        return SimpleNamespace(from_wm=0, to_wm=2, boundary=HistoryBoundary(1, 3))


@pytest.fixture
def isolated_generation(monkeypatch):
    events = []
    repo = _Repository(events)
    state = {"write_error": None, "sync_error": None, "streams": []}

    class File:
        def __init__(self):
            self.content = bytearray()

        def write(self, value):
            assert repo.active
            events.append("write")
            if state["write_error"] is not None:
                raise state["write_error"]
            self.content.extend(value)

        def flush(self):
            assert repo.active
            events.append("flush")

        def fileno(self):
            return 42

        def close(self):
            events.append("file-close")

    output = File()

    def open_output(path, mode):
        assert repo.active
        assert mode == "wb"
        events.append("file-open")
        return output

    def fsync(descriptor):
        assert repo.active
        assert descriptor == 42
        events.append("sync")
        if state["sync_error"] is not None:
            raise state["sync_error"]

    def report_tokens(repository, spec, boundary, stream):
        assert repository is repo

        def values():
            assert repo.active
            events.append("stream-open")
            try:
                yield b"a"
                yield b"b"
            finally:
                assert repo.active
                events.append("stream-close")

        result = values()
        # 模拟仍拥有迭代器的协作者，不能依赖引用计数偶然关闭读取。
        state["streams"].append(result)
        return result

    monkeypatch.setattr(generation, "open", open_output, raising=False)
    monkeypatch.setattr(generation.os, "fsync", fsync)
    monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: None)
    monkeypatch.setattr(Path, "unlink", lambda *args, **kwargs: events.append("unlink"))
    monkeypatch.setattr(bounded_generation, "report_tokens", report_tokens)
    spec = generation.GenerationSpec(
        db_path=Path("state.db"), report_id=1, from_wm=0, to_wm=2,
        frozen_event_id=3, staging_path=Path("staging/report.json"),
    )
    yield repo, spec, events, state, output
    for stream in state["streams"]:
        # 测试失败时也清理替身，避免悬挂生成器在后续用例中发声。
        if stream.gi_frame is not None:
            repo.active = True
            stream.close()
    repo.active = False


def test_generation_owns_read_scope_from_registration_until_file_sync(isolated_generation):
    repo, spec, events, state, output = isolated_generation
    result = generation.generate_report_file(spec, repository=repo, buffer_size=1)
    assert bytes(output.content) == b"ab"
    assert result.size_bytes == 2
    assert events[0:2] == ["read-open", "frozen"]
    assert events.index("stream-close") < events.index("sync")
    assert events[-2:] == ["file-close", "read-close"]
    assert not repo.active


@pytest.mark.parametrize("failure", [OSError("write"), KeyboardInterrupt(), GeneratorExit()])
def test_write_failure_closes_stream_before_report_read_scope(isolated_generation, failure):
    repo, spec, events, state, output = isolated_generation
    state["write_error"] = failure
    with pytest.raises(type(failure)) as caught:
        generation.generate_report_file(spec, repository=repo, buffer_size=1)
    assert caught.value is failure
    assert events.index("stream-close") < events.index("read-close")
    assert events[-1] == "read-close"
    assert "unlink" in events
    assert "sync" not in events
    assert not repo.active


def test_sync_failure_removes_partial_file_and_releases_read_scope(isolated_generation):
    repo, spec, events, state, output = isolated_generation
    failure = OSError("sync")
    state["sync_error"] = failure
    with pytest.raises(OSError) as caught:
        generation.generate_report_file(spec, repository=repo, buffer_size=1)
    assert caught.value is failure
    assert events.index("stream-close") < events.index("sync")
    assert events[-3:] == ["file-close", "unlink", "read-close"]
    assert not repo.active
