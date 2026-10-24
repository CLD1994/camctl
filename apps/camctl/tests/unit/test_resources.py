"""资源访问名称的局部校验不读取真实资源目录。"""

import pytest

from camctl import resources


def test_resource_names_reject_arbitrary_paths(monkeypatch) -> None:
    def unexpected_resource_read(package):
        raise AssertionError("拒绝资源名时不应读取包资源目录")

    monkeypatch.setattr(
        resources,
        "available_resources",
        lambda: frozenset({"protocol/registered.schema.json"}),
    )
    monkeypatch.setattr(resources.importlib_resources, "files", unexpected_resource_read)

    for bad in [
        "../resources.py",
        "..\\..\\..\\..\\pyproject.toml",
        "/etc/passwd",
        "protocol/../../../pyproject.toml",
        "protocol/nonexistent.schema.json",
        "",
    ]:
        with pytest.raises(resources.ResourceError):
            resources.resource_bytes(bad)
