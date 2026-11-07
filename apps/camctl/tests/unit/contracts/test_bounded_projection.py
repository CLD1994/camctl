"""有界投影公开契约；资源读取在稳定资源入口隔离。"""
import json
from unittest.mock import create_autospec

import pytest
from camctl.contracts import public_projection as p


@pytest.fixture
def registry(monkeypatch):
    doc = {"format_version": 1, "relations": {}, "projections": {
        "sample": {"root_table": "samples", "fields": {
            "id": {"value": {"op": "read", "column": "samples.id", "encoding": "id"}},
            "items": {"when": {"op": "read", "column": "samples.include"},
                      "value": {"op": "rows", "relations": ["must_not_expand"], "projection": "item"}},
        }}}}
    monkeypatch.setattr(p, "resource_bytes", create_autospec(p.resource_bytes,
        side_effect=lambda _: json.dumps(doc).encode()))
    p._dependencies.cache_clear()
    yield doc
    p._dependencies.cache_clear()


def facts(include=True):
    return p.ProjectionInput("sample", 1, {"samples": {1: {"include": include}}},
                             deferred_fields={"sample": frozenset({"items"})})


@pytest.mark.parametrize("include,expected", [(True,"keep_empty"),(False,"omit")])
def test_condition_is_decided_without_reading_array_members(registry, include, expected):
    result = p.project_bounded(facts(include))
    assert result.finite == {"id": "1"}
    assert result.deferred[("items",)].value == expected


def test_empty_omission_is_distinct_from_condition_omission(registry):
    registry["projections"]["sample"]["fields"]["items"]["value"]["empty"] = "omit"
    assert p.project_bounded(facts()).deferred[("items",)].value == "omit_empty"


def test_missing_condition_fact_is_not_silent_omission(registry):
    f = p.ProjectionInput("sample", 1, {"samples": {1: {}}}, deferred_fields=facts().deferred_fields)
    with pytest.raises(p.PublicProjectionError, match="sample.items"):
        p.project_bounded(f)


def test_nested_decisions_have_public_paths(registry):
    registry["projections"]["outer"] = {"root_table":"samples", "fields": {
        "result": {"value":{"op":"project", "projection":"sample"}}}}
    f = p.ProjectionInput("outer", 1, facts().tables, deferred_fields=facts().deferred_fields)
    result = p.project_bounded(f)
    assert result.finite == {"result":{"id":"1"}}
    assert result.deferred[("result","items")].value == "keep_empty"


def test_full_projection_remains_a_plain_fragment(registry):
    assert p.project_public(facts()) == {"id":"1"}


def test_missing_related_target_is_an_error(registry):
    registry["relations"] = {"target": {"from":"samples.target_id", "to":"targets.id"}}
    spec = registry["projections"]["sample"]
    spec["relations"] = ["target"]
    spec["fields"]["items"]["when"] = {"op":"read", "column":"targets.include"}
    f = p.ProjectionInput("sample", 1, {"samples":{1:{"target_id":7}}},
                          deferred_fields=facts().deferred_fields)
    with pytest.raises(p.PublicProjectionError):
        p.project_bounded(f)


def test_parent_omission_does_not_invent_descendant_decisions(registry):
    registry["projections"]["outer"] = {"root_table":"samples", "fields": {
        "result": {"when":{"op":"literal", "value":False},
                   "value":{"op":"project", "projection":"sample"}}}}
    f = p.ProjectionInput("outer", 1, facts().tables, deferred_fields=facts().deferred_fields)
    result = p.project_bounded(f)
    assert result.finite == {}
    assert result.deferred == {}


def test_literal_empty_growth_array_still_requires_streaming(registry):
    registry["projections"]["sample"]["fields"]["items"]["value"] = {"op":"literal", "value":[]}
    result = p.project_bounded(facts())
    assert result.finite == {"id":"1"}
    assert result.deferred[("items",)] is p.ArrayEmission.KEEP_EMPTY


def test_deferred_field_below_materialized_array_is_rejected(registry):
    registry['relations'] = {'children':{'from':'samples.id','to':'children.parent_id'}}
    registry['projections']['sample']['fields']['items'] = {'value': {
        'op':'rows','relations':['children'],'projection':'child'}}
    registry['projections']['child'] = {'root_table':'children','fields':{
        'later':{'value':{'op':'literal','value':[]}}}}
    f = p.ProjectionInput('sample',1,{'samples':{1:{'include':True}},
        'children':{2:{'parent_id':1}}}, deferred_fields={'child':frozenset({'later'})})
    with pytest.raises(p.PublicProjectionError):
        p.project_bounded(f)
