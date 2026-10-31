"""预览没有登记时，从完整或未完成的真实观察分类其取回失败。"""

from camctl.capture.result_inputs import ObservedFile, ResultPage
from camctl.capture.results import FileKind
from camctl.operations.models import CallOutcome, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.outputs.preview_facts import unconfirmed_previews


ORIGINALS = {41: "/DCIM/CAM_001/first.OSV", 42: "/DCIM/CAM_001/second.OSV"}


def page(*, observations=(), entries=(), complete=True, error=None):
    outcome = CallOutcome(error=error, settlement=Settlement(SettlementBasis.OBSERVED,
        EvidenceValue("result_returned", 1, {"optional_preview_observations": list(observations)})))
    return ResultPage(tuple(entries), None, None, complete, None, outcome, complete)


def test_tx2_timeout_and_unavailable_observations_are_not_reliable_absence():
    saved = page(observations=(
        {"preview_identity": "/DCIM/CAM_001/first.LRF", "state": "scan_failed", "scan_complete": False},
        {"preview_identity": "/DCIM/CAM_001/second.LRF", "state": "scan_unavailable", "scan_complete": False},
    ))
    assert unconfirmed_previews(ORIGINALS, [saved]) == frozenset({41, 42})


def test_complete_scope_without_preview_observation_proves_absence():
    assert unconfirmed_previews(ORIGINALS, [page()]) == frozenset()


def test_complete_fresh_pair_scan_can_confirm_preview_disappeared():
    saved = page(observations=({
        "preview_identity": "/DCIM/CAM_001/first.LRF", "state": "preview_missing",
        "scan_complete": True, "observed_preview_identities": [],
    },))
    assert unconfirmed_previews(ORIGINALS, [saved]) == frozenset()


def test_another_preview_identity_is_not_absence():
    saved = page(observations=({
        "preview_identity": "/DCIM/CAM_001/first.LRF", "state": "preview_missing",
        "scan_complete": True, "observed_preview_identities": ["/DCIM/CAM_001/first.lrf"],
    },))
    assert unconfirmed_previews(ORIGINALS, [saved]) == frozenset({41})


def test_empty_or_partial_scan_cannot_prove_absence():
    assert unconfirmed_previews(ORIGINALS, []) == frozenset({41, 42})
    assert unconfirmed_previews(ORIGINALS, [page(complete=False)]) == frozenset({41, 42})
    assert unconfirmed_previews(ORIGINALS, [page(error=ErrorValue("adb_directory_failed", "device_file_access"))]) == frozenset({41, 42})
    assert unconfirmed_previews(ORIGINALS, [
        page(complete=False, error=ErrorValue("adb_directory_failed", "device_file_access")), page(),
    ]) == frozenset({41, 42})


def test_pairing_from_another_directory_or_stem_does_not_change_this_family():
    saved = page(observations=(
        {"preview_identity": "/DCIM/backup/first.LRF", "state": "scan_failed"},
        {"preview_identity": "/DCIM/CAM_001/First.LRF", "state": "scan_failed"},
    ))
    assert unconfirmed_previews(ORIGINALS, [saved]) == frozenset()


def test_segmented_raw_preview_without_formal_output_is_also_unconfirmed():
    preview = ObservedFile("/DCIM/CAM_001/first.LRF", {}, {}, False, None,
        kind=FileKind.VIDEO, format_id="lrf")
    assert unconfirmed_previews(ORIGINALS, [page(entries=(preview,))]) == frozenset({41})


def test_observation_on_an_earlier_page_is_retained_until_complete_scope():
    first = page(observations=({"preview_identity": "/DCIM/CAM_001/first.LRF", "state": "zero_size"},), complete=False)
    assert unconfirmed_previews(ORIGINALS, [first, page()]) == frozenset({41})
