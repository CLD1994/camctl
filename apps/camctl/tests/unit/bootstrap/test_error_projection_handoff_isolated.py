"""最终日志格式函数使用实际纯投影；通道与全部外部资源仍为替身。"""

import ast
from collections.abc import Mapping
import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


TESTS = Path(__file__).parents[1]
SOURCE = Path(__file__).parents[3] / "src/camctl/bootstrap/lifecycle.py"


def _load_projection_fixture():
    name = "isolated_projection_fixture_for_terminal"
    spec = importlib.util.spec_from_file_location(
        name, TESTS / "logging_runtime/test_projection_isolated.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def formatting():
    source = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {"_failed_outcome_message", "_direct_exception_message"}
    selected = [node for node in source.body
                if isinstance(node, ast.FunctionDef) and node.name in names]
    tree = ast.Module(body=[ast.ImportFrom(module="__future__",
        names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    ast.fix_missing_locations(tree)
    fixture = _load_projection_fixture()
    with fixture.isolated() as diagnostics:
        # 元数据与最终字节通道是协作者；被测函数实际使用公共诊断投影。
        sys.modules["camctl.logging_runtime.formatting"].encode_record = (
            lambda level, value: json.dumps(value, ensure_ascii=False))
        namespace = {"Mapping": Mapping}
        exec(compile(tree, str(SOURCE), "exec"), namespace)
        yield diagnostics, namespace


def _outcome(details, sources):
    return SimpleNamespace(reason="state_db_error", details=details,
                           _diagnostic_sources=tuple(sources))


def _render(namespace, outcome):
    deps = SimpleNamespace(mode=SimpleNamespace(value="run"))
    return json.loads(namespace["_failed_outcome_message"](deps, outcome))


def _pointer(document, path):
    assert path.startswith("#/")
    value = document
    for part in path[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def test_prepared_error_uses_actual_reference_and_preserves_machine_details(formatting):
    diagnostics, namespace = formatting
    error = ValueError("original failure")
    snapshot = diagnostics.test_error_diagnostics.exception_diagnostic(error)
    details = {"exception": snapshot, "transaction_status": "unknown"}
    before = copy.deepcopy(details)
    with diagnostics.error_diagnostics_context(None, lambda message: None):
        state = diagnostics.record_error(error, stage="capture")
        rendered = _render(namespace, _outcome(details, [(snapshot, error)]))
        record = json.loads(diagnostics.diagnostic_record_for(state).message)
    assert rendered["details"]["exception"] == {"diagnostic_ref": record["diagnostic_id"]}
    assert rendered["details"]["transaction_status"] == "unknown"
    assert details == before


def test_new_wrapper_keeps_own_facts_and_references_prepared_cause(formatting):
    diagnostics, namespace = formatting
    cause = ValueError("device failed")
    wrapper = RuntimeError("capture could not finish")
    wrapper.__cause__ = wrapper.__context__ = cause
    snapshot = diagnostics.test_error_diagnostics.exception_diagnostic(wrapper)
    with diagnostics.error_diagnostics_context(None, lambda message: None):
        state = diagnostics.record_error(cause, stage="capture")
        rendered = _render(namespace, _outcome({"exception": snapshot}, [(snapshot, wrapper)]))
    actual = rendered["details"]["exception"]
    assert actual["message"] == "capture could not finish"
    assert actual["cause"] == {"diagnostic_ref": state.diagnostic_id}
    assert actual["context"] in (actual["cause"], {"$ref": "#/details/exception/cause"})


def test_unprepared_shared_cause_is_complete_and_its_pointer_resolves(formatting):
    diagnostics, namespace = formatting
    cause = ValueError("same actual cause")
    error = RuntimeError("wrapper")
    error.__cause__ = error.__context__ = cause
    snapshot = diagnostics.test_error_diagnostics.exception_diagnostic(error)
    rendered = _render(namespace, _outcome({"a~/b": snapshot}, [(snapshot, error)]))
    actual = rendered["details"]["a~/b"]
    assert actual["cause"]["message"] == "same actual cause"
    assert _pointer(rendered, actual["context"]["$ref"]) is actual["cause"]
    assert "a~0~1b" in actual["context"]["$ref"]


@pytest.mark.parametrize("prepared", [False, True])
def test_worker_state_reference_requires_a_frozen_record(formatting, prepared):
    diagnostics, namespace = formatting
    failure = {"error_code": "worker_failed", "diagnostic": {"message": "actual worker failure"}}
    before = copy.deepcopy(failure)
    state = diagnostics.DiagnosticState()
    if prepared:
        with diagnostics.error_diagnostics_context(None, lambda message: None):
            state = diagnostics.record_error(failure, stage="report_shutdown")
    rendered = _render(namespace, _outcome({"worker_failure": failure}, [(failure, state)]))
    assert rendered["details"]["worker_failure"] == (
        {"diagnostic_ref": state.diagnostic_id} if prepared else failure)
    assert failure == before


def test_equal_independent_errors_remain_distinct_actual_facts(formatting):
    diagnostics, namespace = formatting
    first, second = ValueError("same text"), ValueError("same text")
    snapshots = [diagnostics.test_error_diagnostics.exception_diagnostic(error)
                 for error in (first, second)]
    rendered = _render(namespace, _outcome({"errors": snapshots}, zip(snapshots, (first, second))))
    assert [item["message"] for item in rendered["details"]["errors"]] == ["same text", "same text"]
    assert all("diagnostic_ref" not in item and "$ref" not in item
               for item in rendered["details"]["errors"])
