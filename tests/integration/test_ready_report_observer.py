"""报告观察器读取真实文件时允许发布者替换旧 READY 报告。"""

import json
import errno
import io
import os

import pytest

import test_camctl_c_module_roundtrip as observer


REPORT = {"report_id": "6", "plans": [{"actions": [
    {"name": "next-record", "status": "succeeded"},
    {"name": "obtain", "status": "running"}]}]}
EXPECTED = {"next-record": "succeeded", "obtain": "running"}
WINDOWS = pytest.mark.skipif(os.name != "nt", reason="Windows 文件句柄到 Python reader 的归属验证")


@pytest.fixture
def ready_report(tmp_path):
    ready = tmp_path / "ready"
    ready.mkdir()
    path = ready / "status-report-6.json"
    path.write_text(json.dumps(REPORT), encoding="utf-8")
    return path


@pytest.mark.parametrize("operation", ["unlink", "rename"])
def test_open_reader_allows_publisher_to_remove_original_path(ready_report, operation):
    with observer._open_ready_report(ready_report) as reader:
        if operation == "unlink":
            ready_report.unlink()
        else:
            renamed = ready_report.with_name("claimed-report.json")
            ready_report.rename(renamed)
            assert json.loads(renamed.read_text(encoding="utf-8")) == REPORT
        replacement = ready_report.with_name("status-report-7.json")
        replacement.write_text('{"replacement":true}', encoding="utf-8")
        assert json.loads(reader.read()) == REPORT
    assert reader.closed
    assert not ready_report.exists()
    assert json.loads(replacement.read_text(encoding="utf-8")) == {"replacement": True}
    if operation == "rename":
        assert json.loads(renamed.read_text(encoding="utf-8")) == REPORT


def test_closing_reader_keeps_unwithdrawn_report(ready_report):
    with observer._open_ready_report(ready_report) as reader:
        assert json.load(reader) == REPORT
    assert reader.closed
    assert json.loads(ready_report.read_text(encoding="utf-8")) == REPORT


def test_open_missing_report_preserves_missing_error(tmp_path):
    path = tmp_path / "missing.json"
    with pytest.raises(FileNotFoundError):
        observer._open_ready_report(path)
    assert not path.exists()


def test_reader_exception_closes_file_without_removing_report(ready_report):
    with pytest.raises(RuntimeError, match="consumer error"):
        with observer._open_ready_report(ready_report) as reader:
            fd = reader.fileno()
            raise RuntimeError("consumer error")
    assert reader.closed
    with pytest.raises(OSError) as closed:
        os.fstat(fd)
    assert closed.value.errno == errno.EBADF
    assert json.loads(ready_report.read_text(encoding="utf-8")) == REPORT
    _assert_no_reader_holds_file(ready_report)


def _assert_no_reader_holds_file(path):
    if os.name == "nt":
        import _winapi
        # 独占打开会拒绝任何残留读句柄；随后释放本次检查的句柄。
        handle = _winapi.CreateFile(str(path), _winapi.GENERIC_READ, 0, 0,
                                    _winapi.OPEN_EXISTING, 128, 0)
        _winapi.CloseHandle(handle)


@WINDOWS
def test_failed_handle_handoff_releases_windows_handle(ready_report, monkeypatch):
    import msvcrt
    def failed_handoff(handle, flags):
        raise OSError("handle handoff failed")
    monkeypatch.setattr(msvcrt, "open_osfhandle", failed_handoff)
    with pytest.raises(OSError, match="handle handoff failed"):
        observer._open_ready_report(ready_report)
    _assert_no_reader_holds_file(ready_report)


@WINDOWS
def test_failed_file_object_handoff_releases_descriptor(ready_report, monkeypatch):
    actual_open = io.open
    acquired_fds = []
    def failed_text_wrapper(file, mode="r", buffering=-1, encoding=None, errors=None,
                            newline=None, closefd=True, opener=None):
        if isinstance(file, int):
            acquired_fds.append(file)
        elif os.fspath(file) == os.fspath(ready_report):
            actual_opener = opener
            def recording_opener(path, flags):
                fd = actual_opener(path, flags)
                acquired_fds.append(fd)
                return fd
            opener = recording_opener
        else:
            return actual_open(file, mode, buffering, encoding, errors, newline, closefd, opener)
        # 真实标准库取得 FD 后执行文本包装；失败与释放均由它发生。
        return actual_open(file, mode, buffering, "camctl-test-nonexistent-codec",
                           errors, newline, closefd, opener)
    with monkeypatch.context() as patcher:
        patcher.setattr(io, "open", failed_text_wrapper)
        with pytest.raises(LookupError):
            observer._open_ready_report(ready_report)
    assert len(acquired_fds) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(acquired_fds[0])
    assert closed.value.errno == errno.EBADF
    _assert_no_reader_holds_file(ready_report)
    assert json.loads(ready_report.read_text(encoding="utf-8")) == REPORT


@pytest.mark.parametrize("replacement", ["unlink", "rename"])
def test_observer_returns_opened_report_when_publisher_replaces_ready(ready_report, monkeypatch, replacement):
    actual_open = observer._open_ready_report
    def open_during_publication(path):
        reader = actual_open(path)
        try:
            if replacement == "unlink":
                path.unlink()
            else:
                path.rename(path.with_name("claimed-report.json"))
        except BaseException:
            reader.close()
            raise
        return reader
    monkeypatch.setattr(observer, "_open_ready_report", open_during_publication)
    assert observer._report_with_statuses(ready_report.parent, EXPECTED) == REPORT
    assert not ready_report.exists()


def test_observer_waits_for_matching_ready_status_without_reading_staging(ready_report, tmp_path):
    staged = tmp_path / "staging"
    staged.mkdir()
    (staged / "status-report-7.json").write_text(json.dumps(REPORT), encoding="utf-8")
    pending = {"plans": [{"actions": [{"name": "next-record", "status": "pending"}]}]}
    ready_report.write_text(json.dumps(pending), encoding="utf-8")
    assert observer._report_with_statuses(ready_report.parent, EXPECTED) is None
    ready_report.write_text(json.dumps(REPORT), encoding="utf-8")
    assert observer._report_with_statuses(ready_report.parent, EXPECTED) == REPORT


@pytest.mark.parametrize("content", [b"invalid JSON", b"\xef\xbb\xbf" + json.dumps(REPORT).encode("utf-8")],
                         ids=["invalid-json", "utf8-bom"])
def test_observer_preserves_invalid_json_error_and_closes_reader(ready_report, content):
    ready_report.write_bytes(content)
    with pytest.raises(json.JSONDecodeError):
        observer._report_with_statuses(ready_report.parent, EXPECTED)
    _assert_no_reader_holds_file(ready_report)
    assert ready_report.read_bytes() == content


@pytest.mark.parametrize("content", [b"\xff", json.dumps(REPORT).encode("utf-16"), json.dumps(REPORT).encode("utf-32")],
                         ids=["invalid-utf8", "utf16", "utf32"])
def test_observer_preserves_decode_error_and_closes_reader(ready_report, content):
    ready_report.write_bytes(content)
    with pytest.raises(UnicodeDecodeError):
        observer._report_with_statuses(ready_report.parent, EXPECTED)
    _assert_no_reader_holds_file(ready_report)
    assert ready_report.read_bytes() == content


def test_reader_keeps_default_text_newline_semantics(ready_report):
    ready_report.write_bytes("相机\r\n取回\r完成\n".encode("utf-8"))
    with observer._open_ready_report(ready_report) as reader:
        assert reader.read() == "相机\n取回\n完成\n"


@pytest.mark.parametrize("opening_error", [FileNotFoundError, PermissionError])
def test_observer_skips_existing_opening_races(ready_report, monkeypatch, opening_error):
    def inaccessible(path):
        raise opening_error("opening raced with publication")
    monkeypatch.setattr(observer, "_open_ready_report", inaccessible)
    assert observer._report_with_statuses(ready_report.parent, EXPECTED) is None


def test_observer_preserves_other_read_errors_and_closes_reader(ready_report, monkeypatch):
    actual_open = observer._open_ready_report
    def failed_read(path):
        reader = actual_open(path)
        def read_error(*args, **kwargs):
            raise OSError("read failed")
        reader.read = read_error
        return reader
    monkeypatch.setattr(observer, "_open_ready_report", failed_read)
    with pytest.raises(OSError, match="read failed"):
        observer._report_with_statuses(ready_report.parent, EXPECTED)
    _assert_no_reader_holds_file(ready_report)
