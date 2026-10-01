"""编码结构由独立内存登记提供，不读取资源文件。"""
from copy import deepcopy
import json
from unittest.mock import create_autospec

import pytest

from camctl.contracts import public_projection as projection


@pytest.fixture
def registry(monkeypatch):
    document = {"format_version": 1, "projections": {"sample": {"root_table": "samples", "fields": {
        "later": {"value": {"op": "entities", "entity": "second", "encoding_order": 2}},
        "identity": {"value": {"op": "read", "column": "samples.id", "encoding": "id"}},
        "earlier": {"value": {"op": "entities", "entity": "first", "encoding_order": 1}},
    }}}}
    def read(name):
        assert name == "registry/report-dependencies.json"
        return json.dumps(document).encode("utf-8")
    resource = create_autospec(projection.resource_bytes, side_effect=read)
    monkeypatch.setattr(projection, "resource_bytes", resource)
    projection._dependencies.cache_clear()
    yield document
    projection._dependencies.cache_clear()


def test_entity_structure_uses_explicit_order_after_field_reordering(registry):
    structure = projection.projection_structure("sample")
    assert structure.identity_field == "identity"
    assert structure.entity_fields == (("earlier", "first"), ("later", "second"))


def test_integral_exact_json_order_is_accepted(registry):
    registry["projections"]["sample"]["fields"]["later"]["value"]["encoding_order"] = 2.0
    assert projection.projection_structure("sample").entity_fields == (("earlier", "first"), ("later", "second"))


@pytest.mark.parametrize("bad", [None, True, False, 0, -1, "2", 2.5, 1, 3])
def test_invalid_or_incomplete_collection_order_is_rejected(registry, bad):
    registry["projections"]["sample"]["fields"]["later"]["value"]["encoding_order"] = bad
    with pytest.raises(projection.PublicProjectionError):
        projection.projection_structure("sample")


def test_missing_collection_order_is_rejected(registry):
    del registry["projections"]["sample"]["fields"]["later"]["value"]["encoding_order"]
    with pytest.raises(projection.PublicProjectionError):
        projection.projection_structure("sample")


def test_multiple_identity_fields_are_rejected(registry):
    fields = registry["projections"]["sample"]["fields"]
    fields["other_identity"] = deepcopy(fields["identity"])
    with pytest.raises(projection.PublicProjectionError):
        projection.projection_structure("sample")


def test_unknown_projection_structure_is_rejected(registry):
    with pytest.raises(projection.PublicProjectionError):
        projection.projection_structure("unknown")
