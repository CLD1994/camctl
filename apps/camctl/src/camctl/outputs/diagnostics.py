"""取回结果以原错误登记和原回执交付诊断，不补查当前业务状态。"""

from camctl.contracts.workflow_errors import registered_error_spec
from camctl.logging_runtime.diagnostics import DiagnosticState, record_error
from camctl.operations.models import ErrorValue
from camctl.outputs.qualification import QualificationOutcome


def registered_result_error(registry_key, code, details):
    name, spec = registered_error_spec(registry_key, code)
    return ErrorValue(name, spec["stage"], {} if details is None else details)


def record_source_resolution(result, action_id, *, state=None, context=None):
    """来源解析的原有限结果同时供取回和清理消费。"""
    if result.error_code is not None:
        error = registered_result_error("action_error_id", result.error_code, result.error_details)
        record_error(error, stage=error.stage,
            context={"action_id": action_id, **(context or {})},
            state=result.diagnostic if state is None else state)


def record_selection(snapshot, *, action_id, selection_id, source_action_id):
    from camctl.persistence.repositories.outputs import _source_error_details

    failures = []
    if snapshot.source_error_code is not None:
        failures.append(registered_result_error("item_error_ids.obtain_source_selections",
            snapshot.source_error_code, _source_error_details(snapshot.source_error_code, source_action_id)))
    for item in snapshot.items:
        if item.error_code is not None:
            failures.append({"output_id": item.output_id, "requested_output_id": item.requested_output_id,
                "error": registered_result_error("item_error_ids.obtain_items",
                    item.error_code, item.error_details)})
    if failures:
        record_error({"failures": failures}, stage="output_selection",
            context={"action_id": action_id, "selection_id": selection_id,
                     "source_action_id": source_action_id}, state=snapshot.diagnostic)


def record_qualification(result, candidate):
    if result.outcome is not QualificationOutcome.REJECTED_FINAL or result.error_code is None:
        return
    error = registered_result_error("item_error_ids.obtain_items", result.error_code, result.error_details)
    record_error(error, stage=error.stage, state=result.diagnostic,
        context={"action_id": candidate.action_id, "item_id": candidate.item_id,
            "output_id": candidate.output_id, "source_device_file_id": candidate.source_device_file_id,
            "source_intermediate_file_id": candidate.source_intermediate_file_id})
