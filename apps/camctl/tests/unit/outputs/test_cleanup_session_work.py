"""会话不以单个 QUERY 集合代替全部原保存责任。"""

import pytest

from camctl.bootstrap.cleanup_assembly import cleanup_flow


@pytest.mark.parametrize("phase", range(5))
def test_cleanup_owner_counts_every_original_pending_collection(phase):
    collections = tuple({} for _ in range(5))
    flow = cleanup_flow(lambda _: None, pending_collections=collections)
    assert flow.required_settlements() == 0
    collections[phase]["original"] = object()
    assert flow.required_settlements() == 1
    collections[phase].clear()
    assert flow.required_settlements() == 0


def test_generic_recovery_types_preserve_capture_import_identity():
    from camctl.capture import recovery as capture
    from camctl.operations import recovery as operations

    for name in (
        "RecoveryBoundary",
        "RecoveryBlockedReason",
        "RecoveryDiagnostic",
        "recovery_registry",
    ):
        assert getattr(capture, name) is getattr(operations, name)


@pytest.mark.parametrize("operation", ["delete", "query"])
def test_host_recovery_uses_generic_registry_and_original_adapter_declaration(
    operation,
):
    from camctl.bootstrap.cleanup_assembly import HostArtifacts
    from camctl.operations.recovery import recovery_registry

    registry = recovery_registry(HostArtifacts, operation)
    contract = registry.contract("local_file_recovery", 1)
    assert (
        contract.operation == operation
        and not contract.fields
        and contract.identity_field is None
    )


@pytest.mark.parametrize("horizon", [True, -1, 1.0, "1"])
def test_recovery_finish_rejects_invalid_horizon(horizon):
    from camctl.operations.attempts import AttemptFinish, AttemptConfigError
    from camctl.operations.models import AttemptTicket
    from camctl.operations.validation import validate_outcome
    from camctl.devices.evidence import EvidenceRegistry
    from camctl.outputs.cleanup_flow import _not_dispatched, _not_dispatched_evidence

    ticket = AttemptTicket(1, "query", "1", "exists/1", 1)
    validated = validate_outcome(
        ticket,
        _not_dispatched("query").outcome,
        _not_dispatched_evidence(EvidenceRegistry(()), ticket),
    )
    with pytest.raises(AttemptConfigError):
        AttemptFinish(ticket, validated, 1, recovery_max_event_id=horizon)
