"""一份原片的选择规则由完整选择与逐产物候选共同使用。"""

from dataclasses import replace
from contextlib import ExitStack
from enum import IntEnum
from importlib.util import find_spec, module_from_spec
import sys
from unittest.mock import create_autospec, patch

import pytest

from camctl.contracts.values import ConsistencyError
from camctl import resources
from camctl.contracts import enums, schemas, workflow_errors
from camctl.outputs.catalog import OutputKind
from camctl.outputs.definitions import SelectionMode


def _isolated_sources():
    # 仅提供本组选择分支消费的登记样本；真实登记组合由集成测试验证。
    definitions = {
        "actions.type": {"CAMERA_RECORD": 2},
        "actions.source_resolution_state": {"PENDING": 1, "FIXED": 2, "FAILED": 3},
        "obtain_items.status": {"UNRESOLVED": 1, "SELECTED": 2, "FAILED": 4},
        "obtain_items.basis": {"ORIGINAL": 1, "REPAIRED": 2, "PREVIEW": 3,
                               "REPAIRED_NOT_LARGER": 4, "EXPLICIT": 5},
        "outputs.availability": {"AVAILABLE": 1, "RESTRICTED": 2, "CLEANED": 3,
                                 "MISSING": 4, "UNKNOWN": 5},
    }
    item_codes = {
        ("obtain_items", "output_not_found"): 1,
        ("obtain_items", "output_source_mismatch"): 2,
        ("obtain_items", "output_unavailable"): 3,
        ("obtain_items", "output_cleanup_started"): 4,
        ("obtain_items", "source_file_unconfirmed"): 6,
        ("obtain_items", "preview_missing"): 8,
        ("obtain_source_selections", "no_outputs"): 1,
        ("obtain_source_selections", "preview_missing"): 2,
    }
    with ExitStack() as stack:
        readers = _block_resources(stack)
        stack.enter_context(patch.object(enums, "enum_for", create_autospec(
            enums.enum_for, side_effect=lambda column: IntEnum(column, definitions[column]))))
        stack.enter_context(patch.object(workflow_errors, "item_error_id", create_autospec(
            workflow_errors.item_error_id, side_effect=lambda table, name: item_codes[table, name])))
        stack.enter_context(patch.object(workflow_errors, "action_error_id", create_autospec(
            workflow_errors.action_error_id, return_value=99)))
        spec = find_spec("camctl.outputs.sources")
        module = module_from_spec(spec)
        # dataclass 在定义时查询所属模块；离开后恢复原实例，不修改真实登记缓存。
        stack.enter_context(patch.dict(sys.modules, {spec.name: module}))
        spec.loader.exec_module(module)
        for reader in readers:
            reader.assert_not_called()
    return module


def _block_resources(stack):
    readers = []
    for module in (resources, enums, schemas, workflow_errors):
        reader = create_autospec(module.resource_bytes,
            side_effect=AssertionError("单元测试不能读取真实包资源"))
        stack.enter_context(patch.object(module, "resource_bytes", reader))
        readers.append(reader)
    return readers


@pytest.fixture(autouse=True)
def no_resource_reads():
    with ExitStack() as stack:
        readers = _block_resources(stack)
        yield
        for reader in readers:
            reader.assert_not_called()


sources = _isolated_sources()
CatalogEntry, ItemStatus = sources.CatalogEntry, sources.ItemStatus


ORIGINAL = CatalogEntry(101, OutputKind.ORIGINAL, 1, size_bytes=1000)
PREVIEW = CatalogEntry(102, OutputKind.PREVIEW, 1, original_output_id=101, size_bytes=100)
REPAIRED = CatalogEntry(103, OutputKind.REPAIRED, 1, original_output_id=101, size_bytes=80)


def test_default_without_repair_selects_original():
    result = sources.select_for_original(11, ORIGINAL, SelectionMode.DEFAULT, preview=PREVIEW)
    assert (result.output_id, result.basis, result.status, result.original_output_id) == (101, 1, ItemStatus.SELECTED, None)


@pytest.mark.parametrize("availability,status,code", [
    (1, ItemStatus.SELECTED, None), (2, ItemStatus.FAILED, 4),
    (3, ItemStatus.FAILED, 3), (4, ItemStatus.FAILED, 3), (5, ItemStatus.SELECTED, None),
])
def test_default_repair_is_selected_without_fallback(availability, status, code):
    result = sources.select_for_original(11, ORIGINAL, SelectionMode.DEFAULT,
        repaired=replace(REPAIRED, availability=availability))
    assert (result.output_id, result.basis, result.status, result.error_code) == (103, 2, status, code)
    assert result.original_output_id == 101
    if code:
        assert result.error_details["output_id"] == "103"


@pytest.mark.parametrize("repaired", [None, REPAIRED])
def test_preview_requires_actual_preview_even_when_repair_exists(repaired):
    result = sources.select_for_original(11, ORIGINAL, SelectionMode.PREVIEW, repaired=repaired)
    assert result.output_id is None
    assert result.status is ItemStatus.FAILED
    assert result.error_code == 8
    assert result.error_details == {"source_action_instance_id": "11", "original_output_id": "101"}


def test_preview_without_repair_does_not_compare_sizes():
    result = sources.select_for_original(11, ORIGINAL, SelectionMode.PREVIEW, preview=PREVIEW)
    assert (result.output_id, result.basis, result.preview_output_id) == (102, 3, 102)
    assert result.preview_size is None
    assert result.repaired_size is None


@pytest.mark.parametrize("size,output_id,basis", [(80, 103, 4), (100, 103, 4), (120, 102, 3)])
def test_preview_compares_complete_sizes_including_equality(size, output_id, basis):
    result = sources.select_for_original(11, ORIGINAL, SelectionMode.PREVIEW,
        preview=PREVIEW, repaired=replace(REPAIRED, size_bytes=size))
    assert (result.output_id, result.basis, result.status) == (output_id, basis, ItemStatus.SELECTED)
    assert (result.original_output_id, result.preview_output_id, result.preview_size, result.repaired_size) == (101, 102, 100, size)


@pytest.mark.parametrize("preview_size,repaired_size", [(None, 80), (100, None), (None, None)])
def test_preview_unknown_size_preserves_failed_preview_target(preview_size, repaired_size):
    result = sources.select_for_original(11, ORIGINAL, SelectionMode.PREVIEW,
        preview=replace(PREVIEW, size_bytes=preview_size), repaired=replace(REPAIRED, size_bytes=repaired_size))
    assert (result.output_id, result.basis, result.status, result.error_code) == (102, 3, ItemStatus.FAILED, 6)
    assert result.error_details == {"output_id": "102"}
    assert result.preview_output_id == 102


@pytest.mark.parametrize("availability,status,code", [
    (2, ItemStatus.FAILED, 4), (3, ItemStatus.FAILED, 3),
    (4, ItemStatus.FAILED, 3), (5, ItemStatus.SELECTED, None),
])
@pytest.mark.parametrize("size,selected", [(80, 103), (120, 102)])
def test_preview_keeps_chosen_identity_and_size_evidence(availability, status, code, size, selected):
    result = sources.select_for_original(11, ORIGINAL, SelectionMode.PREVIEW,
        preview=replace(PREVIEW, availability=availability if selected == 102 else 1),
        repaired=replace(REPAIRED, size_bytes=size, availability=availability if selected == 103 else 1))
    assert (result.output_id, result.status, result.error_code) == (selected, status, code)
    assert (result.preview_size, result.repaired_size) == (100, size)


@pytest.mark.parametrize("original,preview,repaired", [
    (PREVIEW, None, None),
    (replace(ORIGINAL, original_output_id=99), None, None),
    (ORIGINAL, REPAIRED, None),
    (ORIGINAL, None, PREVIEW),
    (ORIGINAL, replace(PREVIEW, original_output_id=99), None),
    (ORIGINAL, None, replace(REPAIRED, original_output_id=None)),
    (ORIGINAL, replace(PREVIEW, output_id=101), None),
    (ORIGINAL, PREVIEW, replace(REPAIRED, output_id=102)),
])
@pytest.mark.parametrize("mode", [SelectionMode.DEFAULT, SelectionMode.PREVIEW])
def test_selection_rejects_mixed_roles_or_original_identity(original, preview, repaired, mode):
    with pytest.raises(ConsistencyError):
        sources.select_for_original(11, original, mode, preview=preview, repaired=repaired)


@pytest.mark.parametrize("mode", [SelectionMode.EXPLICIT_IDS, None, "preview"])
def test_original_selection_rejects_modes_without_family_rules(mode):
    with pytest.raises(ValueError):
        sources.select_for_original(11, ORIGINAL, mode)
