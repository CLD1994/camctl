"""业务存储的事件规则适配与登记。"""

from __future__ import annotations

from typing import Mapping
from camctl.capture.photo_pair_rules import _HostPhotoPairError, _device_photo_source_pair, _host_photo_execution_spec, _host_photo_registered_pair, _host_photo_source_pair, host_photo_metadata_error, host_photo_promotion_failure, host_photo_sources_promoted, _FILE_ROLE, _HOST_PURPOSE_FOR_KIND, _KIND_CODES, _OUTPUT_KIND
from camctl.capture.output_file_rules import capture_binding_error, device_output_error, initial_output_error, device_source_ownership_error, intermediate_output_ownership_error, output_carrier_error, repair_output_error, derivative_origin_error, existing_derivative_error, origin_links_error, preview_source_pair_error, duplicate_derivative_error
from camctl.capture.terminal_rules import ACTION_TERMINAL as _ACTION_TERMINAL, activity_capture_action_error, activity_capture_transition_error, preparation_failure_action_error, recording_success_error, requires_segment_finish, successful_action_error
from camctl.capture.result_set_rules import TIME_AND_OUTPUTS_METHOD as _TIME_AND_OUTPUTS_METHOD, RESULT_BEGIN_REASON as _RESULT_BEGIN_REASON, RESULT_COMPLETE_REASON as _RESULT_COMPLETE_REASON, RESULT_PHASE_TARGETS as _RESULT_PHASE_TARGETS, result_begin_error, result_conclusion_error
from camctl.capture.emergency_rules import emergency_attempt_count_error, emergency_attempt_error, emergency_attempt_presence_error, emergency_failure_error, emergency_identity_error, emergency_observation_error, emergency_run_status_error, emergency_target_error
from camctl.capture.device_file_rules import checksum_decision_error, checksum_value_error, completion_facts_error, completion_wait_reference_error, incomplete_facts_error, ownership_claim_error, ownership_pair_error
from camctl.capture.activity_completion_rules import result_page_confirms_completion, start_result_confirms_completion, stop_result_confirms_completion
from camctl.capture.models import HostTimerStopSave, validate_capture_result
from camctl.capture.result_inputs import saved_outcome
from camctl.capture.processing import RepairOutcome
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import is_json_integer, json_equal, parse_exact_json
from camctl.contracts.schemas import SchemaRuleError
from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.contracts.workflow_errors import validate_public_error
from camctl.acceptance.definitions import read_action_spec
from camctl.devices.recording_completion import completed_file_wait_source, is_completed_file_wait
from camctl.history.decoding import decode_event_row
from camctl.history.validators import EventValidationError
from camctl.outputs.catalog import OutputKind
from camctl.capture.release_rules import release_basis_holds, output_scope_resolved, unstarted_release_allowed
from camctl.persistence.repositories.operations_guards import _check_config_numbers
from camctl.operations.models import AttemptTicket, EffectState


_ACTION_FINISHED_EVENT = 8


_INTERMEDIATE_FILE_EVENT = 26


_DEVICE_FILE_EVENT = 17


_ATTEMPT_RESULT_EVENT = 12


_RUN_KIND = enum_for("operation_runs.kind")


_ATTEMPT_STATUS = enum_for("operation_attempts.status")


_RESULT_SET_EVENT = 16


_RESULT_OUTPUTS_FINALIZED_REASON = 5


_FILE_CREATE_REASON = 1


_FILE_OWNERSHIP_REASON = 2


_FILE_COMPLETE_REASON = 3


_FILE_CHECKSUM_REASON = 4


_FILE_PRESENCE_REASON = 5


_FILE_PRESENCE = enum_for("device_files.presence_state")


_COMPLETION_BASIS = enum_for("device_files.completion_evidence_json.basis")


_FILE_COMPLETION_NEXT = {
    1: frozenset({2, 3, 4}),
    2: frozenset({3, 4}),
    4: frozenset({3}),
}


_LIFECYCLE_REASON = 2


_RETENTION = enum_for("intermediate_files.retention_state")


_ACTION_SUCCEEDED = 3


_ACTION_TYPE = enum_for("actions.type")


def _guard_host_photo_completion(
    context, action_id: int, *, host_spec: bool, terminal: bool, device_spec: bool = False,
) -> None:
    source_rows = context.complete_rows(
        "intermediate_files", "owner_action_id", action_id)
    if device_spec:
        device_rows = context.complete_rows("device_files", "source_action_id", action_id)
        sources = _device_photo_source_pair(device_rows, source_rows, action_id, terminal=terminal,
            action=context.state_rows["actions"][action_id])
        if context.photo_result_reads is None:
            raise EventValidationError("设备源登记缺少原拍摄结果读取端口")
        context.photo_result_reads.verify(context.state_rows["actions"][action_id], sources,
                                         before_event_id=context.transaction.first_event_id)
    else:
        sources = _host_photo_source_pair(
            source_rows, action_id, host_spec=host_spec, terminal=terminal,
            action=context.state_rows["actions"][action_id])
    if sources is None:
        return
    context.complete_rows("outputs", "source_action_id", action_id)
    transaction_rows = context.transaction_rows
    if transaction_rows is None:
        raise EventValidationError("成功单张拍摄缺少同事务完整产物提案")
    outputs = transaction_rows.get("outputs", {})
    origins = transaction_rows.get("output_origins", {})
    _host_photo_registered_pair(outputs, origins, action_id, sources, device=device_spec,
        action=context.state_rows["actions"][action_id])
    action_outputs = [
        (identity, values)
        for identity, values in outputs.items()
        if values.get("source_action_id") == action_id
    ]
    for output_id, _ in action_outputs:
        context.complete_rows("output_origins", "output_id", output_id)
    original_output_id = next(
        identity for identity, values in action_outputs
        if values.get("kind") == _KIND_CODES[OutputKind.ORIGINAL])
    context.complete_rows(
        "output_origins", "original_output_id", original_output_id)


def _guard_facts(context, table: str, row_id: int) -> dict[str, Any]:
    facts = dict(context.state_rows.get(table, {}).get(row_id, {}))
    facts.setdefault("id", row_id)
    return facts


def _action_finish_guard(event, context) -> None:
    """动作终态组合与产物同事务校验。"""
    if event.event_type == _ACTION_FINISHED_EVENT and event.reason == 7:
        from .preparation import PreparationReads
        from .availability_failure import verify_photo_failure
        if type(context.preparation_reads) is not PreparationReads:
            raise EventValidationError("可用性失败缺少原连接事实")
        verify_photo_failure(context.preparation_reads.connection, event, context)
    if event.event_type == _ACTION_FINISHED_EVENT and event.reason != 6:
        for row in event.rows:
            details = row.after.values.get("error_details_json") if row.table == "actions" else None
            if isinstance(details, Mapping) and details.get("reason") == "physical_identity_conflict":
                from .preparation import PreparationReads
                from .physical_alias import action_physical_identity, physical_alias_conflict
                if type(context.preparation_reads) is not PreparationReads:
                    raise EventValidationError("机身冲突失败缺少原连接事实")
                action = _guard_facts(context, "actions", row.row_id)
                connection = context.preparation_reads.connection
                if not physical_alias_conflict(connection, action["device_id"],
                                               action_physical_identity(connection, row.row_id)):
                    raise EventValidationError("机身冲突失败缺少未完成原责任")
    if event.event_type == _ACTION_FINISHED_EVENT and event.reason == 6:
        from .preparation import PreparationReads, verify_binding_preparation_failure
        from camctl.contracts.workflow_errors import action_error_id
        if type(context.preparation_reads) is not PreparationReads:
            raise EventValidationError("准备绑定失败缺少原连接事实")
        request = event.evidence.get("preparation_binding_request")
        for row in event.rows:
            if row.table != "actions":
                continue
            action = _guard_facts(context, "actions", row.row_id)
            if (request["action_id"] != row.row_id or request["occurred_at"] != event.occurred_at
                    or row.after.values.get("error_code") != action_error_id("device_binding_unavailable")
                    or not json_equal(row.after.values.get("error_details_json"), request["details"])):
                raise EventValidationError("准备绑定失败与原申请不符")
            try:
                verify_binding_preparation_failure(context.preparation_reads.connection, action, request)
            except (ValueError, TypeError, KeyError) as error:
                raise EventValidationError(str(error)) from error
    if event.event_type == _ACTION_FINISHED_EVENT and event.reason == 5:
        from .preparation import PreparationReads
        from camctl.contracts.workflow_errors import validate_error_details, action_error_id
        if type(context.preparation_reads) is not PreparationReads:
            raise EventValidationError("准备失败缺少原连接的真实失败核对")
        for row in event.rows:
            if row.table != "actions":
                continue
            action = _guard_facts(context, "actions", row.row_id)
            details = row.after.values.get("error_details_json")
            error = preparation_failure_action_error(action, row.after.values)
            if error is not None:
                raise EventValidationError(error)
            try:
                from camctl.history.validators import parse_preparation_failure_request
                request = parse_preparation_failure_request(event.evidence.get("preparation_failure_request"))
                validate_error_details("capture_failed", details)
                if request != {"action_id": row.row_id, "reason": details["reason"], "occurred_at": event.occurred_at}:
                    raise EventValidationError("准备失败申请与实际动作、原因或时刻不符")
                context.preparation_reads.verify_failure(action, details)
            except (ValueError, TypeError) as error:
                raise EventValidationError(str(error)) from error
    for row in event.rows:
        if row.table == "actions" and row.before.exists:
            after = row.after.values
            if after.get("status") in (3, 4, 6):
                facts=_guard_facts(context,'actions',row.row_id)
                segmented = requires_segment_finish(facts)
                if segmented or 'segment_finish_request' in event.evidence:
                    if context.current_reads is None:
                        raise EventValidationError('分段终态缺少原保存事实读取端口')
                    context.current_reads.verify_segment_finish_event(event,context,row.row_id)
            if after.get("status") == _ACTION_SUCCEEDED:
                facts = _guard_facts(context, "actions", row.row_id)
                facts.update(row.before.values)
                error = successful_action_error(facts)
                if error is not None:
                    raise EventValidationError(error)
                if facts.get("type") == _ACTION_TYPE.CAMERA_RECORD:
                    if context.current_reads is None:
                        raise EventValidationError("录像成功缺少独立的原输入读取端口")
                    error = recording_success_error(context.current_reads.recording_input_issues(row.row_id))
                    if error is not None:
                        raise EventValidationError(error)
                if facts.get("type") == _ACTION_TYPE.CAMERA_TAKE_PHOTO:
                    try:
                        host_spec = _host_photo_execution_spec(facts)
                        _guard_host_photo_completion(
                            context, row.row_id, host_spec=host_spec, terminal=False,
                            device_spec=read_action_spec(facts).get("output_storage") == "device")
                    except ConsistencyError as error:
                        raise EventValidationError(str(error)) from error
                    except _HostPhotoPairError as error:
                        raise EventValidationError(str(error)) from error
        elif row.table == "outputs" and not row.before.exists:
            source = row.after.values.get("source_action_id")
            action = _guard_facts(context, "actions", source)
            if action.get("status") not in _ACTION_TERMINAL:
                raise EventValidationError(
                    f"产物登记要求源动作终态同事务成立: 动作 {source} 状态"
                    f" {action.get('status')}"
                )


def _output_guard(event, context) -> None:
    """从事件当前事实核对来源、原设备绑定、文件角色与提升配对。"""
    from camctl.devices.start_entry import is_hasselblad_photo

    if event.event_type == _INTERMEDIATE_FILE_EVENT:
        _promotion_pairing(event, context)
        return
    for row in event.rows:
        if row.table == "outputs" and not row.before.exists:
            _output_file(row.after.values, context)
            values = row.after.values
            file_id = values.get("device_file_id")
            action = _registration_facts(context, "actions", values.get("source_action_id"))
            if (file_id is not None and is_hasselblad_photo(action)
                    and action["execution_spec_json"]["output_storage"] == "device"):
                source = _registration_facts(context, "device_files", file_id)
                evidence = source.get("completion_evidence_json")
                _photo_source_file_guard(context, event, file_id,
                    evidence.get("observation") if isinstance(evidence, Mapping) else None,
                    role=source.get("role"), sha256=source.get("sha256"), check_sha=True,
                    size_bytes=source.get("size_bytes"), check_size=True)
    _output_relationships(event, context)
    source_actions = {
        row.after.values.get("source_action_id")
        for row in event.rows
        if row.table == "outputs" and not row.before.exists
    }
    for action_id in source_actions:
        action = _registration_facts(context, "actions", action_id)
        if (action.get("type") != _ACTION_TYPE.CAMERA_TAKE_PHOTO
                or action.get("status") != _ACTION_SUCCEEDED):
            continue
        source_rows = context.complete_rows(
            "intermediate_files", "owner_action_id", action_id)
        terminal = host_photo_sources_promoted(source_rows, action_id)
        try:
            host_spec = _host_photo_execution_spec(action)
            _guard_host_photo_completion(
                context, action_id, host_spec=host_spec, terminal=terminal,
                device_spec=read_action_spec(action).get("output_storage") == "device")
        except ConsistencyError as error:
            raise EventValidationError(str(error)) from error
        except _HostPhotoPairError as error:
            raise EventValidationError(str(error)) from error


def _promotion_pairing(event, context) -> None:
    """INTERMEDIATE_FILE_CHANGED.LIFECYCLE：提升只因同事务登记发生。

    推进到 PROMOTED 的中间文件要求先前事件已登记承载它的正式
    产物；释放与交接的保留变化不经本核对。
    """
    if event.reason != _LIFECYCLE_REASON:
        return
    for row in event.rows:
        if row.table != "intermediate_files":
            continue
        if row.after.values.get("retention_state") != int(_RETENTION.PROMOTED):
            continue
        file = context.state_rows.get("intermediate_files", {}).get(row.row_id)
        if file is None:
            raise EventValidationError(
                f"提升缺少当前文件事实: intermediate_files#{row.row_id}")
        owner = file.get("owner_action_id")
        for output in context.state_rows.get("outputs", {}).values():
            if (output.get("kind") in _HOST_PURPOSE_FOR_KIND
                    and output.get("intermediate_file_id") == row.row_id
                    and output.get("source_action_id") == owner):
                break
        else:
            raise EventValidationError(
                "文件提升必须由同事务先行的正式产物登记授权:"
                f" intermediate_files#{row.row_id}")


def _output_file(values, context) -> None:
    """新登记产物及所引用的既有原片共用文件身份与来源校验。"""
    source_id = values["source_action_id"]
    source = _registration_facts(context, "actions", source_id)
    source_binding = _capture_binding(source)
    kind = values.get("kind")
    error = output_carrier_error(values)
    if error is not None:
        raise EventValidationError(error)
    device_file_id = values.get("device_file_id")
    if device_file_id is not None:
        facts = _registration_facts(context, "device_files", device_file_id)
        error = device_source_ownership_error(facts, source_id)
        if error is not None:
            raise EventValidationError(error)
        observer = _registration_facts(context, "actions", facts.get("observer_action_id"))
        if _capture_binding(observer) != source_binding:
            raise EventValidationError("产物来源与文件观察者的原设备或驱动绑定不一致")
        error = device_output_error(facts, kind, device_file_id)
        if error is not None:
            raise EventValidationError(error)
    else:
        facts = _registration_facts(context, "intermediate_files", values.get("intermediate_file_id"))
        error = intermediate_output_ownership_error(facts, source_id)
        if error is not None:
            raise EventValidationError(error)
        if kind == _OUTPUT_KIND.REPAIRED:
            _require_promotable_repair_output(facts)
            _require_repair_success(context, source_id, values.get("intermediate_file_id"))
        else:
            _require_promotable_host_photo(facts, source, kind)
            error = host_photo_metadata_error(values, source, kind)
            if error is not None:
                raise EventValidationError(error)


def _require_promotable_host_photo(facts, source, kind) -> None:
    error, cause = host_photo_promotion_failure(facts, source, kind)
    if error is not None:
        if cause is not None:
            raise EventValidationError(error) from cause
        raise EventValidationError(error)


def _require_promotable_repair_output(facts) -> None:
    """修复成品的承载文件必须仍是未释放的完整修复输出。"""
    error = repair_output_error(facts)
    if error is not None:
        raise EventValidationError(error)


def _require_repair_success(context, source_id: int, file_id) -> None:
    """登记以本动作的修复成功事实为前提；缺失或指向他处均拒绝。"""
    for processing in context.state_rows.get("recording_processing", {}).values():
        if (processing.get("action_id") == source_id
                and processing.get("repair_state") == RepairOutcome.SUCCEEDED.value
                and processing.get("repair_output_file_id") == file_id):
            if context.current_reads is None:
                raise EventValidationError("正式修复提升缺少独立媒体读取端口")
            try:
                context.current_reads.repair_media_proof(source_id, processing["id"], file_id, saved=True)
            except (ConsistencyError, ValueError) as error:
                raise EventValidationError(str(error)) from error
            return
    raise EventValidationError(
        f"产物登记缺少本动作的修复成功事实: intermediate_files#{file_id}")


def _output_relationships(event, context) -> None:
    """登记自己的关系与当前完整反向集合共同决定原片及派生唯一性。"""
    outputs = {row.row_id: row.after.values for row in event.rows
               if row.table == "outputs" and not row.before.exists}
    links: dict[int, list[Mapping[str, Any]]] = {}
    for row in event.rows:
        if row.table != "output_origins":
            continue
        values = row.after.values
        if values["output_id"] not in outputs:
            raise EventValidationError("关联必须属于本事件共同登记的产物")
        links.setdefault(values["output_id"], []).append(values)
    kinds_by_original: dict[int, set[int]] = {}
    for identity, output in outputs.items():
        if context.complete_rows("output_origins", "output_id", identity):
            raise EventValidationError("新产物不能已有原片关联")
        own_links = links.get(identity, ())
        kind = output["kind"]
        error = origin_links_error(identity, kind, own_links)
        if error is not None:
            raise EventValidationError(error)
        if kind == _OUTPUT_KIND.ORIGINAL:
            continue
        original_id = own_links[0]["original_output_id"]
        original = _registration_facts(context, "outputs", original_id)
        error = derivative_origin_error(original, output, kind)
        if error is not None:
            raise EventValidationError(error)
        if context.complete_rows("output_origins", "output_id", original_id):
            raise EventValidationError("引用的原片不能携带派生关联")
        _output_file(original, context)
        if kind == _OUTPUT_KIND.PREVIEW:
            file = (_registration_facts(context, "device_files", output["device_file_id"])
                    if output["device_file_id"] is not None else None)
            error = preview_source_pair_error(original, output, file)
            if error is not None:
                raise EventValidationError(error)
        if original_id not in kinds_by_original:
            related = context.complete_rows("output_origins", "original_output_id", original_id)
            kinds: set[int] = set()
            for relation in related.values():
                previous = _registration_facts(context, "outputs", relation["output_id"])
                error = existing_derivative_error(previous, output, kinds)
                if error is not None:
                    raise EventValidationError(error)
                kinds.add(previous.get("kind"))
            kinds_by_original[original_id] = kinds
        kinds = kinds_by_original[original_id]
        error = duplicate_derivative_error(kind, kinds)
        if error is not None:
            raise EventValidationError(error)
        kinds.add(kind)


def _registration_facts(context, table: str, identity: int | None) -> Mapping[str, Any]:
    facts = context.state_rows.get(table, {}).get(identity)
    if facts is None:
        raise EventValidationError(f"产物登记缺少当前关联记录: {table}#{identity}")
    return facts


def _capture_binding(action: Mapping[str, Any]) -> tuple[str, str]:
    error = capture_binding_error(action)
    if error is not None:
        raise EventValidationError(error)
    return action.get("device_id"), action.get("driver_id")


def _cleanup_aggregate_guard(event, context) -> None:
    """产物初始可用性与清理状态组合校验。"""
    for row in event.rows:
        if row.table != "outputs" or row.before.exists:
            continue
        values = row.after.values
        error = initial_output_error(values)
        if error is not None:
            raise EventValidationError(error)


def _plan_aggregate_guard(event, context) -> None:
    """受理创建时，计划初始状态与全部动作的受理结果一致。"""
    from camctl.acceptance.storage_rules import validate_initial_plan_status

    for row in event.rows:
        if row.table != "plans":
            continue
        if not row.before.exists:
            plan_id = row.row_id
            statuses = tuple(
                values.get("status")
                for values in context.state_rows.get("actions", {}).values()
                if values.get("plan_id") == plan_id
            )
            try:
                validate_initial_plan_status(row.after.values.get("status"), statuses)
            except ValueError as error:
                raise EventValidationError(str(error)) from error
            continue


def register_capture_guards(register) -> None:
    """注册采集终态事务的正式业务守卫（装配期调用）。"""
    register("action_finish", _action_finish_guard)
    register("output", _output_guard)
    register("cleanup_aggregate", _cleanup_aggregate_guard)
    register("plan_aggregate", _plan_aggregate_guard)
    register("device_file", _device_file_guard)
    register("emergency", _emergency_guard)
    from .finite_cancel import register_finite_cancel_guard
    register_finite_cancel_guard(register)
    from .software_recovery import register_software_recovery_guard
    register_software_recovery_guard(register)
    register("activity", _activity_guard)
    register("release", _release_guard)
    register("result_check", _result_check_guard)
    from .baseline import register_baseline_guard
    register_baseline_guard(register)
    from .baseline_reads import register_baseline_read_guard
    register_baseline_read_guard(register)
    from .result_pages import register_result_page_guard
    register_result_page_guard(register)
    from .result_file_ownership import register_ownership_from_result_page_guard
    register_ownership_from_result_page_guard(register)
    from .result_file_pairing import register_result_scan_pairing_guard
    register_result_scan_pairing_guard(register)
    from .segment_inspections import register_segment_inspection_guard
    register_segment_inspection_guard(register)


_FILE_COMPLETION = enum_for("device_files.completion_state")


def _stop_confirms(run, action, activity, actual):
    return (run["kind"] == int(_RUN_KIND.STOP)
            and run["activity_id"] == activity["id"]
            and run["responsibility_key"] == f"stop/{action['id']}"
            and action["type"] in (2, 3)
            and actual.effect is EffectState.CONFIRMED)


def _stop_source_from_rows(event, state):
    """数据库读取及事件守卫复用同一原停止事实核验。"""
    rows = [row for row in event.rows if row.table == "operation_attempts"]
    if event.event_type != _ATTEMPT_RESULT_EVENT or len(rows) != 1:
        raise ConsistencyError("拍摄停止引用的事件不是单次实际结果")
    original, = rows
    attempt = _source_fact(state, "operation_attempts", original.row_id)
    run = _source_fact(state, "operation_runs", attempt["run_id"])
    action = _source_fact(state, "actions", run["action_id"])
    activity = _source_fact(state, "device_activities", run["activity_id"])
    actual = saved_outcome(attempt["status"], attempt["effect_state"], attempt["result_json"], attempt["error_json"])
    if (attempt["result_event_id"] != event.event_id or activity["action_id"] != action["id"]
            or not _stop_confirms(run, action, activity, actual)
            or any(not json_equal(original.after.values.get(name), attempt[name])
                for name in ("status", "effect_state", "result_json", "error_json"))):
        raise ConsistencyError("原结果没有一致的拍摄停止确认")
    return activity, event.occurred_at


_HISTORY_SOURCE_COLUMNS = ("id", "transaction_id", "event_type", "event_version", "occurred_at",
                           "clock_status", "change_seq", "body_json")


def _history_source_event(state, event_id):
    stored = state.get("history_events", {}).get(event_id)
    if (not isinstance(stored, Mapping) or set(stored) != set(_HISTORY_SOURCE_COLUMNS)
            or not json_equal(stored.get("id"), event_id)):
        raise ConsistencyError("文件完成缺少所引用的完整原历史输入")
    return decode_event_row(tuple(stored[name] for name in _HISTORY_SOURCE_COLUMNS))


def _source_fact(state, table, identity):
    if not is_json_integer(identity) or identity <= 0:
        raise ConsistencyError("原文件完成事实的关联身份无效")
    facts = state.get(table, {}).get(identity)
    if not isinstance(facts, Mapping) or not json_equal(facts.get("id"), identity):
        raise ConsistencyError(f"文件完成缺少原关联事实: {table}#{identity}")
    return facts


def _verify_record_result_page(saved, page_event, action, activity, state):
    run = _source_fact(state, "operation_runs", saved.ref.ticket.run_id)
    attempt = _source_fact(state, "operation_attempts", page_event.evidence["attempt_id"])
    first, last = attempt["result_first_page_event_id"], attempt["result_last_page_event_id"]
    if (action["type"] not in (int(_ACTION_TYPE.CAMERA_RECORD), int(_ACTION_TYPE.CAMERA_TIMELAPSE))
            or saved.ref.ticket.target_id != str(activity["id"])
            or run["kind"] != int(_RUN_KIND.CHECK_CAPTURE_RESULTS)
            or run["action_id"] != action["id"] or run["activity_id"] != activity["id"]
            or run["responsibility_key"] != f"results/{activity['id']}"
            or attempt["run_id"] != run["id"] or attempt["attempt_no"] != saved.ref.ticket.attempt_id
            or not is_json_integer(first) or not is_json_integer(last)
            or not first <= page_event.event_id <= last):
        raise ConsistencyError("完成文件的原结果页与动作、活动或可靠页范围不符")


def _verify_file_result_source(saved, page_event, action, activity, state, expected, stop_event_id=None):
    from .result_sources import result_source_from_state
    attempt = _source_fact(state, "operation_attempts", page_event.evidence["attempt_id"])
    source = result_source_from_state(action, activity, attempt, state)
    if source.kind != expected:
        raise ConsistencyError("文件完成改变原 RESULTS 意图采用的来源")
    if stop_event_id is not None and (source.context is None
            or source.context["stop_result_event_id"] != stop_event_id):
        raise ConsistencyError("文件完成没有原意图之前的同一可靠 STOP")


def _verify_stop_wait_file(facts, evidence, size, state, *, event_id=None):
    """原页中的单文件完成与原 STOP、固定等待规则共同提供假设依据。"""
    from .result_pages import _decode
    from .result_sources import ResultSourceKind

    if not isinstance(evidence, Mapping) or set(evidence) != {
            "basis", "observation", "activity_id", "result_page_event_id", "stop_result_event_id"}:
        raise ConsistencyError("停止后等待完成依据必须具有完整且固定的成员")
    for field in ("activity_id", "result_page_event_id", "stop_result_event_id"):
        ObjectId(evidence[field])
    if evidence["basis"] != int(_COMPLETION_BASIS.STOP_RETURN_AND_WAIT):
        raise ConsistencyError("文件完成未使用停止后等待依据")
    page_event = _history_source_event(state, evidence["result_page_event_id"])
    stop_event = _history_source_event(state, evidence["stop_result_event_id"])
    if (stop_event.event_id >= page_event.event_id
            or (event_id is not None and page_event.event_id >= event_id)):
        raise ConsistencyError("文件完成不能引用停止前或尚未可靠保存的结果页")
    saved, _ = _decode(page_event)
    activity = _source_fact(state, "device_activities", evidence["activity_id"])
    action = _source_fact(state, "actions", activity["action_id"])
    observer = _source_fact(state, "actions", facts["observer_action_id"])
    _verify_record_result_page(saved, page_event, action, activity, state)
    _verify_file_result_source(saved, page_event, action, activity, state, ResultSourceKind.STOP, stop_event.event_id)
    wait_ms = action["execution_spec_json"].get("file_completion_wait_ms")
    if not is_json_integer(wait_ms) or wait_ms <= 0:
        raise ConsistencyError("录像没有固定的停止后文件完成等待规则")
    stopped, _ = _stop_source_from_rows(stop_event, state)
    if stopped["id"] != activity["id"] or stopped["action_id"] != action["id"]:
        raise ConsistencyError("完成文件的原 STOP 与录像活动、动作不符")
    identity = parse_exact_json(facts["identity_key"])
    binding = _capture_binding(action)
    if (not isinstance(identity, list) or len(identity) != 3
            or identity[:2] != list(binding) or _capture_binding(observer) != binding
            or facts["source_action_id"] != action["id"]
            or (identity[2], facts["id"]) not in saved.file_ids):
        raise ConsistencyError("完成文件的原身份、来源或固定绑定与结果页不符")
    entries = [entry for entry in saved.page.entries if entry.identity == identity[2]]
    if len(entries) != 1:
        raise ConsistencyError("完成文件缺少唯一的原结果页观察")
    entry, = entries
    if (not entry.complete or entry.size_bytes is None or not json_equal(entry.size_bytes, size)
            or not json_equal(entry.evidence, evidence["observation"])
            or not json_equal(entry.locator, facts["locator_json"])):
        raise ConsistencyError("完成文件与原页的完整观察、长度或定位不符")
    settlement = saved.page.outcome.settlement
    completion = None if settlement is None else settlement.evidence.data.get("file_completion")
    if not is_completed_file_wait(completion, activity_id=str(activity["id"]),
            stop_result_event_id=stop_event.event_id, required_wait_ms=wait_ms):
        raise ConsistencyError("完成文件的原页缺少匹配且满足要求的实际等待")
    data = settlement.evidence.data
    source = None
    source_id = data.get("file_completion_source_page_event_id")
    if source_id is not None:
        ObjectId(source_id)
        source_event = _history_source_event(state, source_id)
        original, _ = _decode(source_event)
        _verify_record_result_page(original, source_event, action, activity, state)
        _verify_file_result_source(original, source_event, action, activity, state, ResultSourceKind.STOP, stop_event.event_id)
        original_settlement = original.page.outcome.settlement
        source = {"event_id": source_event.event_id, "activity_id": original.ref.ticket.target_id,
                  "data": None if original_settlement is None else original_settlement.evidence.data}
    try:
        completed_file_wait_source(data, page_event_id=page_event.event_id,
            activity_id=str(activity["id"]), stop_result_event_id=stop_event.event_id,
            required_wait_ms=wait_ms, source_page=source)
    except ValueError as error:
        raise ConsistencyError("文件完成复用的等待没有匹配的原实际等待页") from error


def _verify_natural_wait_file(facts, evidence, size, state, *, event_id=None, occurred_at=None):
    """自然完成引用原发送、实际等待和文件页；不借用其他活动的完成事实。"""
    from .result_pages import _decode
    from .result_sources import ResultSourceKind
    from camctl.devices.capture_completion import validate_completion_context
    from camctl.devices.tasks import CompletionMode, EndControl, StartReturn
    from camctl.history.events import load_event_registry

    fields = {"basis", "observation", "activity_id", "wait_completed_event_id",
              "result_page_event_id", "start_result_event_id"}
    if not isinstance(evidence, Mapping) or set(evidence) != fields:
        raise ConsistencyError("原生文件完成缺少原发送、等待或结果页的完整引用")
    activity = _source_fact(state, "device_activities", evidence["activity_id"])
    action = _source_fact(state, "actions", activity["action_id"])
    spec = action["execution_spec_json"]
    if (action["type"] != int(_ACTION_TYPE.CAMERA_TIMELAPSE)
            or "file_completion_wait_ms" not in spec
            or not spec["wait_after_send"] or spec["start_return_meaning"] != int(StartReturn.SENT)
            or spec["end_control"] != int(EndControl.DEVICE)
            or spec["completion_mode"] != int(CompletionMode.TIME_AND_OUTPUTS)):
        raise ConsistencyError("原生文件完成与原有限设备任务不符")
    page_event = _history_source_event(state, evidence["result_page_event_id"])
    start_event = _history_source_event(state, evidence["start_result_event_id"])
    wait_event = _history_source_event(state, evidence["wait_completed_event_id"])
    if (not start_event.event_id < wait_event.event_id < page_event.event_id
            or (event_id is not None and page_event.event_id >= event_id)
            or page_event.occurred_at < wait_event.occurred_at
            or (occurred_at is not None and page_event.occurred_at > occurred_at)):
        raise ConsistencyError("自然文件完成的原事件时序不符")
    start_rows = [row for row in start_event.rows if row.table == "operation_attempts"]
    if start_event.event_type != _ATTEMPT_RESULT_EVENT or len(start_rows) != 1:
        raise ConsistencyError("自然文件完成没有原实际发送结果")
    original, = start_rows
    attempt = _source_fact(state, "operation_attempts", original.row_id)
    run = _source_fact(state, "operation_runs", attempt["run_id"])
    if (attempt["result_event_id"] != start_event.event_id
            or run["kind"] != int(_RUN_KIND.START) or run["action_id"] != action["id"]
            or run["activity_id"] != activity["id"]
            or run["responsibility_key"] != f"start/{action['id']}"
            or start_event.occurred_at != activity["sent_at"]
            or any(not json_equal(original.after.values.get(name), attempt[name])
                for name in ("status", "effect_state", "result_json", "error_json"))):
        raise ConsistencyError("自然文件完成的原发送与活动、任务或实际结果不符")
    wait_branch = load_event_registry()["events"]["CAPTURE_WAIT_CHANGED"]
    wait_rows = [row for row in wait_event.rows if row.table == "device_activities"]
    if (wait_event.event_type != wait_branch["id"]
            or wait_event.reason != wait_branch["branches"]["COMPLETE"]["reason"]
            or len(wait_rows) != 1 or wait_rows[0].row_id != activity["id"]
            or wait_rows[0].after.values.get("wait_completed_event_id") != wait_event.event_id
            or activity["wait_completed_event_id"] != wait_event.event_id
            or activity["result_wait_margin_ms"] != spec["result_wait_margin_ms"]):
        raise ConsistencyError("自然文件完成没有同活动、同规则的实际等待完成事件")
    validate_completion_context({"kind": "timelapse_natural", "activity_id": str(activity["id"]),
        "start_result_event_id": start_event.event_id,
        "start_response": {"status": attempt["status"], "effect_state": attempt["effect_state"],
            "result": attempt["result_json"], "error": attempt["error_json"]},
        "sent_at_us": activity["sent_at"], "wait_completed_event_id": wait_event.event_id,
        "wait_completed_at_us": wait_event.occurred_at,
        "target_duration_ms": spec["target_duration_ms"],
        "result_wait_margin_ms": activity["result_wait_margin_ms"],
        "extra_wait_ms": activity["extra_wait_ms_used"],
        "expected_check_at_us": activity["expected_check_at"],
        **({"startup_margin_ms": spec["startup_margin_ms"]} if "startup_margin_ms" in spec else {})}, activity_id=str(activity["id"]))
    saved, _ = _decode(page_event)
    _verify_record_result_page(saved, page_event, action, activity, state)
    _verify_file_result_source(saved, page_event, action, activity, state, ResultSourceKind.NATURAL)
    identity = parse_exact_json(facts["identity_key"])
    observer = _source_fact(state, "actions", facts["observer_action_id"])
    if (not isinstance(identity, list) or len(identity) != 3
            or identity[:2] != list(_capture_binding(action))
            or _capture_binding(observer) != _capture_binding(action)
            or facts["source_action_id"] != action["id"]
            or (identity[2], facts["id"]) not in saved.file_ids):
        raise ConsistencyError("自然文件完成的原身份、来源或固定绑定与原页不符")
    entries = [entry for entry in saved.page.entries if entry.identity == identity[2]]
    if (len(entries) != 1 or not entries[0].complete
            or not json_equal(entries[0].size_bytes, size)
            or not json_equal(entries[0].evidence, evidence["observation"])
            or not json_equal(entries[0].locator, facts["locator_json"])):
        raise ConsistencyError("自然文件完成改变了原页观察、长度或定位")


def _native_wait_file(facts, state):
    action = _source_fact(state, "actions", facts["source_action_id"] or facts["observer_action_id"])
    return (action["type"] == int(_ACTION_TYPE.CAMERA_TIMELAPSE)
            and "file_completion_wait_ms" in action["execution_spec_json"])


def _wait_record_file(facts, state, basis):
    """已归属或最初观察者的固定定义识别需单独等待的录像文件。"""
    action_id = facts["source_action_id"] or facts["observer_action_id"]
    action = _source_fact(state, "actions", action_id)
    return ("file_completion_wait_ms" in action["execution_spec_json"]
            and (action["type"] == int(_ACTION_TYPE.CAMERA_RECORD)
                or (action["type"] == int(_ACTION_TYPE.CAMERA_TIMELAPSE)
                    and basis != int(_COMPLETION_BASIS.TIME_AND_OUTPUTS))))


def _current_file_facts(context, row) -> Mapping[str, Any]:
    facts = context.state_rows.get("device_files", {}).get(row.row_id)
    if facts is None:
        raise EventValidationError(f"设备文件事件缺少当前事实: device_files#{row.row_id}")
    return facts


def _completion_request_guard(event, row, facts) -> None:
    """新事件保留完整原输入，投影仍只记录实际改变的元信息列。"""
    if "completion_request" not in event.evidence:
        # 旧事件只提供实际变化，仍按既有结构读取及回放。
        return
    request = event.evidence["completion_request"]
    if not isinstance(request, Mapping) or set(request) != {"locator", "original_name", "media_type"}:
        raise EventValidationError("文件形成状态的原元信息申请成员不完整")
    for name, column in (("locator", "locator_json"), ("original_name", "original_name"),
                         ("media_type", "media_type")):
        value = request[name]
        if value is not None:
            if name == "locator":
                valid = isinstance(value, Mapping)
            else:
                valid = isinstance(value, str) and bool(value)
            if not valid:
                raise EventValidationError("文件形成状态的原元信息申请类型无效")
        changed = value is not None and not json_equal(value, facts[column])
        if (changed != (column in row.after.values)
                or (changed and not json_equal(row.after.values[column], value))):
            raise EventValidationError("文件形成状态的原元信息申请与实际变化不一致")


def _device_file_guard(event, context) -> None:
    """DEVICE_FILE_OBSERVED 五分支：身份、来源、配对与完成事实核对。

    身份键编码观察者的固定绑定；来源只能从未知一次确认，预览配对
    两端属于同一来源任务；形成状态按登记转换推进且已完成不倒退；
    摘要与支持声明、存在性变化各自满足登记约束。
    """
    if event.event_type != _DEVICE_FILE_EVENT:
        return
    for row in event.rows:
        if row.table != "device_files":
            continue
        _device_file_row_guard(event, row, context)


def _photo_source_file_guard(context, event, file_id, observation, *, role=None, sha256=None,
                             check_sha=False, size_bytes=None, check_size=False):
    """照片设备文件从原连接核实 START 和实际角色页。"""
    reads = context.photo_result_reads
    if reads is None:
        if isinstance(observation, Mapping) and ("photo_source" in observation or "photo_start" in observation):
            raise EventValidationError("照片来源事实缺少原写连接")
        return
    try:
        reads.verify_device_file(file_id, observation, before_event_id=event.event_id,
                                 role=role, sha256=sha256, check_sha=check_sha,
                                 size_bytes=size_bytes, check_size=check_size)
    except (ConsistencyError, ValueError, KeyError, TypeError) as error:
        raise EventValidationError("照片设备文件缺少一致的原角色来源") from error


def _device_file_row_guard(event, row, context) -> None:
    reason = event.reason
    if reason == _FILE_CREATE_REASON:
        if row.before.exists:
            raise EventValidationError("文件发现必须是创建行")
        values = row.after.values
        action = context.state_rows.get("actions", {}).get(
            values.get("observer_action_id"))
        if action is None:
            raise EventValidationError("文件发现缺少观察者动作事实")
        device, driver = _capture_binding(action)
        key = values.get("identity_key")
        try:
            decoded = parse_exact_json(key)
        except ValueError as error:
            raise EventValidationError(f"文件身份键不是有效精确 JSON: {key!r}") from error
        if (not isinstance(decoded, list) or len(decoded) != 3
                or decoded[0] != device or decoded[1] != driver
                or not isinstance(decoded[2], str) or not decoded[2]):
            raise EventValidationError("文件身份键与观察者的设备或驱动绑定不一致")
        if not isinstance(values.get("locator_json"), dict):
            raise EventValidationError("文件定位结构必须是对象")
        for column in ("role", "presence_state", "completion_state", "checksum_support"):
            if values.get(column) != 1:
                raise EventValidationError(f"新发现文件的 {column} 必须是未知初始值")
        for column in ("source_action_id", "ownership_evidence_json",
                       "original_device_file_id", "pairing_evidence_json",
                       "completion_evidence_json", "size_bytes", "sha256",
                       "last_error_json"):
            if values.get(column) is not None:
                raise EventValidationError(f"新发现文件不能携带 {column}")
        return

    if not row.before.exists:
        raise EventValidationError("文件归属、形成状态、摘要及存在性必须是更新行")
    if reason == _FILE_OWNERSHIP_REASON:
        before, after = row.before.values, row.after.values
        error = ownership_claim_error(row.row_id, before, after)
        if error is not None:
            raise EventValidationError(error)
        source, role = after.get("source_action_id"), after.get("role")
        evidence = after.get("ownership_evidence_json")
        paired = after.get("original_device_file_id")
        if role == int(_FILE_ROLE.PREVIEW):
            original = context.state_rows.get("device_files", {}).get(paired)
            if original is None:
                raise EventValidationError("预览配对缺少原片当前事实")
            error = ownership_pair_error(original, source)
            if error is not None:
                raise EventValidationError(error)
        _photo_source_file_guard(context, event, row.row_id, evidence.get("observation"), role=role)
        return

    facts = _current_file_facts(context, row)
    after = row.after.values
    if reason == _FILE_COMPLETE_REASON:
        _completion_request_guard(event, row, facts)
        current = facts.get("completion_state")
        state = after.get("completion_state")
        if state not in _FILE_COMPLETION_NEXT.get(current, frozenset()):
            raise EventValidationError(
                f"文件形成状态不能从 {current!r} 推进到 {state!r}")
        observed = {**facts, **after}
        if state == int(_FILE_COMPLETION.COMPLETE):
            size = observed.get("size_bytes")
            evidence = observed.get("completion_evidence_json")
            error = completion_facts_error(size, evidence)
            if error is not None:
                raise EventValidationError(error)
            _photo_source_file_guard(context, event, row.row_id, evidence.get("observation"),
                                     role=facts.get("role"), size_bytes=size, check_size=True)
            error = completion_wait_reference_error(evidence)
            if error is not None:
                raise EventValidationError(error)
            try:
                if (evidence["basis"] == int(_COMPLETION_BASIS.TIME_AND_OUTPUTS)
                        and ("result_page_event_id" in evidence or _native_wait_file(facts, context.state_rows))):
                    _verify_natural_wait_file(observed, evidence, size, context.state_rows, event_id=event.event_id, occurred_at=event.occurred_at)
                elif evidence["basis"] == int(_COMPLETION_BASIS.STOP_RETURN_AND_WAIT):
                    _verify_stop_wait_file(observed, evidence, size, context.state_rows, event_id=event.event_id)
                elif (evidence["basis"] != int(_COMPLETION_BASIS.STOP_RETURN_AND_WAIT)
                        and _wait_record_file(facts, context.state_rows, evidence["basis"])):
                    raise ConsistencyError("停止后等待的录像文件首次完成必须使用原停止与等待依据")
            except (ConsistencyError, ValueError, KeyError, TypeError) as error:
                raise EventValidationError("文件完成的原停止与等待依据不一致") from error
        else:
            error = incomplete_facts_error(state, observed)
            if error is not None:
                raise EventValidationError(error)
        return

    if reason == _FILE_CHECKSUM_REASON:
        support = after.get("checksum_support")
        error = checksum_decision_error(facts, support)
        if error is not None:
            raise EventValidationError(error)
        sha256 = after.get("sha256")
        completion = facts.get("completion_evidence_json")
        _photo_source_file_guard(context, event, row.row_id,
            completion.get("observation") if isinstance(completion, Mapping) else None,
            role=facts.get("role"), sha256=sha256, check_sha=True)
        error = checksum_value_error(facts, support, sha256)
        if error is not None:
            raise EventValidationError(error)
        return

    if reason == _FILE_PRESENCE_REASON:
        state = after.get("presence_state")
        if state not in (int(member) for member in _FILE_PRESENCE) or state == facts.get(
                "presence_state"):
            raise EventValidationError("存在性观察必须是实际状态变化")
        return
    raise EventValidationError(f"未登记的设备文件观察分支: {reason!r}")


def _emergency_guard(event, context) -> None:
    """应急补记的组合守卫：责任键、尝试归属、次数与结果组合。

    会话身份由本事件创建的最终流程行承载；停止成功的补记必须具
    有可靠停止证据——零尝试或依据来自尝试结果之外时经事件依据成
    员保存，其余保存在实际尝试结果的观察中；两种依据都须指向目
    标活动。停止依据不伴随其他结果分类。
    """
    run_values = None
    run_id = None
    attempts: list[dict] = []
    for row in event.rows:
        if row.table == "operation_runs" and not row.before.exists:
            if run_values is not None:
                raise EventValidationError("应急补记只共同创建一个最终流程")
            run_values = row.after.values
            run_id = row.row_id
        elif row.table == "operation_attempts" and not row.before.exists:
            attempts.append(row.after.values)
    if run_values is None:
        raise EventValidationError("应急补记缺少最终流程行")
    error = emergency_identity_error(run_values)
    if error is not None:
        raise EventValidationError(error)
    _check_config_numbers(run_values, f"operation_runs#{run_id}")
    error = emergency_run_status_error(run_values, attempts)
    if error is not None:
        raise EventValidationError(error)
    status = run_values.get("status")
    if status in (4, 6):
        error = run_values["error_json"]
        try:
            validate_public_error(error)
        except SchemaRuleError:
            raise
        except ValueError as cause:
            raise EventValidationError("应急流程错误不符合公共契约") from cause
        failure = emergency_failure_error(run_values)
        if failure is not None:
            raise EventValidationError(failure)
    error = emergency_attempt_presence_error(run_values, attempts)
    if error is not None:
        raise EventValidationError(error)
    error = emergency_observation_error(run_values, attempts, event.evidence.get("observation"))
    if error is not None:
        raise EventValidationError(error)
    activity_id = run_values.get("activity_id")
    activity = (
        dict(context.state_rows.get("device_activities", {}).get(activity_id, {}))
        if context is not None else {}
    )
    for row in event.rows:
        if row.table == "device_activities" and row.row_id == activity_id:
            if not row.after.exists:
                raise EventValidationError("应急补记不能删除目标活动")
            activity.update(row.before.values)
            activity.update(row.after.values)
    error = emergency_target_error(run_values, activity)
    if error is not None:
        raise EventValidationError(error)
    error = emergency_attempt_count_error(run_values, attempts)
    if error is not None:
        raise EventValidationError(error)
    for attempt in attempts:
        _check_config_numbers(attempt, "应急尝试")
        error = emergency_attempt_error(run_values, run_id, event.event_id, attempt)
        if error is not None:
            raise EventValidationError(error)


def _validate_activity_result(row, context) -> None:
    """核实活动实际 after，省略的变化列沿用可靠原值。"""
    facts = (
        dict(context.state_rows.get("device_activities", {}).get(row.row_id, {}))
        if row.before.exists else {})
    facts.update(row.before.values)
    facts.update(row.after.values)
    try:
        missing = {"capture_json", "last_error_json"} - facts.keys()
        if missing:
            raise ValueError(f"活动缺少可靠结果字段: {sorted(missing)}")
        if facts["capture_json"] is not None:
            validate_capture_result(facts["capture_json"])
        if facts["last_error_json"] is not None:
            validate_public_error(facts["last_error_json"])
    except SchemaRuleError:
        raise
    except ValueError as error:
        raise EventValidationError(
            f"设备活动#{row.row_id}的采集结果或错误无效: {error}") from error


def _activity_guard(event, context) -> None:
    """活动状态守卫：结束不由路径、超时或本地退出补造。

    采集判定列只适用于照片与延时：录像活动不得携带采集结果或判
    定依据；确定判定只能从待定初始化一次并同事务携带依据。
    """
    for row in event.rows:
        if row.table != "device_activities" or not row.after.exists:
            continue
        _validate_activity_result(row, context)
        if not row.before.exists:
            action = context.state_rows.get("actions", {}).get(row.after.values.get("action_id"), {})
            from .file_batch_reads import FileBatchReads
            if (type(context.file_batch_reads) is not FileBatchReads
                    or not context.file_batch_reads.permits(action["id"], action["device_id"])):
                raise EventValidationError("新拍摄活动不能越过同机文件批次")
            from .scheduling import _fixed_identity
            from camctl.devices.drivers.hasselblad.identity import CameraIdentity
            raw_identity = row.after.values.get("expected_identity_json")
            try:
                identity = None if raw_identity is None else CameraIdentity(**raw_identity)
                expected, resource_key = _fixed_identity(action, identity)
                if (not json_equal(raw_identity, expected)
                        or row.after.values.get("resource_key") != resource_key):
                    raise ValueError("活动固定机身身份与资源键不一致")
            except (TypeError, ValueError, KeyError) as error:
                raise EventValidationError("活动固定机身身份无效") from error
            from .preparation import PreparationReads
            if type(context.preparation_reads) is not PreparationReads:
                raise EventValidationError("活动创建缺少内核绑定的设备检查核对")
            if context.preparation_reads.availability_in_flight(action["device_id"], identity):
                raise EventValidationError("活动创建越过了尚未收场的设备检查")
            if identity is not None:
                from .physical_alias import physical_alias_conflict
                if physical_alias_conflict(context.preparation_reads.connection, action["device_id"], identity):
                    raise EventValidationError("活动越过了其他逻辑设备的机身限制")
            if action.get("execution_started") == 0:
                from .preparation import PreparationReads
                if type(context.preparation_reads) is not PreparationReads:
                    raise EventValidationError("pending活动创建缺少内核绑定的原申请核对")
                try:
                    context.preparation_reads.verify_create(event, context, row)
                except (ValueError, TypeError, KeyError) as error:
                    raise EventValidationError(str(error)) from error
            continue
        before_state = row.before.values.get("activity_state")
        after_state = row.after.values.get("activity_state")
        if 'control_elapsed_ns' in row.after.values:
            observation=event.evidence.get('observation',{})
            result_id=observation.get('stop_result_event_id')
            if (row.before.values.get('control_elapsed_ns') is not None or after_state!=3
                    or before_state==3 or context.current_reads is None
                    or not is_json_integer(result_id)
                    or not context.transaction.first_event_id<=result_id<event.event_id):
                raise EventValidationError('连续控制时长必须随原STOP首次结束共同保存，不能后补')
            original=context.current_reads.transaction_event(result_id,context.transaction)
            attempts=context.state_rows.get('operation_attempts',{})
            if (original.event_type!=_ATTEMPT_RESULT_EVENT or original.occurred_at!=event.occurred_at
                    or not any(change.table=='operation_attempts' and
                        attempts.get(change.row_id,{}).get('result_event_id')==result_id
                        for change in original.rows)):
                raise EventValidationError('连续控制时长没有同组原STOP的实际结果和时刻')
        if after_state == 3 and before_state != 3:
            # 实际停止或原结果页的完成观察承载设备结束；本地退出
            # 和目录扫描结束均不构成该依据。
            stopped = any(
                row.table == "operation_runs"
                and not row.before.exists
                and row.after.values.get("status") == 3
                for row in event.rows
            )
            runs = context.state_rows.get("operation_runs", {})
            stopped = stopped or any(
                values.get("status") == 3 for values in runs.values()
            )
            observation = event.evidence.get("observation")
            completed = False
            if isinstance(observation, Mapping) and "result_page_event_id" in observation:
                completed = result_page_confirms_completion(observation, row.row_id, event.event_id)
                if not completed:
                    raise EventValidationError("结果页完成观察不属于原活动或可靠先前页")
            if isinstance(observation, Mapping) and "start_result_event_id" in observation:
                facts = context.state_rows.get("device_activities", {}).get(row.row_id, {})
                completed = start_result_confirms_completion(
                    observation, facts, row.row_id, event.event_id, runs.values(),
                    context.state_rows.get("operation_attempts", {}).values())
                if not completed:
                    raise EventValidationError("完成返回缺少原活动的可靠成功 START 结果")
            if isinstance(observation, Mapping) and "stop_result_event_id" in observation:
                facts = context.state_rows.get("device_activities", {}).get(row.row_id, {})
                action = context.state_rows.get("actions", {}).get(facts.get("action_id"), {})
                result_id, elapsed = observation["stop_result_event_id"], observation.get("control_elapsed_ns")
                try:
                    HostTimerStopSave(elapsed)
                except ValueError as error:
                    raise EventValidationError("主机停止的控制时长非法") from error
                completed = stop_result_confirms_completion(
                    observation, facts, action, row.row_id, event.event_id, row.after.values,
                    runs.values(), context.state_rows.get("operation_attempts", {}).values())
                if not completed:
                    raise EventValidationError("拍摄停止观察缺少原 STOP 确认或一致控制时长")
            if not stopped and not completed:
                raise EventValidationError("活动结束缺少可靠停止或设备完成事实")
        basis_before = row.before.values.get("completion_basis")
        capture_columns = (
            "capture_json", "completion_basis", "completion_evidence_json")
        if any(row.after.values.get(column) is not None
               for column in capture_columns):
            action_id = row.before.values.get(
                "action_id", context.state_rows.get(
                    "device_activities", {}).get(row.row_id, {}).get("action_id"))
            action = context.state_rows.get("actions", {}).get(action_id)
            error = activity_capture_action_error(action_id, action)
            if error is not None:
                raise EventValidationError(error)
        error = activity_capture_transition_error(basis_before, row.after.values)
        if error is not None:
            raise EventValidationError(error)


def _release_guard(event, context) -> None:
    """占用释放守卫：统一释放判定与输出范围限制。

    ENDED 仍可保持 HELD；保存 RELEASED 要求活动已结束、可靠未派
    发或无效果拒绝、适用的等待与产物完成依据三者居一，且基准比
    较的输出范围归属已经固定。
    """
    for row in event.rows:
        if row.table != "device_activities" or not row.before.exists:
            continue
        if not (
            row.after.values.get("occupancy_state") == 2
            and row.before.values.get("occupancy_state") == 1
        ):
            continue
        facts = dict(
            context.state_rows.get("device_activities", {})
            .get(row.row_id, {})
        )
        facts.update(row.after.values)
        action = context.state_rows.get("actions", {}).get(facts.get("action_id"), {})
        known_preparation = action.get("execution_started") == 0 or any(
            run.get("kind") == int(_RUN_KIND.PREPARE_CAPTURE) and run.get("activity_id") == row.row_id
            for run in context.state_rows.get("operation_runs", {}).values())
        if known_preparation and context.preparation_reads is None:
            raise EventValidationError("准备释放缺少内核绑定的原责任核对")
        if context.preparation_reads is not None:
            from .preparation import PreparationReads
            if type(context.preparation_reads) is not PreparationReads:
                raise EventValidationError("准备释放缺少内核绑定的原连接核对")
            if not context.preparation_reads.calls_resolved(row.row_id):
                raise EventValidationError("设置准备调用或原结果尚未收场，不能释放占用")
        if not release_basis_holds(facts):
            raise EventValidationError(
                "占用释放缺少活动结束、未派发或完成依据")
        action = context.state_rows.get("actions", {}).get(facts.get("action_id"), {})
        if "preparation_resolved" in event.evidence and event.evidence["preparation_resolved"] is not True:
            raise EventValidationError("准备收场依据必须是实际可靠的 true")
        if "preparation_error" in event.evidence and event.evidence.get("preparation_resolved") is not True:
            raise EventValidationError("实际准备错误必须与准备收场共同保存")
        unstarted = unstarted_release_allowed(facts, action, event.evidence.get("preparation_resolved"))
        from camctl.capture.software_recovery import recovery_state
        recovery = next((recovery_state(item) for item in context.state_rows.get("software_recoveries", {}).values()
                         if item["activity_id"] == row.row_id), None)
        if recovery is not None:
            if recovery.get("phase") != "ready" or context.current_reads is None:
                raise EventValidationError("软件恢复尚未完成准备及范围责任，不能释放相机")
            context.current_reads.verify_software_recovery_release(facts, recovery)
        if (facts.get("ownership_mode") == 2 and not unstarted
                and not output_scope_resolved(facts, recovery)):
            raise EventValidationError("原输出范围的归属及集合未确定不得释放占用")


def _result_check_guard(event, context) -> None:
    """结果集合核实守卫：分支、判定编码与轮次状态一致。

    一轮只消耗一次：开始分支只推进状态，结论分支必须携带规则标
    识与结构化依据，判定编码与分支对应；取消或耗尽不补造集合。
    """
    if event.event_type != _RESULT_SET_EVENT:
        return
    rows = [
        row for row in event.rows
        if row.table == "device_activities" and row.before.exists
    ]
    if len(rows) != 1:
        raise EventValidationError("结果集合核实恰好修改一个设备活动")
    row = rows[0]
    if event.reason == _RESULT_OUTPUTS_FINALIZED_REASON:
        from camctl.capture.result_inputs import page_from_outcome, saved_outcome

        if (set(row.before.values) != {"output_set_finalized_event_id"}
                or row.before.values["output_set_finalized_event_id"] is not None
                or not json_equal(row.after.values, {"output_set_finalized_event_id": event.event_id})
                or set(event.evidence) != {"attempt_id", "result_page_event_id"}):
            raise EventValidationError("独立集合事实只能首次保存本事件引用及原尝试末页")
        attempt_id, page_id = event.evidence["attempt_id"], event.evidence["result_page_event_id"]
        if (not is_json_integer(attempt_id) or not is_json_integer(page_id)
                or not 0 < page_id < event.event_id or attempt_id <= 0):
            raise EventValidationError("独立集合事实缺少可靠原尝试或先前页引用")
        attempt = context.state_rows.get("operation_attempts", {}).get(attempt_id, {})
        run = context.state_rows.get("operation_runs", {}).get(attempt.get("run_id"), {})
        activity = context.state_rows.get("device_activities", {}).get(row.row_id, {})
        if (attempt.get("status") != int(_ATTEMPT_STATUS.SUCCEEDED)
                or attempt.get("error_json") is not None
                or attempt.get("result_last_page_event_id") != page_id
                or run.get("kind") not in (int(_RUN_KIND.CHECK_CAPTURE_RESULTS),
                                            int(_RUN_KIND.RESOLVE_OUTPUT_SCOPE))
                or run.get("activity_id") != row.row_id or run.get("action_id") != activity.get("action_id")):
            raise EventValidationError("独立集合事实不属于原活动的可靠成功扫描末页")
        try:
            ticket = AttemptTicket(attempt["attempt_no"], "result", str(row.row_id),
                run["responsibility_key"], run["id"])
            outcome = saved_outcome(attempt["status"], attempt["effect_state"],
                attempt["result_json"], attempt["error_json"])
            observation = next(item for item in outcome.observations
                               if item.type == "result_files_listed" and item.version == 2)
            from camctl.devices.directory import DirectoryCursor
            cursor = observation.data["cursor"]
            page = page_from_outcome(ticket, outcome,
                cursor=None if cursor is None else DirectoryCursor.from_json(cursor))
        except (ValueError, KeyError, StopIteration) as error:
            raise EventValidationError("独立集合事实缺少合法的原 v2 扫描结果") from error
        if page.next_cursor is not None or not page.set_finalized:
            raise EventValidationError("独立集合事实要求扫描结束且原集合确定")
        from .result_pages import verify_output_scope_ownership
        action = context.state_rows.get("actions", {}).get(run["action_id"])
        if action is None:
            raise EventValidationError("集合范围核实缺少原动作")
        try:
            verify_output_scope_ownership(context.current_reads, action, ticket, page_id)
        except (ConsistencyError, ValueError) as error:
            raise EventValidationError(f"集合范围归属核实无效: {error}") from error
        return
    state_before = row.before.values.get("result_set_state")
    if event.reason == _RESULT_BEGIN_REASON:
        error = result_begin_error(row.before.values, row.after.values)
        if error is not None:
            raise EventValidationError(error)
        return
    error = result_conclusion_error(event.reason, state_before, row.after.values)
    if error is not None:
        raise EventValidationError(error)
    check = row.after.values.get("result_check_json")
    capture = row.after.values.get("capture_json")
    if capture is not None:
        phase = next(phase for phase, target in _RESULT_PHASE_TARGETS.items()
                     if target[0] == event.reason)
        try:
            validate_capture_result(capture, phase=phase)
        except SchemaRuleError:
            raise
        except ValueError as error:
            raise EventValidationError(f"新采集结果与集合结论不符: {error}") from error
    facts = context.state_rows.get('device_activities', {}).get(row.row_id, {})
    proof = row.after.values.get('completion_evidence_json', facts.get('completion_evidence_json'))
    if (event.reason == _RESULT_COMPLETE_REASON and isinstance(proof, Mapping)
            and proof.get('method') == _TIME_AND_OUTPUTS_METHOD):
        from .result_sources import natural_result_source
        from .result_pages import capture_result_scope
        try:
            action = _source_fact(context.state_rows, 'actions', facts['action_id'])
            source = natural_result_source(action, facts, context.state_rows)
            observation = check['observation']
            last_id = observation.get('last_page_event_id')
            if source is None:
                if last_id is not None:
                    raise ConsistencyError('自然集合缺少原结果请求')
                return
            if event.occurred_at < source.natural_lower_bound:
                raise ConsistencyError('自然集合事实早于原可靠来源')
            if last_id is not None:
                attempt = next(value for value in context.state_rows['operation_attempts'].values()
                    if value['intent_event_id'] == source.intent_event_id)
                run = _source_fact(context.state_rows, 'operation_runs', attempt['run_id'])
                ticket = AttemptTicket(attempt['attempt_no'], 'result', str(row.row_id),
                    run['responsibility_key'], run['id'])
                reader = context.current_reads
                if reader is None or reader.scope != capture_result_scope(action['id'], ticket, last_id):
                    raise ConsistencyError('自然集合缺少原有界结果页读取')
                cursor, first, last = None, None, None
                while True:
                    batch = reader.result_pages(ticket, cursor, 1)
                    for page in batch.items:
                        if (page.ref.event_id >= event.event_id
                                or page.occurred_at < source.natural_lower_bound
                                or page.occurred_at > event.occurred_at):
                            raise ConsistencyError('自然集合包含不合格的原实际页时刻')
                        first = page.ref.event_id if first is None else first
                        last = page
                    if batch.next_cursor is None:
                        break
                    cursor = batch.next_cursor
                if (last is None or first != observation.get('first_page_event_id')
                        or last.ref.event_id != last_id or not last.page.scan_complete
                        or not last.page.set_finalized or last.page.outcome.error is not None):
                    raise ConsistencyError('自然集合缺少原请求的可靠完整扫描')
        except (ConsistencyError, ValueError, KeyError, StopIteration) as error:
            raise EventValidationError(f'自然集合的原结果来源无效: {error}') from error
