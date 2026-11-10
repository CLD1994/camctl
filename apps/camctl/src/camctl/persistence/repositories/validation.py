"""业务存储从正式定义构造一份独立、不可变的事件规则集合。"""

from types import MappingProxyType
from functools import lru_cache

from camctl.history.events import load_event_registry
from camctl.history.validators import EventValidationError, resolve_validated_event, validate_event_facts
from camctl.logging_runtime.transaction_timing import trace_detail


def build_business_rules(overrides=None):
    """不读取其他装配的登记状态；规则名称以事件登记为准。"""
    from camctl.history import validators
    from camctl.persistence.repositories import (
        acceptance, cancellation, capture, file_batches, motor, operations,
        outputs, scheduling, segments, timelapse,
    )
    from camctl.reporting import policy
    from camctl.session import clock

    rules = {}

    def add(name, rule):
        rules[name] = rule

    for name, rule in (
        ("row_shape", validators._row_shape_guard),
        ("transaction", validators._transaction_guard),
        ("ownership", validators._ownership_guard),
        ("report_impact", validators._report_impact_guard),
    ):
        add(name, rule)
    for register in (
        acceptance.register_acceptance_guards, clock.register_clock_guard,
        operations.register_operation_guards, capture.register_capture_guards,
        timelapse.register_timelapse_guards, outputs.register_outputs_guards,
        file_batches.register_file_batch_guards, cancellation.register_cancellation_guards,
        policy.register_report_guards, scheduling.register_window_guard,
        motor.register_motor_guards, segments.register_segment_guards,
    ):
        register(add)
    registry = load_event_registry()
    expected = {name for event in registry["events"].values()
                for branch in event["branches"].values() for name in branch["guards"]}
    if set(rules) != expected:
        raise ValueError(f"正式规则与事件登记不一致: 缺少 {sorted(expected - rules.keys())}，"
                         f"未登记 {sorted(rules.keys() - expected)}")
    if overrides is not None:
        unknown = set(overrides) - expected
        if unknown:
            raise ValueError(f"测试规则未登记: {sorted(unknown)}")
        rules.update(overrides)
    return MappingProxyType(rules)


class BusinessStorage:
    """一份业务规则装配；数据库连接由每次调用的事务拥有。"""

    def __init__(self, overrides=None):
        self._rules = build_business_rules(overrides)

    @trace_detail("history_event_validation")
    def validate_event(self, event, context):
        event_name, branch_name, branch = validate_event_facts(event, context)
        for name in branch["guards"]:
            rule = self._rules.get(name)
            if rule is None:
                raise EventValidationError(
                    f"{event_name}.{branch_name} 要求的具名校验未接入: {name}")
            rule(event, context)
        return resolve_validated_event(event, context, event_name, branch_name)

    def commit_operation(self, command, operation_key, owned):
        from .transactions import commit_operation
        return commit_operation(command, operation_key, owned, storage=self)


@lru_cache(maxsize=1)
def default_business_storage():
    """生产写入共享一次完整装配，事务事实仍由每次调用单独拥有。"""
    return BusinessStorage()
