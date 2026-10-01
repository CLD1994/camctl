"""参数规则单元测试不读取真实公共 Schema 资源。"""
import pytest
from referencing import Registry
from camctl.contracts import schemas


@pytest.fixture(autouse=True)
def local_schema_registry(monkeypatch):
    monkeypatch.setattr(schemas, "schema_registry", lambda: Registry())
