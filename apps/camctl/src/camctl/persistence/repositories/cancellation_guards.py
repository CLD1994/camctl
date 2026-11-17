"""业务存储的事件规则适配与登记。"""

from __future__ import annotations

from camctl.cancellation.models import CancelApplyMode, CancelOutcomeChoice, CancellationEffect, SelectionBasis
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, parse_object_id
from camctl.contracts.workflow_errors import registered_error
from camctl.history.validators import EventValidationError


_CANCEL_ACTION_TYPE = 6


_TARGETS_FIXED_EVENT = 4


_TARGETS_CANCEL_REASON = 3


_TARGETS_FAIL_REASON = 4


_ACTION_STATUS = enum_for("actions.status")


_DELIVERY_STATUS = enum_for("deliveries.status")


_WITHDRAWAL_STATUS = enum_for("deliveries.withdrawal_state")


_TARGET_PENDING, _TARGET_FIXED, _TARGET_FAILED = 1, 2, 3


_ACTION_TERMINAL = (3, 4, 5, 6)


_TARGET_ERROR_CODES = ("cancel_self_target", "cancel_target_not_found")


_CANCEL_CHANGED_EVENT = 25


_CANCEL_APPLY_REASON, _CANCEL_RESULT_REASON, _CANCEL_STOP_WAIT_REASON = 1, 2, 3


_ITEM_PENDING, _ITEM_RUNNING, _ITEM_SUCCEEDED, _ITEM_FAILED, _ITEM_CANCELED =     1, 2, 3, 4, 5


_OBTAIN_TARGET_TYPE = 4


def _cancel_targets_guard(event, context) -> None:
    """登记名 cancel：核对目标固定、取消生效与逐项结果的完整约束。"""
    if event.event_type == _CANCEL_CHANGED_EVENT:
        _guard_cancel_changed(event, context)
        return
    if event.event_type != _TARGETS_FIXED_EVENT:
        return
    action_rows = [row for row in event.rows if row.table == "actions"]
    if len(action_rows) != 1 or not action_rows[0].before.exists:
        raise EventValidationError("取消目标事件必须恰好更新一条动作行")
    action_id = action_rows[0].row_id
    action = context.state_rows.get("actions", {}).get(action_id)
    if action is None:
        raise EventValidationError("取消目标事件缺少动作当前事实")
    created = [row for row in event.rows
               if row.table == "cancel_items" and not row.before.exists]
    if event.reason == _TARGETS_CANCEL_REASON:
        if not created:
            raise EventValidationError("取消目标固定必须创建取消成员")
        direct: set[int] = set()
        linked: set[int] = set()
        for row in created:
            values = row.after.values
            if values.get("action_id") != action_id:
                raise EventValidationError("取消成员必须属于目标固定动作")
            if values.get("target_action_id") == action_id:
                raise EventValidationError("取消成员不能以取消动作自身为目标")
            if (values.get("status") != 1
                    or values.get("outcome") is not None
                    or values.get("error_code") is not None):
                raise EventValidationError("取消成员初始值必须是待处理且无结果")
            basis = values.get("selection_basis")
            effect = values.get("cancellation_effect")
            if basis not in (member.value for member in SelectionBasis):
                raise EventValidationError(f"取消成员依据非法: {basis!r}")
            if effect not in (CancellationEffect.NOT_APPLIED.value,
                              CancellationEffect.NOT_REQUIRED.value):
                raise EventValidationError(f"取消成员初始效果非法: {effect!r}")
            if basis == SelectionBasis.DIRECT.value:
                direct.add(values.get("target_action_id"))
            else:
                linked.add(values.get("target_action_id"))
        request = event.evidence.get("photo_entry_request")
        if request is not None:
            _guard_photo_entry_request(event, context, request, created, action_id)
        _validate_photo_preview_scope(action, [row.after.values for row in created], context,
            frozenset(block["action_id"] for block in request["blocks"]) if request is not None else frozenset())
        links = context.state_rows.get("auto_preview_links", {})
        for obtain_id in linked:
            for source_id in direct:
                if any(
                    link.get("obtain_action_id") == obtain_id
                    and link.get("source_action_id") == source_id
                    and link.get("is_valid") == 1
                    for link in links.values()
                ):
                    break
            else:
                raise EventValidationError(
                    f"联动成员缺少有效自动关联依据: {obtain_id!r}")
        return
    if event.reason == _TARGETS_FAIL_REASON:
        if created:
            raise EventValidationError("目标解析失败不创建取消成员")
        after = action_rows[0].after.values
        allowed = {
            registered_error(code)["action_error_id"]
            for code in _TARGET_ERROR_CODES
        }
        if after.get("error_code") not in allowed:
            raise EventValidationError("目标解析失败的错误码不在登记集合")
        return


def _guard_cancel_changed(event, context) -> None:
    """CANCEL_CHANGED：生效转换、动作标记一致与逐项结果约束。"""
    items = [row for row in event.rows if row.table == "cancel_items"]
    if not items:
        raise EventValidationError("取消变化事件必须携带取消成员行")
    for row in items:
        before_facts = context.state_rows.get("cancel_items", {}).get(row.row_id)
        if before_facts is None:
            raise EventValidationError("取消成员更新缺少当前事实")
        origin = context.state_rows.get("actions", {}).get(
            before_facts.get("action_id"))
        if (origin is None
                or origin.get("type") != _CANCEL_ACTION_TYPE
                or origin.get("target_selection_state") != _TARGET_FIXED):
            raise EventValidationError("取消成员推进要求已固定的取消动作")
        if row.after.values.get("action_id") not in (
                None, before_facts.get("action_id")):
            raise EventValidationError("取消成员归属保持不变")
    if event.reason == _CANCEL_APPLY_REASON:
        _guard_cancel_apply(event, context, items)
    elif event.reason == _CANCEL_RESULT_REASON:
        _guard_cancel_result(event, context, items)
    elif event.reason == _CANCEL_STOP_WAIT_REASON:
        for row in items:
            if row.after.values.get("status") != _ITEM_CANCELED:
                raise EventValidationError("结束等待的成员必须转为已取消")
    else:
        raise EventValidationError(f"取消变化分支不可解释: {event.reason!r}")


def _guard_cancel_apply(event, context, items) -> None:
    """APPLY：项进入处理中、效果一次确定、目标标记与项目标一致。"""
    if len(items) != 1:
        raise EventValidationError("取消生效必须处理唯一成员")
    member = items[0]
    item = context.state_rows.get("cancel_items", {}).get(member.row_id)
    if item is None:
        raise EventValidationError("取消生效缺少成员原事实")
    target_id = item["target_action_id"]
    target = context.state_rows.get("actions", {}).get(target_id)
    if target is None:
        raise EventValidationError("取消生效缺少目标原事实")
    from camctl.devices.start_entry import is_hasselblad_photo, entry_from_rows, EntryPhase
    if is_hasselblad_photo(target) and event.evidence.get("cancel_request", {}).get("mode") in ("pre_start", "with_stop"):
        activities = context.complete_rows("device_activities", "action_id", target_id)
        runs = context.complete_rows("operation_runs", "action_id", target_id)
        start = [row for row in runs.values() if row["responsibility_key"] == f"start/{target_id}"]
        attempts = []
        for run in start:
            attempts.extend(context.complete_rows("operation_attempts", "run_id", run["id"]).values())
        phase = entry_from_rows(target, next(iter(activities.values()), None), attempts)
        if event.evidence["cancel_request"]["mode"] == "with_stop" or phase is not EntryPhase.BLOCKED:
            raise EventValidationError("Hasselblad 取消缺少原 START 可靠未进入依据")
    if target["type"] == 8:
        if "cancel_request" in event.evidence:
            raise EventValidationError("电机取消沿用 motor_request，不重复申请身份")
    else:
        request = event.evidence.get("cancel_request")
        if (not isinstance(request, dict)
                or set(request) != {"item_id", "mode", "occurred_at"}
                or isinstance(request["item_id"], bool) or not isinstance(request["item_id"], int)
                or request["item_id"] != member.row_id
                or isinstance(request["occurred_at"], bool) or not isinstance(request["occurred_at"], int)
                or request["occurred_at"] != event.occurred_at):
            raise EventValidationError("取消生效必须携带原成员与时间的完整 cancel_request")
        if "motor_request" in event.evidence or "motor_permit" in event.evidence:
            raise EventValidationError("非电机取消不携带电机申请身份")
        try:
            mode = CancelApplyMode(request["mode"])
        except (TypeError, ValueError) as error:
            raise EventValidationError("取消生效的原模式非法") from error
        target_changes = [row for row in event.rows if row.table == "actions"]
        effect = member.after.values.get("cancellation_effect", item["cancellation_effect"])
        if mode in (CancelApplyMode.PRE_START, CancelApplyMode.WITH_STOP):
            if target["cancel_requested"] or len(target_changes) != 1:
                raise EventValidationError("首次取消要求未取消目标及唯一目标标记")
            change = target_changes[0]
            if change.row_id != target_id or effect != CancellationEffect.APPLIED.value:
                raise EventValidationError("首次取消的目标身份与成员效果不符")
            from camctl.cancellation.rules import cancel_status_after_apply
            try:
                status = cancel_status_after_apply(target, mode)
            except ValueError as error:
                raise EventValidationError(str(error)) from error
            expected = {"cancel_requested": 1}
            if status is not None:
                expected["status"] = status
            if change.after.values != expected:
                raise EventValidationError("原取消模式与目标状态转换不符")
        elif mode is CancelApplyMode.TERMINAL:
            if (target["status"] not in _ACTION_TERMINAL or target_changes
                    or effect != CancellationEffect.NOT_REQUIRED.value):
                raise EventValidationError("终态取消要求原终态保持且成员无需施加取消")
        elif (not target["cancel_requested"] or target_changes
              or effect != CancellationEffect.APPLIED.value):
            raise EventValidationError("复用取消要求原目标已取消且保持原标记")
    marked: set[int] = set()
    for row in items:
        values = row.after.values
        if values.get("status") != _ITEM_RUNNING:
            raise EventValidationError("取消生效把成员推进到处理中")
        effect = values.get("cancellation_effect")
        if effect is not None and effect not in (
                CancellationEffect.APPLIED.value,
                CancellationEffect.NOT_REQUIRED.value):
            raise EventValidationError(f"取消生效的效果非法: {effect!r}")
    for row in event.rows:
        if row.table != "actions":
            continue
        if row.after.values.get("cancel_requested") != 1 \
                or row.before.values.get("cancel_requested") != 0:
            raise EventValidationError("取消生效把目标标记置为已请求")
        after_status = row.after.values.get("status")
        if after_status not in (2, 6, None):
            raise EventValidationError(f"取消生效的目标状态非法: {after_status!r}")
        targets = {
            item_row.after.values.get("target_action_id")
            if not item_row.before.exists or item_row.before.values.get(
                "target_action_id") is None
            else item_row.before.values.get("target_action_id")
            for item_row in items}
        facts = context.state_rows.get("cancel_items", {}).get(row.row_id, {})
        if row.row_id not in targets and row.row_id not in {
                item.get("target_action_id")
                for item in context.state_rows.get(
                    "cancel_items", {}).values()}:
            raise EventValidationError("被标记目标必须属于本次取消成员")
        target = context.state_rows['actions'][row.row_id]
        if target['type'] == 8:
            from camctl.persistence.repositories.motor import _notice, _validate_combination
            from camctl.motor.models import MotorActionFacts, SendOutcome, SendPermit
            from camctl.motor.rules import owns_send_permit
            previous = context.complete_rows('motor_notifications','action_id',row.row_id)
            notification_rows = [change for change in event.rows if change.table == 'motor_notifications']
            if after_status != 6:
                raise EventValidationError('电机可靠未发送取消必须共同进入取消终态')
            if previous:
                if len(previous) != 1 or len(notification_rows) != 1:
                    raise EventValidationError('电机意图取消必须有唯一发送结论')
                notice_values = next(iter(previous.values()))
                permit_values = event.evidence.get('motor_permit',{})
                try:
                    permit = SendPermit(**permit_values)
                except TypeError as error:
                    raise EventValidationError('电机取消缺少发送许可依据') from error
                if not owns_send_permit(MotorActionFacts(target,_notice(notice_values)),permit):
                    raise EventValidationError('电机取消许可与原未决意图不匹配')
                change = notification_rows[0]
                if change.row_id != notice_values['id'] or change.after.values != {'outcome':2,'finished_at':event.occurred_at}:
                    raise EventValidationError('电机取消必须共同保存可靠未发送事实')
                final_notice = _notice({**notice_values,**change.after.values})
            else:
                if notification_rows or event.evidence.get('motor_permit') is not None:
                    raise EventValidationError('没有意图的取消不能补建发送事实')
                final_notice = None
            _validate_combination({**target,**row.after.values},final_notice)
        marked.add(row.row_id)
    for row in event.rows:
        if row.table == "cancel_delivery_items" and not row.before.exists:
            values = row.after.values
            if values.get("status") != 1 or values.get("error_code") is not None:
                raise EventValidationError("撤回明细初始必须是待处理且无错误")


def _guard_cancel_result(event, context, items) -> None:
    """RESULT：成功携带完成依据、失败携带登记错误，效果不改写。"""
    for row in items:
        before_facts = context.state_rows.get("cancel_items", {}).get(row.row_id)
        values = row.after.values
        if values.get("cancellation_effect") not in (
                None, before_facts.get("cancellation_effect")):
            raise EventValidationError("最终结果不改写取消效果")
        if values.get("status") == _ITEM_SUCCEEDED:
            target = context.state_rows.get("actions", {}).get(
                before_facts["target_action_id"])
            if target is None:
                raise EventValidationError("取消成功结果缺少目标动作事实")
            if target["type"] in (1, 2, 3) and context.preparation_reads is not None:
                from .preparation import PreparationReads
                if type(context.preparation_reads) is not PreparationReads or not context.preparation_reads.cancel_ready(target):
                    raise EventValidationError("拍摄取消结果仍有准备调用或占用责任")
            from camctl.cancellation.rules import report_cancel_settled
            if not report_cancel_settled(target):
                raise EventValidationError("报告取消成功结果要求目标已终态")
            if values.get("outcome") not in (member.value for member in
                                             CancelOutcomeChoice):
                raise EventValidationError("取消成功必须携带完成依据")
            if values.get("error_code") is not None:
                raise EventValidationError("取消成功不携带错误")
        elif values.get("status") == _ITEM_FAILED:
            if not isinstance(values.get("error_code"), int):
                raise EventValidationError("取消失败必须携带登记错误")
        else:
            raise EventValidationError(
                f"最终结果的成员状态非法: {values.get('status')!r}")


def _withdrawal_guard(event, context) -> None:
    """登记名 withdrawal：撤回明细属于本次目标的取回交付。

    完整撤回状态机由交付模块（N5 组合）拥有；本守卫只核对取消生
    效事务中创建的撤回明细身份。
    """
    for row in event.rows:
        if row.table != "cancel_delivery_items" or row.before.exists:
            continue
        values = row.after.values
        item = context.state_rows.get("cancel_items", {}).get(
            values.get("cancel_item_id"))
        if item is None:
            raise EventValidationError(
                f"撤回明细缺少所属取消成员: {values.get('cancel_item_id')!r}")
        delivery = context.state_rows.get("deliveries", {}).get(
            values.get("delivery_id"))
        if delivery is None or delivery.get("action_id") != item.get(
                "target_action_id"):
            raise EventValidationError(
                f"撤回明细的交付不属于目标取回: {values.get('delivery_id')!r}")
        if event.event_type == 23 and event.reason == 7:
            origin = context.state_rows.get("actions", {}).get(item.get("action_id"))
            target = context.state_rows.get("actions", {}).get(item.get("target_action_id"))
            if (values.get("status") != 1 or values.get("error_code") is not None
                    or values.get("error_details_json") is not None
                    or item.get("status") != _ITEM_RUNNING
                    or origin is None or origin.get("type") != _CANCEL_ACTION_TYPE
                    or origin.get("status") != int(_ACTION_STATUS.RUNNING)
                    or origin.get("cancel_requested") != 0
                    or target is None or target.get("type") != _OBTAIN_TARGET_TYPE
                    or target.get("cancel_requested") != 1
                    or not (delivery.get("status") in (int(_DELIVERY_STATUS.PUBLISHED),
                                                        int(_DELIVERY_STATUS.WITHDRAWN))
                            or delivery.get("status") in (int(_DELIVERY_STATUS.PUBLISHING),
                                                          int(_DELIVERY_STATUS.CANCELED))
                            and delivery.get("withdrawal_state") == int(_WITHDRAWAL_STATUS.UNKNOWN))):
                raise EventValidationError("撤回明细必须属于仍有效的原取消责任")


def register_cancellation_guards(register) -> None:
    """注册取消目标固定事件的正式业务守卫（装配期调用）。"""
    register("cancel", _cancel_targets_guard)
    register("withdrawal", _withdrawal_guard)


def _guard_photo_entry_request(event, context, request, created, origin_id):
    from camctl.devices.start_entry import is_hasselblad_photo, EntryPhase, entry_from_rows
    if request['action_id'] != origin_id or request['occurred_at'] != event.occurred_at:
        raise EventValidationError('入口取消申请的原发起者或首次时刻不符')
    expected = [{'action_id': row.after.values['target_action_id'],
                 'basis': row.after.values['selection_basis'],
                 'effect': row.after.values['cancellation_effect']} for row in created]
    if request['targets'] != expected:
        raise EventValidationError('入口取消申请与固定成员不符')
    for block in request['blocks']:
        target_id = block['action_id']
        target = context.state_rows.get('actions',{}).get(target_id)
        if target is None or not is_hasselblad_photo(target):
            raise EventValidationError('入口取消申请只允许原 Hasselblad 单张源')
        activities = context.complete_rows('device_activities','action_id',target_id)
        activity = next(iter(activities.values()),None)
        runs = context.complete_rows('operation_runs','action_id',target_id)
        starts = [run for run in runs.values() if run['responsibility_key'] == f'start/{target_id}']
        attempts = []
        for run in starts:
            attempts.extend(context.complete_rows('operation_attempts','run_id',run['id']).values())
        if entry_from_rows(target, activity, attempts) is not EntryPhase.BLOCKED:
            raise EventValidationError('入口取消固定缺少前序可靠未进入结果')
        ticket = block['ticket']
        if ticket is None:
            if attempts:
                raise EventValidationError('无票据入口取消不能覆盖原意图')
        elif (len(starts) != 1 or starts[0]['id'] != ticket['run_id']
                or starts[0]['activity_id'] != int(ticket['target_id'])
                or max((a['attempt_no'] for a in attempts),default=0) != ticket['attempt_id']):
            raise EventValidationError('入口取消固定的原票据不符')
        members = [row for row in created if row.after.values['target_action_id'] == target_id]
        if context.current_reads is None or len(members) != 1:
            raise EventValidationError('入口取消固定缺少实际同组源应用查证')
        item_id = members[0].row_id
        try:
            application = context.current_reads.photo_entry_apply_event(event, target_id, item_id, context.transaction)
        except (ConsistencyError, AttributeError, TypeError, ValueError) as error:
            raise EventValidationError('入口取消固定缺少原完整实际源应用事务') from error
        expected = {'item_id': item_id, 'mode': 'pre_start', 'occurred_at': event.occurred_at}
        sources = [row for row in application.rows if row.table == 'actions']
        items = [row for row in application.rows if row.table == 'cancel_items']
        if (application.evidence.get('cancel_request') != expected
                or application.occurred_at != event.occurred_at
                or len(sources) != 1 or sources[0].row_id != target_id
                or sources[0].before.values.get('cancel_requested') != 0
                or sources[0].after.values.get('cancel_requested') != 1
                or len(items) != 1 or items[0].row_id != item_id
                or items[0].before.values.get('status') != _ITEM_PENDING
                or items[0].after.values.get('status') != _ITEM_RUNNING):
            raise EventValidationError('入口取消的实际同组应用结构与原完整申请不符')


def _source_start_rows(context, source):
    from camctl.persistence.repositories.capture_facts import start_facts_from_rows
    try:
        activities = context.complete_rows('device_activities', 'action_id', source['id'])
        runs = context.complete_rows('operation_runs', 'action_id', source['id'])
        attempts = {}
        for identity, run in runs.items():
            if run['id'] != identity:
                raise ConsistencyError('原操作行身份与完整范围不符')
            rows = context.complete_rows('operation_attempts', 'run_id', identity)
            if any(row['id'] != key for key, row in rows.items()):
                raise ConsistencyError('原尝试行身份与完整范围不符')
            attempts[identity] = tuple(rows.values())
        if any(row['id'] != key for key, row in activities.items()):
            raise ConsistencyError('原活动行身份与完整范围不符')
        targets = {}
        for run in runs.values():
            identity = run['activity_id']
            if identity is not None and identity not in activities:
                rows = context.complete_rows('device_activities', 'id', identity)
                target = rows.get(identity)
                if target is None or target['id'] != identity:
                    raise ConsistencyError('原操作目标活动缺失或身份不符')
                targets[identity] = target
        facts = start_facts_from_rows(source, tuple(activities.values()), tuple(runs.values()), attempts,
            activity_targets=tuple(targets.values()))
        return facts, attempts
    except (KeyError, TypeError, ValueError) as error:
        raise EventValidationError('取消来源的原 START 完整事实不可核实') from error


def _photo_preview_source_allowed(context, source, reserved_sources, *, allow_pending_reserved=False):
    """读取完整入口事实；业务规则决定关联并按需读取在途确认。"""
    from camctl.cancellation.rules import existing_photo_preview_decision, photo_preview_entry_decision
    from camctl.devices.start_entry import entry_from_rows
    facts, attempts_by_run = _source_start_rows(context, source)
    previous = existing_photo_preview_decision(source, facts.activity)
    if previous is not None:
        return previous
    attempts = () if facts.run is None else attempts_by_run[facts.run['id']]
    phase = entry_from_rows(source, facts.activity, attempts)
    options = {'reserved': source['id'] in reserved_sources,
               'allow_pending_reserved': allow_pending_reserved}
    result = photo_preview_entry_decision(phase, attempts, **options)
    if result is None:
        result = photo_preview_entry_decision(phase, attempts, **options,
            confirmation_pending=_pending_start_confirmation(context, source))
    return result


def _pending_start_confirmation(context, source):
    return any(run['kind'] == int(enum_for('operation_runs.kind').QUERY_ACTIVITY)
        and run['query_purpose'] == int(enum_for('operation_runs.query_purpose').START_CONFIRMATION)
        and run['status'] in (int(enum_for('operation_runs.status').PENDING), int(enum_for('operation_runs.status').ACTIVE))
        for run in context.complete_rows('operation_runs', 'action_id', source['id']).values())


def _saved_cancel_target(origin):
    from camctl.cancellation.models import CancelTarget
    try:
        raw = origin['input_fields_json']['params']['target']
        fields = {key: parse_object_id(value) if key in ('action_instance_id', 'plan_instance_id') else value
                  for key, value in raw.items()}
        return CancelTarget(**fields)
    except (KeyError, AttributeError, TypeError, ValueError) as error:
        raise EventValidationError('取消固定缺少可靠原地址') from error


class _CancelContextLookup:
    """四类原地址仅访问已经声明完整的当前原行范围。"""
    def __init__(self, context):
        self.context = context

    def action_exists(self, identity):
        return identity in self.context.complete_rows('actions', 'id', identity)

    def plan_actions(self, identity):
        if identity not in self.context.complete_rows('plans', 'id', identity):
            return None
        return tuple(sorted(self.context.complete_rows('actions', 'plan_id', identity)))

    def group_actions(self, identity, group):
        members = self.plan_actions(identity)
        if members is None:
            return None
        return tuple(key for key in members if self.context.state_rows['actions'][key]['group_name'] == group)

    def plan_by_request(self, request_id):
        plans = self.context.complete_rows('plans', 'request_id', parse_object_id(request_id))
        if len(plans) > 1:
            raise EventValidationError('原请求对应多个计划')
        return None if not plans else self.plan_actions(next(iter(plans)))


def _validate_photo_preview_scope(origin, members, context, reserved_sources=frozenset(), *, allow_pending_reserved=False):
    """原地址、完整直接集合、真实关联和资格共同决定唯一固定集合。"""
    from camctl.cancellation.models import TargetFacts, ResolvedTargets, CancelTargetError
    from camctl.cancellation.targets import resolve_cancel_target, prepare_cancel_set
    from camctl.devices.start_entry import is_hasselblad_photo
    resolution = resolve_cancel_target(_saved_cancel_target(origin), _CancelContextLookup(context))
    if resolution.missing or resolution.lookup_failed is not None:
        raise EventValidationError('取消固定缺少可靠完整原直接集合')
    actions = context.state_rows.get('actions', {})
    direct = resolution.action_ids
    links = {}
    for identity in direct:
        links.update(context.complete_rows('auto_preview_links', 'source_action_id', identity))
    obtains = set(direct) | {m['target_action_id'] for m in members} | {link['obtain_action_id'] for link in links.values()}
    for identity in obtains:
        related = context.complete_rows('auto_preview_links', 'obtain_action_id', identity)
        if len(related) > 1:
            raise EventValidationError('同一预览只能具有一条原自动关联')
        links.update(related)
    identities = obtains | {link['source_action_id'] for link in links.values() if link['source_action_id'] is not None}
    for identity in identities:
        action = context.complete_rows('actions', 'id', identity).get(identity)
        if action is None or 'type' not in action:
            raise EventValidationError('固定范围缺少原动作事实')
        if action['type'] == 1 and not {'driver_id', 'execution_spec_json'} <= action.keys():
            raise EventValidationError('固定拍摄来源缺少原驱动或执行规格字段')
    if not any(is_hasselblad_photo(actions[identity]) for identity in identities):
        return
    valid = [link for link in links.values() if link['is_valid'] == 1]
    sources = [link['source_action_id'] for link in valid]
    if len(sources) != len(set(sources)):
        raise EventValidationError('同一来源只能具有一条有效自动预览关联')
    facts = []
    from camctl.cancellation.rules import capture_eligibility_facts, decide_cancel_eligibility, may_apply_cancel
    for identity in direct:
        source = actions[identity]
        if is_hasselblad_photo(source):
            allowed = _photo_preview_source_allowed(context, source, reserved_sources,
                allow_pending_reserved=allow_pending_reserved)
        elif source['type'] in (1, 2, 3):
            start, _ = _source_start_rows(context, source)
            allowed = may_apply_cancel(decide_cancel_eligibility(capture_eligibility_facts(
                source, start.activity, not_started=start.not_started)))
        else:
            # 非拍摄直接目标不提供合法自动预览来源；其自身收场沿原路径。
            allowed = False
        facts.append(TargetFacts(identity, source['status'] in _ACTION_TERMINAL, allowed))
    expected = prepare_cancel_set(origin['id'], ResolvedTargets(tuple(facts),
        tuple((link['source_action_id'], link['obtain_action_id']) for link in valid if link['source_action_id'] in direct)))
    if isinstance(expected, CancelTargetError):
        raise EventValidationError('原完整取消集合包含发起者自身')
    submitted = [(m['target_action_id'], m['selection_basis'], m['cancellation_effect']) for m in members]
    actual = [(target.action_id, target.basis.value, target.cancellation_effect.value) for target in expected.targets]
    if len(set(submitted)) != len(submitted) or sorted(submitted) != sorted(actual):
        raise EventValidationError('固定成员、依据或初始效果与原完整申请不符')
