"""绑定失败申请的产物目录构造契约。"""

from copy import deepcopy
from dataclasses import replace

import pytest

from camctl.capture.media import RecordingFailure
from camctl.outputs.catalog import FileReference, OutputCatalogFacts, OutputDraft, OutputKind
from camctl.persistence.repositories.capture import FinishBindingFailure


@pytest.fixture
def binding_request():
    original_a = OutputDraft(OutputKind.ORIGINAL, FileReference(device_file_id=11), True)
    original_b = OutputDraft(OutputKind.ORIGINAL, FileReference(device_file_id=12), True)
    preview = OutputDraft(OutputKind.PREVIEW, FileReference(device_file_id=13), True,
                          original_batch_file_id=11)
    return FinishBindingFailure(
        action_id=23,
        occurred_at=1_000,
        failure=RecordingFailure("device_binding_unavailable", {
            "device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": "missing"}),
        drafts=(preview, original_b, original_a),
        catalog_facts=OutputCatalogFacts(23, True),
    )


def test_binding_directory_input_accepts_complete_paired_drafts(binding_request):
    preview, original_b, original_a = binding_request.drafts
    assert (preview.kind, original_b.kind, original_a.kind) == (
        OutputKind.PREVIEW, OutputKind.ORIGINAL, OutputKind.ORIGINAL)
    assert preview.original_batch_file_id == original_a.file.device_file_id == 11
    assert binding_request.catalog_facts == OutputCatalogFacts(binding_request.action_id, True)


def test_binding_directory_input_accepts_empty_drafts_without_catalog(binding_request):
    original = deepcopy(binding_request)

    empty = replace(binding_request, drafts=(), catalog_facts=None)

    assert empty.drafts == () and empty.catalog_facts is None
    assert binding_request == original
    preview, _, original_a = binding_request.drafts
    assert preview.original_batch_file_id == original_a.file.device_file_id == 11


@pytest.mark.parametrize("invalid", [
    "list_drafts", "catalog_missing", "catalog_other_action", "catalog_unowned", "catalog_bool_as_int",
    "incomplete", "complete_as_int", "noncanonical_sha", "intermediate_repaired", "empty_with_catalog",
])
def test_binding_directory_input_rejects_noncanonical_members(binding_request, invalid):
    request = binding_request
    preview, b, a = request.drafts
    candidates = {
        "list_drafts": lambda: replace(request, drafts=list(request.drafts)),
        "catalog_missing": lambda: replace(request, catalog_facts=None),
        "catalog_other_action": lambda: replace(request, catalog_facts=OutputCatalogFacts(request.action_id + 1, True)),
        "catalog_unowned": lambda: replace(request, catalog_facts=OutputCatalogFacts(request.action_id, False)),
        "catalog_bool_as_int": lambda: replace(request, catalog_facts=OutputCatalogFacts(request.action_id, 1)),
        "incomplete": lambda: replace(request, drafts=(preview, b, replace(a, file_complete=False))),
        "complete_as_int": lambda: replace(request, drafts=(preview, b, replace(a, file_complete=1))),
        "noncanonical_sha": lambda: replace(request, drafts=(preview, b, replace(a, sha256="ab" * 32))),
        "intermediate_repaired": lambda: replace(request, drafts=(OutputDraft(OutputKind.REPAIRED,
            FileReference(intermediate_file_id=1), True, original_batch_file_id=a.file.device_file_id),)),
        "empty_with_catalog": lambda: replace(request, drafts=()),
    }

    with pytest.raises((TypeError, ValueError)):
        candidates[invalid]()
