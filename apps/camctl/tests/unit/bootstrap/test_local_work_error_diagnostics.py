"""本地组合收场保留原控制对象，并完整交付其他拥有者的诊断。"""

import asyncio
import json

import pytest

from camctl.bootstrap.background_flow import CombinedLocalWork
from camctl.operations.error_diagnostics import DiagnosticError, exception_diagnostic


def _secondary_failures():
    cause = OSError(13, "原始读取失败", b"/camera/\xff")
    try:
        raise cause
    except OSError as original:
        try:
            raise DiagnosticError(
                "源关闭失败",
                diagnostic_details={"sentinel": "完整原始详情", "stderr_hex": "00ff0a"},
            ) from original
        except DiagnosticError as failure:
            diagnostic = failure
    group = ExceptionGroup("全部关闭错误", [
        DiagnosticError(f"拥有者 {index}", diagnostic_details={"index": index})
        for index in range(33)
    ])
    return diagnostic, group


def _saved_diagnostics(error):
    """从最终可保存的异常诊断中读取附注内的 JSON，不依赖提示文案。"""
    saved = json.loads(json.dumps(exception_diagnostic(error), ensure_ascii=False))
    diagnostics = []
    for note in saved.get("notes", ()):
        start = note.find("{")
        if start < 0:
            continue
        try:
            value = json.loads(note[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "type" in value:
            diagnostics.append(value)
    return diagnostics


def _assert_secondary_diagnostics(diagnostics):
    first, second = diagnostics
    assert first["type"] == "camctl.operations.error_diagnostics.DiagnosticError"
    assert first["diagnostic_details"] == {
        "sentinel": "完整原始详情", "stderr_hex": "00ff0a"}
    assert first["cause"]["type"] == "builtins.PermissionError"
    assert first["cause"]["errno"] == 13
    assert first["cause"]["filename"] == {"bytes_hex": "2f63616d6572612fff"}
    assert first["cause"]["traceback"][-1]["function"] == "_secondary_failures"
    assert first["traceback"][-1]["function"] == "_secondary_failures"
    assert second["type"] == "builtins.ExceptionGroup"
    assert [child["diagnostic_details"]["index"]
            for child in second["exceptions"]] == list(range(33))


@pytest.mark.parametrize("control_index", [0, 1, 2])
@pytest.mark.parametrize("grouped_control", [False, True])
def test_raise_failures_keeps_control_and_all_secondary_diagnostics(
        control_index, grouped_control):
    cancel = asyncio.CancelledError("原控制异常")
    original_leaves = (cancel, ValueError("原控制组次级错误"))
    control = (BaseExceptionGroup("原控制组", original_leaves)
               if grouped_control else cancel)
    original_args = control.args
    control.add_note("原控制附注")
    failures = list(_secondary_failures())
    failures.insert(control_index, control)

    with pytest.raises(type(control)) as caught:
        CombinedLocalWork._raise_failures(tuple(failures))

    assert caught.value is control
    assert control.args == original_args
    if grouped_control:
        assert all(actual is original for actual, original in
                   zip(control.exceptions, original_leaves, strict=True))
    assert control.__notes__[0] == "原控制附注"
    _assert_secondary_diagnostics(_saved_diagnostics(control))


@pytest.mark.parametrize("control", [False, True])
def test_raise_single_failure_keeps_original_without_annotation(control):
    failure = asyncio.CancelledError("唯一控制错误") if control else _secondary_failures()[0]
    original_notes = tuple(getattr(failure, "__notes__", ()))

    with pytest.raises(type(failure)) as caught:
        CombinedLocalWork._raise_failures((failure,))

    assert caught.value is failure
    assert tuple(getattr(failure, "__notes__", ())) == original_notes
    if not control:
        assert exception_diagnostic(failure)["diagnostic_details"]["sentinel"] == "完整原始详情"


def test_raise_ordinary_failures_keeps_original_group_members_in_order():
    failures = _secondary_failures()

    with pytest.raises(ExceptionGroup) as caught:
        CombinedLocalWork._raise_failures(failures)

    assert all(actual is original for actual, original in
               zip(caught.value.exceptions, failures, strict=True))
    _assert_secondary_diagnostics(exception_diagnostic(caught.value)["exceptions"])


def test_attach_failures_snapshots_all_graphs_before_target_annotations():
    control = asyncio.CancelledError("原控制异常")
    control.add_note("添加前唯一附注")
    first, second = _secondary_failures()
    first.__cause__ = control
    second.__cause__ = control
    first.add_note("次级错误原附注")

    CombinedLocalWork._attach_failures(control, (first, second))

    diagnostics = _saved_diagnostics(control)
    assert len(diagnostics) == 2
    for diagnostic in diagnostics:
        assert diagnostic["cause"]["type"] == "asyncio.exceptions.CancelledError"
        assert diagnostic["cause"]["notes"] == ["添加前唯一附注"]
    assert diagnostics[0]["notes"] == ["次级错误原附注"]
    assert first.__notes__ == ["次级错误原附注"]
    assert "次级错误原附注" in "\n".join(control.__notes__)


def test_attach_failures_skips_same_object_without_adding_notes():
    class UnannotatableCancellation(asyncio.CancelledError):
        def add_note(self, _note):
            raise AssertionError("同一错误不得自附注")

    control = UnannotatableCancellation("同一原始错误")
    CombinedLocalWork._attach_failures(control, (control, control))
    with pytest.raises(UnannotatableCancellation) as caught:
        CombinedLocalWork._raise_failures((control, control))
    assert caught.value is control
    assert not hasattr(control, "__notes__")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_count", [1, 2])
async def test_observer_cancel_keeps_control_and_actual_owner_diagnostics(failure_count):
    started, release = asyncio.Event(), asyncio.Event()
    failures = _secondary_failures()[:failure_count]

    class Owner:
        def __init__(self, failure, *, wait):
            self.failure, self.wait = failure, wait
            self.active = False
            self.settle_calls = 0
            self.was_cancelled = False

        def required_settlements(self):
            return int(self.active)

        def stop_new_work(self):
            raise AssertionError("观察者取消不得停止实际拥有者")

        async def settle(self):
            self.settle_calls += 1
            self.active = True
            try:
                if self.wait:
                    started.set()
                    await release.wait()
            except asyncio.CancelledError:
                self.was_cancelled = True
                raise
            finally:
                self.active = False
            raise self.failure

    owners = [Owner(failure, wait=index == 0) for index, failure in enumerate(failures)]
    combined = CombinedLocalWork(owners)
    observed = []

    async def observe():
        try:
            await combined.settle()
        except asyncio.CancelledError as error:
            observed.append(error)
            raise

    observer = asyncio.create_task(observe())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        observer.cancel("原观察者取消")
        resumed = asyncio.Event()
        asyncio.get_running_loop().call_soon(resumed.set)
        await asyncio.wait_for(resumed.wait(), timeout=1)
        assert not observer.done()
        assert combined.required_settlements() == 1
        release.set()

        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(observer, timeout=1)

        assert caught.value is observed[0]
        assert caught.value.args == ("原观察者取消",)
        diagnostics = _saved_diagnostics(caught.value)
        assert len(diagnostics) == failure_count
        assert diagnostics[0]["diagnostic_details"]["sentinel"] == "完整原始详情"
        assert diagnostics[0]["cause"]["errno"] == 13
        if failure_count == 2:
            _assert_secondary_diagnostics(diagnostics)
        assert all(owner.settle_calls == 1 and not owner.was_cancelled for owner in owners)
        assert combined.required_settlements() == 0
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(observer, return_exceptions=True), timeout=1)
