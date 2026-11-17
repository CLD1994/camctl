"""业务存储的事件规则适配与登记。"""

from __future__ import annotations

from collections import ChainMap
from decimal import Decimal
from types import SimpleNamespace
from typing import Mapping
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import is_json_integer, json_equal
from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.history.events import load_event_registry
from camctl.history.validators import EventValidationError
from camctl.operations.attempts import AttemptTarget, OperationKind, QueryPurpose, seconds_from_json, retry_wait_violation, may_resolve_output_scope, may_prepare_capture_attempt, operation_responsibility_key
from camctl.operations.models import SettlementBasis
from camctl.operations.ownership_rules import require_obtain_check_action, require_obtain_check_output, require_obtain_check_carrier, require_obtain_check_config, require_host_read_slot, require_device_read_slot, read_round_matches, require_run_activity_owner, require_copy_read_owner, start_camera_matches, binding_read_finish_matches
from camctl.operations.result_format import read_result_document
from camctl.operations.validation import ResultErrorIssue, result_error_issue, confirmed_effect_has_observation, not_dispatched_has_no_effect, run_finish_error_issue, preparation_result_is_confirmed
from camctl.scheduling.resources import baseline_start_reason
from camctl.persistence.atomic import TransactionError
from camctl.persistence.repositories.transactions import row_facts
from .binding_read_cancel import binding_cancel_event as _binding_cancel_event


_RUN_KIND = enum_for("operation_runs.kind")


_RUN_STATUS = enum_for("operation_runs.status")


_QUERY_PURPOSE = enum_for("operation_runs.query_purpose")


_ATTEMPT_STATUS = enum_for("operation_attempts.status")


_EFFECT_STATE = enum_for("operation_attempts.effect_state")


_BASES = frozenset({basis.value for basis in SettlementBasis})


_ATTEMPT_RESULT_EVENT = 12


_RETRY_WAIT_EVENT = 6


_OPERATION_CONFIGURED_EVENT = 10


_RESUME_READ_REASON = 4


_PURPOSE_FLOW_KIND = {
    QueryPurpose.START_CONFIRMATION: _RUN_KIND.START,
    QueryPurpose.STOP_CONFIRMATION: _RUN_KIND.STOP,
    QueryPurpose.RESIDUAL_STOP_CONFIRMATION: _RUN_KIND.STOP_RESIDUAL,
}


def _fail(message: str) -> None:
    raise EventValidationError(message)


def _guard_facts(context, table: str, row_id: int) -> dict[str, Any]:
    facts = dict(context.state_rows.get(table, {}).get(row_id, {}))
    facts.setdefault("id", row_id)
    return facts


def _row_after_facts(context, row: RowChange) -> dict[str, Any]:
    facts = _guard_facts(context, row.table, row.row_id)
    facts.update(row.after.values)
    return facts


def _fixed_reference(state, table: str, identity: int) -> Mapping[str, Any]:
    """目标必须真实存在且身份一致；不读取或解释可变状态。"""
    try:
        ObjectId(identity)
    except ValueError as error:
        raise ConsistencyError(f"{table} 的关联身份无效") from error
    facts = state.get(table, {}).get(identity)
    # 行图以物理 ID 为键；新建事件的业务列不重复保存派生 ID。
    if facts is None or not json_equal(facts.get("id", identity), identity):
        raise ConsistencyError(f"固定关联缺少 {table}#{identity} 的事实")
    return facts


def _obtain_check_is_host(state, item_id: int, action_id: int) -> bool:
    """仅核固定归属和唯一载体；未选择的显式 item 也沿 requested output 核对。"""
    item = _fixed_reference(state, "obtain_items", item_id)
    selection = _fixed_reference(state, "obtain_source_selections", item.get("selection_id"))
    dependency = _fixed_reference(state, "action_dependencies", selection.get("dependency_id"))
    action = _fixed_reference(state, "actions", dependency.get("action_id"))
    require_obtain_check_action(dependency, action, action_id)
    output = _fixed_reference(state, "outputs", item.get("requested_output_id"))
    device, local = require_obtain_check_output(item, dependency, output)
    carrier = _fixed_reference(state, "device_files" if device is not None else "intermediate_files",
                               device if device is not None else local)
    require_obtain_check_carrier(output, carrier, device=device is not None)
    return local is not None


def _verify_obtain_config(run, state) -> None:
    if run.get("obtain_item_id") is None:
        return
    host = _obtain_check_is_host(state, run["obtain_item_id"], run["action_id"])
    timeout, interval = run.get("timeout_s_json"), run.get("retry_interval_s_json")
    require_obtain_check_config(host_source=host, timeout=timeout, interval=interval)


def _verify_complete_obtain_result(attempt) -> None:
    if (attempt is None or attempt.get("status") == int(_ATTEMPT_STATUS.RUNNING)
            or attempt.get("result_event_id") is None or attempt.get("result_json") is None):
        raise ConsistencyError("obtain CHECK 后续意图缺少上一票的完整可靠结果")
    try:
        ObjectId(attempt["result_event_id"])
        read_result_document(attempt["status"], attempt["effect_state"],
                             attempt["result_json"], attempt["error_json"])
    except (KeyError, ValueError, TypeError) as error:
        raise ConsistencyError("obtain CHECK 上一票的实际结果不可解释") from error


def _copy_source(copy) -> tuple[str, int]:
    try:
        device, local = copy["source_device_file_id"], copy["source_intermediate_file_id"]
        if (device is None) == (local is None):
            raise ValueError("拷贝必须恰有一种来源")
        return ("device_files", ObjectId(device)) if device is not None else ("intermediate_files", ObjectId(local))
    except (KeyError, ValueError) as error:
        raise ConsistencyError("拷贝缺少有效的唯一源身份") from error


def _read_attempt_has_slot(state, copy_id: int, copy_round: int) -> bool:
    """当前源、机会和轮次合法时，判断设备拷贝是否已取得机会。"""
    copy = _fixed_reference(state, "file_copies", copy_id)
    table, identity = _copy_source(copy)
    source = _fixed_reference(state, table, identity)
    try:
        slot = copy["slot_device_id"]
        if table == "intermediate_files":
            require_host_read_slot(slot)
        else:
            observer = _fixed_reference(state, "actions", source["observer_action_id"])
            origin = _fixed_reference(state, "actions", source["source_action_id"])
            require_device_read_slot(slot, observer, origin)
        current_round = copy["round"]
    except KeyError as error:
        raise ConsistencyError("当前读取源、绑定或拷贝缺少必要字段") from error
    if not read_round_matches(current_round, copy_round):
        raise TransactionError("读取意图的轮次与当前拷贝不符")
    return table == "intermediate_files" or slot is not None


def _verify_run_identity(run: Mapping[str, Any], state) -> None:
    """核对普通流程的固定列及目标归属，供写事件和只读入口共用。"""
    try:
        if not is_json_integer(run["kind"]):
            raise ValueError("流程种类必须是整数编号")
        kind = OperationKind[_RUN_KIND(run["kind"]).name]
        purpose_code = run["query_purpose"]
        if purpose_code is not None and not is_json_integer(purpose_code):
            raise ValueError("查询用途必须是整数编号")
        purpose = QueryPurpose[_QUERY_PURPOSE(purpose_code).name] if purpose_code is not None else None
        target = AttemptTarget(activity_id=run["activity_id"], copy_id=run["copy_id"],
                               cleanup_item_id=run["cleanup_item_id"], obtain_item_id=run["obtain_item_id"],
                               availability_check_id=run["availability_check_id"])
        expected = operation_responsibility_key(kind, run["action_id"], target, purpose,
                                                session_key=run["session_key"])
        delivery_id, session_key = run["delivery_id"], run["session_key"]
    except (KeyError, ValueError) as error:
        raise ConsistencyError("普通流程的固定身份字段无效或缺失") from error
    if run.get("responsibility_key") != expected:
        raise ConsistencyError("流程责任键与固定身份不符")
    if ((kind is not OperationKind.PREPARE_CAPTURE and session_key is not None)
            or (kind is not OperationKind.READ_FILE and delivery_id is not None)):
        raise ConsistencyError("普通流程的会话或不适用交付引用必须为空")
    if kind is OperationKind.PREPARE_CAPTURE and "max_attempts_used" in run and (
            run.get("max_attempts_used") != 1 or run.get("timeout_s_json") is None
            or run.get("retry_interval_s_json") is not None or run.get("attempts_used", 0) > 1):
        raise ConsistencyError("设置准备流程必须保存单次有超时且无重试的实际配置")
    action_id = run["action_id"]
    if target.activity_id is not None:
        activity = _fixed_reference(state, "device_activities", target.activity_id)
        try:
            ObjectId(activity["action_id"])
        except (KeyError, ValueError) as error:
            raise ConsistencyError("原活动缺少有效所属动作") from error
        require_run_activity_owner(kind, purpose, action_id, activity["action_id"])
        if purpose in _PURPOSE_FLOW_KIND:
            original_kind = int(_PURPOSE_FLOW_KIND[purpose])
            matches = [(identity, facts) for identity, facts in state.get("operation_runs", {}).items()
                       if facts.get("kind") == original_kind
                       and json_equal(facts.get("action_id"), action_id)
                       and json_equal(facts.get("activity_id"), target.activity_id)]
            if len(matches) != 1:
                raise ConsistencyError("查询确认必须关联唯一的原流程")
            original_id, original = matches[0]
            _fixed_reference(state, "operation_runs", original_id)
            _verify_run_identity(original, state)
    elif target.copy_id is not None:
        copy = _fixed_reference(state, "file_copies", target.copy_id)
        try:
            copy_delivery, copy_processing, copy_inspection = (
                copy["delivery_id"], copy["processing_id"], copy.get("inspection_id"))
        except KeyError as error:
            raise ConsistencyError("拷贝缺少完整的交付与处理关联字段") from error
        if sum(value is not None for value in
               (copy_delivery, copy_processing, copy_inspection)) != 1:
            raise ConsistencyError("拷贝必须恰归属交付、录像处理或分段检查之一")
        if not json_equal(delivery_id, copy_delivery):
            raise ConsistencyError("读取流程与拷贝的交付引用不符")
        parent = (_fixed_reference(state, "deliveries", copy_delivery)
                  if copy_delivery is not None else
                  _fixed_reference(state, "recording_processing", copy_processing)
                  if copy_processing is not None else
                  _fixed_reference(state, "segment_inspections", copy_inspection))
        require_copy_read_owner(action_id, parent)
        matches = [identity for identity, facts in state.get("operation_runs", {}).items()
                   if facts.get("kind") == run["kind"] and json_equal(facts.get("copy_id"), target.copy_id)]
        if len(matches) != 1 or not json_equal(matches[0], run.get("id")):
            raise ConsistencyError("拷贝必须关联其唯一原读取流程")
        _fixed_reference(state, "operation_runs", matches[0])
    elif target.availability_check_id is not None:
        check = _fixed_reference(state, "device_availability_checks", target.availability_check_id)
        if not json_equal(check.get("action_id"), action_id):
            raise ConsistencyError("设备检查与触发动作不符")
    elif target.obtain_item_id is not None:
        _obtain_check_is_host(state, target.obtain_item_id, action_id)
    elif target.cleanup_item_id is not None:
        cleanup = _fixed_reference(state, "cleanup_items", target.cleanup_item_id)
        if not json_equal(cleanup.get("action_id"), action_id):
            raise ConsistencyError("流程与目标清理项所属动作不符")


def _operation_identity_guard(event, context) -> None:
    if context.obtain_check_reads is not None:
        from .obtain_check_reads import _ObtainCheckReads
        if type(context.obtain_check_reads) is not _ObtainCheckReads:
            _fail('取回 CHECK 缺少内核绑定的实际读取端口')
        try:
            context.obtain_check_reads.verify(event, context.transaction)
        except ConsistencyError as error:
            raise EventValidationError(str(error)) from error
    relations = context.association_rows
    if context.transaction_rows is None:
        # 独立校验也必须包含当前创建行；不预取未来事件或改写事件前状态。
        current_rows: dict[str, dict[int, dict[str, Any]]] = {}
        for row in event.rows:
            if row.after.exists:
                facts = _row_after_facts(context, row)
                current_rows.setdefault(row.table, {})[row.row_id] = facts
        relations = ChainMap({
            table: ChainMap(rows, relations.get(table, {}))
            for table, rows in current_rows.items()
        }, relations)
    for row in event.rows:
        if row.table == "operation_runs":
            facts = _row_after_facts(context, row)
        elif row.table == "file_copies" and not row.before.exists:
            matches = [(identity, facts) for identity, facts in relations.get("operation_runs", {}).items()
                       if facts.get("kind") == int(_RUN_KIND.READ_FILE)
                       and json_equal(facts.get("copy_id"), row.row_id)]
            if len(matches) != 1:
                _fail("新拷贝必须关联唯一读取流程")
            identity, saved_facts = matches[0]
            facts = dict(saved_facts)
            facts.setdefault("id", identity)
        else:
            continue
        try:
            _verify_run_identity(facts, relations)
        except ConsistencyError as error:
            raise EventValidationError(str(error)) from error


def _query_configuration_guard(event, context) -> None:
    for row in event.rows:
        if row.table == "operation_runs":
            facts = _row_after_facts(context, row)
            kind = facts.get("kind")
            if kind in (
                int(_RUN_KIND.QUERY_ACTIVITY),
                int(_RUN_KIND.CHECK_CAPTURE_RESULTS),
                int(_RUN_KIND.RESOLVE_OUTPUT_SCOPE),
            ):
                if (
                    facts.get("timeout_s_json") is None
                    or facts.get("retry_interval_s_json") is None
                ):
                    _fail("查询与结果核实流程必须保存完整的超时与间隔配置")
            try:
                _verify_obtain_config(facts, context.association_rows)
            except ConsistencyError as error:
                raise EventValidationError(str(error)) from error
            _check_config_numbers(facts, f"operation_runs#{row.row_id}")
        elif row.table == "operation_attempts":
            _check_config_numbers(
                row.after.values, f"operation_attempts#{row.row_id}"
            )


def _check_config_numbers(values: Mapping[str, Any], where: str) -> None:
    maximum = values.get("max_attempts_used")
    if maximum is not None and (
        isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1
    ):
        _fail(f"{where} 的次数上限必须是正整数: {maximum!r}")
    timeout = values.get("timeout_s_json")
    if timeout is not None:
        try:
            number = seconds_from_json(timeout)
        except ValueError as cause:
            raise EventValidationError(f"{where} 的 timeout_s 不是有效秒数") from cause
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, Decimal))
                or number is None or not number.is_finite() or number <= 0):
            _fail(f"{where} 的 timeout_s 必须为正秒数: {timeout!r}")
    interval = values.get("retry_interval_s_json")
    if interval is not None:
        try:
            number = seconds_from_json(interval)
        except ValueError as cause:
            raise EventValidationError(f"{where} 的 retry_interval_s 不是有效秒数") from cause
        if (isinstance(interval, bool) or not isinstance(interval, (int, Decimal))
                or number is None or not number.is_finite() or number < 0):
            _fail(f"{where} 的 retry_interval_s 必须为非负秒数: {interval!r}")


def _attempt_intent_guard(event, context) -> None:
    attempts = [
        row
        for row in event.rows
        if row.table == "operation_attempts"
    ]
    run_rows = [row for row in event.rows if row.table == "operation_runs"]
    if len(attempts) != 1 or attempts[0].before.exists or len(run_rows) != 1:
        _fail("尝试意图必须包含一条新尝试及其唯一流程")
    for attempt in attempts:
        after = attempt.after.values
        if after.get("run_id") != run_rows[0].row_id:
            _fail("尝试意图必须属于同事件的流程")
        if after.get("intent_event_id") != event.event_id:
            _fail("普通尝试的意图引用不可省略，且指向本次事件")
        if (
            after.get("result_event_id") is not None
            or after.get("result_json") is not None
            or after.get("error_json") is not None
        ):
            _fail("意图阶段的尝试不得携带结束结果")
        if (
            after.get("status") != int(_ATTEMPT_STATUS.RUNNING)
            or after.get("effect_state") != int(_EFFECT_STATE.UNKNOWN)
        ):
            _fail("意图阶段的尝试必须为运行中且效果未知")
    run_row = run_rows[0]
    run_after = _row_after_facts(context, run_row)
    from .file_batch_reads import FileBatchReads
    batch_reads = context.file_batch_reads
    action = _guard_facts(context, "actions", run_after["action_id"])
    if (type(batch_reads) is not FileBatchReads
            or not batch_reads.operation_permitted(action, run_after)):
        _fail("设备尝试没有取得所属同机文件批次的执行资格")
    if run_after.get("kind") == int(_RUN_KIND.CHECK_DEVICE_AVAILABILITY):
        from .availability_intent import AvailabilityIntentReads
        reads = context.availability_reads
        if type(reads) is not AvailabilityIntentReads:
            _fail("设备检查意图缺少独立设备资源守卫")
        try:
            reads.verify(event, context, run_row.row_id)
        except (ConsistencyError, ValueError, KeyError) as error:
            raise EventValidationError(f"设备检查意图资格无效: {error}") from error
    attempt_no = attempts[0].after.values["attempt_no"]
    if run_after.get("attempts_used") != attempt_no:
        _fail(
            f"累计次数 {run_after.get('attempts_used')!r} 与新尝试编号"
            f" {attempt_no!r} 不一致"
        )
    if run_row.before.exists:
        before_used = run_row.before.values.get("attempts_used")
        if before_used is None or int(before_used) != int(attempt_no) - 1:
            _fail("新尝试编号必须紧接原累计次数")
    elif attempt_no != 1:
        _fail("首次建立的流程必须从第 1 次尝试开始")
    kind = run_after.get("kind")
    if kind == int(_RUN_KIND.RESOLVE_OUTPUT_SCOPE):
        activity = _guard_facts(context, "device_activities", run_after.get("activity_id"))
        if not may_resolve_output_scope(activity):
            _fail("范围收尾意图要求原活动已结束、占用仍保留、基准固定且范围未确定")
    if kind==int(_RUN_KIND.CHECK_CAPTURE_RESULTS):
        if context.current_reads is None:
            _fail('RESULTS意图缺少原连接的固定集合核验')
        context.current_reads.verify_result_run_open(run_row.row_id,run_after['action_id'])
    if run_after.get("obtain_item_id") is not None:
        if context.obtain_check_reads is None:
            _fail('取回 CHECK 意图缺少原内核的实际读取端口')
        for attempt in attempts:
            if any(not json_equal(attempt.after.values.get(column), run_after.get(column))
                   for column in ("max_attempts_used", "timeout_s_json", "retry_interval_s_json")):
                _fail("obtain CHECK 意图与流程的实际采用配置不符")
        if run_row.before.exists and _obtain_check_is_host(
                context.association_rows, run_after["obtain_item_id"], run_after["action_id"]):
            previous = [facts for facts in context.state_rows.get("operation_attempts", {}).values()
                        if facts.get("run_id") == run_row.row_id
                        and facts.get("attempt_no") == attempt_no - 1]
            if len(previous) != 1:
                _fail("obtain CHECK 后续意图缺少唯一上一票")
            try:
                _verify_complete_obtain_result(previous[0])
            except ConsistencyError as error:
                raise EventValidationError(str(error)) from error
    if kind == int(_RUN_KIND.PREPARE_CAPTURE):
        activity = _guard_facts(context, "device_activities", run_after.get("activity_id"))
        action = _guard_facts(context, "actions", run_after.get("action_id"))
        from .preparation import PreparationReads
        eligible = (type(context.preparation_reads) is PreparationReads
                    and context.preparation_reads.can_prepare(action, activity, event.occurred_at)
                    and context.preparation_reads.calls_resolved(activity["id"]))
        if not may_prepare_capture_attempt(action, activity, eligible=eligible, attempt_no=attempt_no,
                max_attempts=run_after.get("max_attempts_used"), timeout=run_after.get("timeout_s_json"),
                retry_interval=run_after.get("retry_interval_s_json")):
            _fail("设置准备意图要求原pending动作的占用及单次配置")
    if kind == int(_RUN_KIND.START):
        activity = _guard_facts(context, "device_activities", run_after.get("activity_id"))
        action = _guard_facts(context, "actions", run_after.get("action_id"))
        from .preparation import PreparationReads
        if (type(context.preparation_reads) is not PreparationReads
                or context.preparation_reads.device_read_in_flight(action["device_id"])):
            _fail("启动意图缺少同设备读取收场依据")
        from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
        from camctl.devices.drivers.hasselblad.identity import CameraIdentity
        identity = event.evidence.get("start_camera_identity")
        fixed_identity = None
        if action.get("driver_id") == DRIVER_ID:
            try:
                original = CameraIdentity(**activity["expected_identity_json"])
                current = CameraIdentity(**identity)
            except (TypeError, ValueError, KeyError) as error:
                raise EventValidationError("启动意图缺少可靠机身身份") from error
            if not start_camera_matches(original, current, activity.get("resource_key")):
                _fail("启动意图与原固定机身身份不符")
            fixed_identity = original
            from .physical_alias import physical_alias_conflict
            if physical_alias_conflict(context.preparation_reads.connection, action["device_id"], original):
                _fail("启动意图越过了其他逻辑设备的机身限制")
        elif identity is not None:
            _fail("其他驱动不能携带 Hasselblad 机身身份")
        if context.preparation_reads.availability_in_flight(action["device_id"], fixed_identity):
            _fail("启动意图越过了尚未收场的设备检查")
        try:
            preparation_reason = baseline_start_reason(activity, action)
        except ConsistencyError as error:
            raise EventValidationError(str(error)) from error
        if preparation_reason is not None:
            _fail(f"启动意图的基准准备未完成: {preparation_reason}")
    copy_round = attempts[0].after.values.get("copy_round")
    if kind == int(_RUN_KIND.READ_FILE):
        from .outputs import _read_owner_eligible, _slot_owner_action
        copy = _fixed_reference(context.state_rows, "file_copies", run_after.get("copy_id"))
        action = _slot_owner_action(context, copy)
        try:
            if action.get("id") != run_after.get("action_id") or not _read_owner_eligible(action):
                _fail("读取意图要求拷贝所属动作执行中且未取消")
        except ConsistencyError as error:
            raise EventValidationError(str(error)) from error
        if attempt_no > 1 and run_row.before.values.get("retry_wait_required") == 0:
            from .retry_wait_reads import RetryWaitReads
            reads = context.retry_wait_reads
            if type(reads) is not RetryWaitReads or not reads.completed(run_row.row_id, attempt_no - 1):
                _fail("后续读取意图缺少本次重试间隔已完成的事实")
        if (
            not isinstance(copy_round, int)
            or isinstance(copy_round, bool)
            or copy_round < 1
        ):
            _fail("读取尝试必须保存正的拷贝轮次")
        try:
            held = _read_attempt_has_slot(context.state_rows, run_after.get("copy_id"), copy_round)
        except (ConsistencyError, TransactionError) as error:
            raise EventValidationError(str(error)) from error
        if not held:
            _fail("设备读取意图要求当前拷贝已取得原来源设备的读取机会")
        slot = copy["slot_device_id"]
        if slot is not None:
            from .preparation import PreparationReads
            if (type(context.preparation_reads) is not PreparationReads
                    or context.preparation_reads.device_start_in_flight(slot)):
                _fail("读取意图缺少同设备启动收场依据")
            source = _fixed_reference(context.state_rows, "device_files", copy["source_device_file_id"])
            observer = _fixed_reference(context.state_rows, "actions", source["observer_action_id"])
            from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
            fixed_identity = None
            if observer["driver_id"] == DRIVER_ID:
                from .physical_alias import source_physical_alias_conflict, source_physical_identity
                origin = _fixed_reference(context.state_rows, "actions", source["source_action_id"])
                if source_physical_alias_conflict(context.preparation_reads.connection, source, observer, origin):
                    _fail("读取意图越过了其他逻辑设备的机身责任")
                fixed_identity = source_physical_identity(context.preparation_reads.connection, source, observer, origin)
                from .capture_selection import CaptureSelectionInput, read_capture_priority
                try:
                    selection = CaptureSelectionInput.from_document(event.evidence.get("capture_selection"))
                    selected = read_capture_priority(context.preparation_reads.connection, selection,
                        context.transaction.first_event_id - 1, device_id=slot, identity=fixed_identity,
                        occurred_at=event.occurred_at)
                except (ConsistencyError, ValueError) as error:
                    raise EventValidationError(f"读取意图缺少原拍摄选择资格: {error}") from error
                if selected is not None:
                    _fail("读取意图越过了已选中的拍摄")
            if context.preparation_reads.availability_in_flight(slot, fixed_identity):
                _fail("读取意图越过了尚未收场的设备检查")
            if observer["driver_id"] == DRIVER_ID:
                from camctl.scheduling.resources import current_preparation_holder
                if (current_preparation_holder(context.preparation_reads.connection, slot) is not None
                        or context.preparation_reads.capture_holds_device(slot)):
                    _fail("读取意图越过了已有拍摄或准备持有者")
    elif copy_round is not None:
        _fail("只有读取尝试保存拷贝轮次")


def _attempt_result_guard(event, context) -> None:
    for row in event.rows:
        if row.table != "operation_attempts" or not row.before.exists:
            continue
        after = _row_after_facts(context, row)
        if after.get("result_event_id") != event.event_id:
            _fail("结果引用必须指向本次事件")
        status = after.get("status")
        error = after.get("error_json")
        result = after.get("result_json")
        error_issue = result_error_issue(status, error, stored=True)
        if error_issue is ResultErrorIssue.SUCCESS_ERROR:
            _fail("成功尝试不得携带调用错误")
        if error_issue is ResultErrorIssue.MISSING_ERROR:
            _fail("失败或未知尝试必须携带调用错误")
        if not isinstance(result, Mapping):
            _fail("结束尝试必须保存统一结构的结果正文")
        assert result is not None
        if result.get("format_version") != 1:
            _fail("结果正文版本不支持")
        settlement = result.get("settlement")
        if not isinstance(settlement, Mapping) or settlement.get("basis") not in _BASES:
            _fail("结果正文缺少合法的收场依据")
        evidence = settlement.get("evidence")
        if (
            not isinstance(evidence, Mapping)
            or not isinstance(evidence.get("type"), str)
            or not evidence.get("type")
            or isinstance(evidence.get("version"), bool)
            or not isinstance(evidence.get("version"), int)
            or evidence.get("version") < 1
        ):
            _fail("收场依据必须是类型化对象")
        observations = result.get("observations")
        if not isinstance(observations, list):
            _fail("结果正文必须保存观察数组")
        run = _guard_facts(context, "operation_runs", after.get("run_id"))
        if "normal_recording_stop" in event.evidence:
            from .normal_recording_stop import verify_normal_stop_group
            try:
                verify_normal_stop_group(event, context, after, run)
            except (ConsistencyError, ValueError, KeyError, TypeError) as error:
                raise EventValidationError(f"正常录像停止来源无效: {error}") from error
        if (run.get("kind") == int(_RUN_KIND.CHECK_DEVICE_AVAILABILITY)
                and status == int(_ATTEMPT_STATUS.SUCCEEDED)):
            from .availability_release import require_result_group
            try:
                require_result_group(event, context)
            except ConsistencyError as error:
                raise EventValidationError(str(error)) from error
            from .availability_observation import require_saved_availability
            check = _guard_facts(context, "device_availability_checks", run.get("availability_check_id"))
            try:
                require_saved_availability(after, run, check)
            except (KeyError, TypeError, ValueError) as error:
                raise EventValidationError(f"设备可用性结果无效: {error}") from error
        if run.get("kind") == int(_RUN_KIND.PREPARE_CAPTURE) and status == int(_ATTEMPT_STATUS.SUCCEEDED):
            if not preparation_result_is_confirmed(after.get("effect_state"), observations, run["activity_id"]):
                _fail("设置准备成功必须保存原组合完成观察")
        effect = after.get("effect_state")
        if not not_dispatched_has_no_effect(settlement["basis"], effect, stored=True):
            _fail("未派发依据只支持无效果")
        if not confirmed_effect_has_observation(effect, observations, stored=True):
            _fail("已确认效果必须有承载观察")
        if "diagnostics" in result:
            from camctl.operations.tool_diagnostics import decode_diagnostics
            try:
                decode_diagnostics(result["diagnostics"])
            except (ValueError, TypeError) as error:
                _fail(f"内部工具诊断格式无效: {error}")
        call_info = result.get("call_info")
        if call_info is not None and not isinstance(call_info, Mapping):
            _fail("调用信息必须是对象")


def _read_resume_guard(event, context) -> None:
    """恢复配置事件守卫：只属于读取流程的在途尝试。"""
    if event.event_type != _ATTEMPT_RESULT_EVENT or event.reason != _RESUME_READ_REASON:
        return
    for row in event.rows:
        if row.table != "operation_attempts":
            continue
        attempt = context.state_rows.get("operation_attempts", {}).get(row.row_id)
        if attempt is None:
            _fail("恢复配置缺少当前尝试事实")
        run = context.state_rows.get("operation_runs", {}).get(attempt["run_id"])
        if run is None or run.get("kind") != int(_RUN_KIND.READ_FILE):
            _fail("恢复配置只属于读取流程的尝试")


def _operation_finish_guard(event, context) -> None:
    for row in event.rows:
        if row.table != "operation_runs" or not row.before.exists:
            continue
        if "status" not in row.after.values:
            continue
        after = _row_after_facts(context, row)
        status = after.get("status")
        if "binding_read_finish" in event.evidence:
            from .preparation import PreparationReads
            reads = context.preparation_reads
            if type(reads) is not PreparationReads or after.get("kind") != int(_RUN_KIND.READ_FILE):
                _fail("绑定读取结果缺少原读取与事务事实")
            action = row_facts(reads.connection, "actions", after["action_id"])
            if action is None or action["cancel_requested"] not in (0, 1):
                _fail("绑定读取结果缺少原动作取消事实")
            previous = reads.events
            if (len(previous) != 1 or previous[0].event_type != _ATTEMPT_RESULT_EVENT
                    or len(previous[0].rows) != 1
                    or previous[0].rows[0].after.values.get("status") != int(_ATTEMPT_STATUS.SUCCEEDED)):
                _fail("绑定读取结束缺少同事务真实成功结果")
            attempt = context.state_rows.get("operation_attempts", {}).get(previous[0].rows[0].row_id)
            if attempt is None or attempt.get("run_id") != row.row_id:
                _fail("绑定读取结果不属于原流程")
            error = after.get("error_json")
            binding_evidence = event.evidence["binding_read_finish"]
            if not isinstance(binding_evidence, Mapping) or set(binding_evidence) != {"error", "cancel_event", "current_identity"}:
                _fail("绑定读取结束证据不完整")
            binding_error = binding_evidence["error"]
            if not isinstance(binding_error, Mapping) or binding_error.get("code") != "device_binding_unavailable":
                _fail("绑定读取结束缺少原绑定失败")
            copy = row_facts(reads.connection, "file_copies", after.get("copy_id"))
            if copy is None:
                _fail("绑定读取结果缺少原拷贝")
            details = binding_error.get("details")
            identity = binding_evidence["current_identity"]
            if isinstance(details, Mapping) and details.get("reason") == "physical_identity_mismatch":
                from .physical_alias import verify_source_identity_mismatch
                source = row_facts(reads.connection, "device_files", copy.get("source_device_file_id"))
                observer = None if source is None else row_facts(reads.connection, "actions", source["observer_action_id"])
                origin = None if source is None else row_facts(reads.connection, "actions", source["source_action_id"])
                if (source is None or observer is None or origin is None or identity is None
                        or not verify_source_identity_mismatch(reads.connection, source, observer, origin, identity,
                                                               action_id=after["action_id"])):
                    _fail("读取结果的原次机身身份与固定来源不构成不符")
            elif identity is not None:
                _fail("非机身不符的读取结果不能携带当前机身身份")
            if copy.get("source_sha256") is not None:
                from .read_end import load_read_end
                photo_cache = copy.get("source_device_file_id") is None and copy.get("source_intermediate_file_id") is not None
                if photo_cache:
                    from camctl.persistence.repositories.photo_cache_reuse import verify_photo_cache_binding_failure
                    source_id = verify_photo_cache_binding_failure(reads.connection, copy, before_event_id=event.event_id)
                    source = row_facts(reads.connection, "device_files", source_id)
                    observer = None if source is None else row_facts(reads.connection, "actions", source["observer_action_id"])
                    if (observer is None or not isinstance(details, Mapping)
                            or (details.get("device_id"), details.get("expected_driver_id"))
                                != (observer["device_id"], observer["driver_id"])):
                        _fail("照片缓存绑定失败与原设备身份不同")
                if (not isinstance(details, Mapping)
                        or details.get("reason") not in (("missing", "mismatch") if photo_cache else ("missing",))
                        or copy.get("verification_state") != int(enum_for("file_copies.verification_state").MISMATCHED)
                        or copy.get("target_sha256") is None
                        or copy["source_sha256"] == copy["target_sha256"]
                        or load_read_end(copy["id"], SimpleNamespace(connection=reads.connection)) is None):
                    _fail("配置缺失收场缺少原读取结束及已保存的不一致摘要")
            if action["cancel_requested"]:
                if not binding_read_finish_matches(action["cancel_requested"], status, error, binding_error):
                    _fail("已取消动作的读取必须按取消结束")
                actual_cancel = _binding_cancel_event(reads.connection, after["action_id"], event.transaction_id - 1)
                if (not json_equal(binding_evidence["cancel_event"], actual_cancel)
                        or event.occurred_at != max(previous[0].occurred_at, actual_cancel["occurred_at"])):
                    _fail("读取取消结束缺少更早已提交的原取消事件")
            elif (not binding_read_finish_matches(action["cancel_requested"], status, error, binding_error)
                    or binding_evidence["cancel_event"] is not None
                    or event.occurred_at != previous[0].occurred_at):
                _fail("未取消动作的读取必须保存原绑定失败")
        if after.get("kind") == int(_RUN_KIND.CHECK_DEVICE_AVAILABILITY):
            from .availability_finish import AvailabilityFinishReads
            reads = context.availability_finish_reads
            if type(reads) is not AvailabilityFinishReads:
                _fail("设备检查终结缺少原调用收场核验")
            try:
                reads.require_settled(row.row_id)
                from .availability_release import ReleaseGroup
                if (status != int(_RUN_STATUS.SUCCEEDED) and not isinstance(context.availability_reads, ReleaseGroup)
                        and not reads.has_result_in_group(row.row_id)):
                    from .availability_wait_finish import verify_waiting_finish
                    verify_waiting_finish(reads.connection, event, context, row.row_id)
            except ConsistencyError as error:
                raise EventValidationError(str(error)) from error
        if (after.get("kind") == int(_RUN_KIND.CHECK_DEVICE_AVAILABILITY)
                and status == int(_RUN_STATUS.SUCCEEDED)):
            from .availability_release import ReleaseGroup, ResolvedGroup
            if not isinstance(context.availability_reads, (ReleaseGroup, ResolvedGroup)):
                _fail("设备检查成功必须与目标解除事务共同核验")
        error = after.get("error_json")
        error_issue = run_finish_error_issue(status, error)
        if error_issue is ResultErrorIssue.MISSING_ERROR:
            _fail("失败或未确认结束必须携带流程错误")
        if error_issue is ResultErrorIssue.UNEXPECTED_ERROR:
            _fail("该结束结果不携带流程错误")
        if after.get("retry_wait_required") != 0:
            _fail("结束流程必须清除重试等待责任")
        if (after.get("kind") == int(_RUN_KIND.READ_FILE) and status == int(_RUN_STATUS.CANCELED)
                and row.before.values.get("status") == int(_RUN_STATUS.PENDING)
                and after.get("attempts_used") == 0):
            copy = context.state_rows.get("file_copies", {}).get(after.get("copy_id"))
            if copy is not None and copy.get("inspection_id") is not None and copy.get("slot_device_id") is not None:
                from .retry_wait_reads import RetryWaitReads
                inspection = context.state_rows.get("segment_inspections", {}).get(copy["inspection_id"])
                action = context.state_rows.get("actions", {}).get(after.get("action_id"))
                result_copy = (None if context.transaction_rows is None else
                               context.transaction_rows.get("file_copies", {}).get(copy["id"]))
                reads = context.retry_wait_reads
                inspection_state = enum_for("segment_inspections.state")
                before_run = context.state_rows.get("operation_runs", {}).get(row.row_id)
                if (before_run is None or before_run.get("retry_wait_required") != 0
                        or before_run.get("error_json") is not None
                        or inspection is None or inspection.get("action_id") != after.get("action_id")
                        or inspection.get("state") not in (int(inspection_state.COMPLETED),
                            int(inspection_state.FAILED), int(inspection_state.UNCONFIRMED))
                        or action is None or type(action.get("cancel_requested")) is not int
                        or action.get("cancel_requested") != 1
                        or copy.get("slot_device_id") is None or result_copy is None
                        or result_copy.get("slot_device_id") is not None
                        or type(reads) is not RetryWaitReads or not reads.no_attempts(row.row_id)):
                    _fail("未开始的分段检查读取取消要求原动作取消、零尝试和同事务释放机会")
        if (after.get("kind") == int(_RUN_KIND.READ_FILE) and event.event_type == _OPERATION_CONFIGURED_EVENT
                and event.reason == 3 and isinstance(error, Mapping)
                and error.get("code") == "read_attempts_exhausted"
                and "stale_run_request" not in event.evidence):
            from .retry_wait_reads import RetryWaitReads
            reads = context.retry_wait_reads
            if type(reads) is not RetryWaitReads:
                _fail("读取耗尽缺少原事务事实")
            last = reads.last_attempt(row.row_id, after.get("attempts_used"))
            if last is not None:
                copy = context.state_rows.get("file_copies", {}).get(after.get("copy_id"))
                unknown = last[1] == int(_ATTEMPT_STATUS.UNKNOWN)
                if unknown and copy is not None:
                    protected = {"file_copies", "actions", "deliveries", "recording_processing",
                                 "segment_inspections", "device_files", "intermediate_files", "obtain_items"}
                    for previous in context.validated_events:
                        for change in previous.envelope.rows:
                            if (change.table in protected
                                    and (change.row_id in context.state_rows.get(change.table, {})
                                         or change.table == "obtain_items")):
                                _fail("未知读取耗尽不能越过同事务前序资格变化")
                            if change.table == "operation_attempts":
                                facts = context.state_rows.get("operation_attempts", {}).get(change.row_id, {})
                                if (change.before.values.get("run_id") == row.row_id
                                        or change.after.values.get("run_id") == row.row_id
                                        or facts.get("run_id") == row.row_id):
                                    _fail("未知读取耗尽不能越过同事务前序尝试变化")
                    from .outputs import _internal_input_requirement, _read_is_due, _read_owner_eligible, _slot_owner_action
                    from camctl.outputs.read_discovery import read_copy_dispatch_eligible
                    owner = _slot_owner_action(context, copy)
                    processing_id = copy.get("processing_id")
                    processing = context.state_rows.get("recording_processing", {}).get(processing_id)
                    if (owner.get("id") != after.get("action_id") or not _read_owner_eligible(owner)
                            or not _read_is_due(owner, event.occurred_at)
                            or not read_copy_dispatch_eligible(reads.connection, copy["id"])
                            or (copy.get("source_device_file_id") is not None
                                and not reads.unknown_exhaustion_eligible(row.row_id, event.occurred_at))
                            or (processing_id is not None and (processing is None
                                or _internal_input_requirement(processing) is not None))):
                        _fail("未知读取耗尽要求所属工作仍需要原副本")
                if (last[0] != event.evidence.get("attempt_id")
                        or (unknown and reads.recovered_unknown(row.row_id, after.get("attempts_used")) is None)
                        or (not unknown and last[1] != int(_ATTEMPT_STATUS.FAILED))
                        or after.get("attempts_used") < after.get("max_attempts_used")
                        or copy is None
                        or (not unknown and row.before.values.get("retry_wait_required") == 0
                            and not reads.completed(row.row_id, after.get("attempts_used")))
                        or (copy.get("source_device_file_id") is not None and copy.get("delivery_id") is not None
                            and not (reads.read_eligible(row.row_id, event.occurred_at, allow_unheld=True)
                                     if unknown else reads.read_eligible(row.row_id, event.occurred_at)))):
                    _fail("读取耗尽要求原失败已收场、预算耗尽且所属工作仍有资格")
            elif ("attempt_id" in event.evidence or not any(
                    attempt.get("run_id") == row.row_id
                    and attempt.get("attempt_no") == after.get("attempts_used")
                    and attempt.get("status") == int(_ATTEMPT_STATUS.FAILED)
                    and isinstance(attempt.get("result_event_id"), int)
                    and context.transaction.first_event_id <= attempt["result_event_id"] < event.event_id
                    for attempt in context.state_rows.get("operation_attempts", {}).values())):
                _fail("读取耗尽要求原失败已收场、预算耗尽且所属工作仍有资格")
        if after.get("kind") == int(_RUN_KIND.PREPARE_CAPTURE) and status == int(_RUN_STATUS.SUCCEEDED):
            completed = [attempt for attempt in context.state_rows.get("operation_attempts", {}).values()
                         if attempt.get("run_id") == row.row_id
                         and attempt.get("status") == int(_ATTEMPT_STATUS.SUCCEEDED)
                         and attempt.get("effect_state") == int(_EFFECT_STATE.CONFIRMED)
                         and attempt.get("result_event_id") is not None]
            if len(completed) != 1:
                _fail("设置准备成功流程缺少唯一已保存的成功组合尝试")


def _retry_guard(event, context) -> None:
    for row in event.rows:
        if row.table != "operation_runs":
            continue
        facts = _row_after_facts(context, row)
        complete = load_event_registry()["events"]["RETRY_WAIT_CHANGED"]["branches"]["COMPLETE"]["reason"]
        if (event.event_type == _RETRY_WAIT_EVENT and event.reason == complete
                and facts.get("kind") == int(_RUN_KIND.CHECK_DEVICE_AVAILABILITY)):
            from .availability_retry import AvailabilityRetryReads
            if type(context.availability_reads) is not AvailabilityRetryReads:
                _fail("检查等待完成缺少独立原尝试核验")
            try:
                context.availability_reads.verify(event, context, row.row_id)
            except (ConsistencyError, ValueError, KeyError) as error:
                raise EventValidationError(f"检查等待完成无效: {error}") from error
        if event.event_type == _RETRY_WAIT_EVENT and event.reason == complete:
            if facts.get("kind") == int(_RUN_KIND.READ_FILE):
                from .retry_wait_reads import RetryWaitReads
                reads = context.retry_wait_reads
                if type(reads) is not RetryWaitReads:
                    _fail("读取等待完成缺少原事务事实")
                last = reads.last_attempt(row.row_id, facts.get("attempts_used"))
                if (last is None or event.evidence.get("attempt_id") != last[0]
                        or not reads.read_eligible(row.row_id, event.occurred_at)):
                    _fail("读取等待完成要求原尝试已收场且所属工作仍有资格")
        if row.after.values.get("retry_wait_required") != 1:
            continue
        violation = retry_wait_violation(facts.get("status"), facts.get("retry_interval_s_json"))
        if violation is not None:
            _fail(violation)


def register_operation_guards(register) -> None:
    """注册操作事务的正式业务守卫（装配期调用，替换测试替身）。"""
    from .availability_create import register_availability_guard
    register_availability_guard(register)
    register("operation_identity", _operation_identity_guard)
    register("query_configuration", _query_configuration_guard)
    register("attempt_intent", _attempt_intent_guard)
    register("attempt_result", _attempt_result_guard)
    register("read_resume", _read_resume_guard)
    register("operation_finish", _operation_finish_guard)
    register("retry", _retry_guard)
