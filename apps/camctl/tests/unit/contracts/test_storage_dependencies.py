"""用内存源码验证数据库基础设施依赖检查。"""

import pytest

from support.storage_dependencies import Source, _violations


@pytest.mark.parametrize("middle", [
    "import camctl.capture.rules as rules",
    "from camctl.capture.rules import rule",
    "def later():\n    from camctl.capture import rules",
])
def test_rejects_direct_and_function_local_business_imports(middle):
    sources = {"camctl.persistence.atomic": Source(middle), "camctl.capture.rules": Source("")}
    assert any("camctl.persistence.atomic -> camctl.capture.rules" in item for item in _violations(sources, ("camctl.persistence.atomic",)))


@pytest.mark.parametrize(("via_package", "expected"), [
    (True, "camctl.neutral -> camctl.neutral.helper -> camctl.capture.rules"),
    (False, "camctl.neutral.compat -> camctl.capture.rules"),
])
def test_rejects_transitive_package_initialization_and_compatibility_forwarding(via_package, expected):
    sources = {
        "camctl.persistence.atomic": Source("import camctl.neutral.compat"),
        "camctl.neutral": Source("from . import helper" if via_package else "", package=True),
        "camctl.neutral.helper": Source("from camctl.capture import rules"),
        "camctl.neutral.compat": Source("" if via_package else "from camctl.capture.rules import rule"),
        "camctl.capture.rules": Source(""),
    }
    violations = _violations(sources, ("camctl.persistence.atomic",))
    assert any(expected in item for item in violations)


@pytest.mark.parametrize(("statement", "target"), [
    ("import importlib as loader\nloader.import_module('camctl.capture.rules')", "camctl.capture.rules"),
    ("from importlib import import_module as load\nload(variable)", "<无法静态确定>"),
    ("import builtins as b\nload = b.__import__\nload('camctl.capture.rules')", "camctl.capture.rules"),
    ("__import__(variable)", "<无法静态确定>"),
])
def test_rejects_dynamic_imports_even_when_target_cannot_be_resolved(statement, target):
    violations = _violations({"camctl.persistence.atomic": Source(statement)}, ("camctl.persistence.atomic",))
    assert len(violations) == 1 and f"动态导入 {target}" in violations[0]


def test_allows_opaque_callback_reverse_dependency_and_fixed_resources():
    sources = {
        "camctl.persistence.atomic": Source("from importlib import resources\nimport json\nfrom camctl.neutral import ordinary_symbol\ndef run(callback):\n    fixed = resources.files('camctl').joinpath('schema.json').read_text()\n    json.loads(fixed)\n    return callback()"),
        "camctl.neutral": Source(""),
        "camctl.capture.rules": Source("from camctl.persistence import atomic"),
    }
    assert _violations(sources, ("camctl.persistence.atomic",)) == []


@pytest.mark.parametrize("imports_assembly", [False, True])
def test_generic_logging_service_is_traversed_for_business_assembly(imports_assembly):
    sources = {
        "camctl.persistence.atomic": Source("from camctl.logging_runtime import service"),
        "camctl.logging_runtime.service": Source(
            "from camctl.bootstrap import lifecycle" if imports_assembly else "import queue"
        ),
        "camctl.bootstrap.lifecycle": Source(""),
    }
    violations = _violations(sources, ("camctl.persistence.atomic",))
    if imports_assembly:
        assert violations == [
            "camctl.persistence.atomic -> camctl.logging_runtime.service -> camctl.bootstrap.lifecycle"
        ]
    else:
        assert violations == []
