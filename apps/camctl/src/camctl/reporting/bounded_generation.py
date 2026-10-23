"""在固定 H 分页恢复实体和增长详情，逐节点校验后写出报告。"""

from contextlib import ExitStack
from dataclasses import dataclass
from functools import lru_cache
from itertools import islice, chain

from camctl.contracts.segment_errors import public_gap, validate_segment_error_header
from camctl.contracts.segment_values import (
    SegmentGap,
    SegmentGapKind,
    SegmentGapNeighborState,
)
from camctl.contracts.public_projection import (
    OMIT,
    ProjectionInput,
    project_public,
    projection_structure,
)
from camctl.contracts.values import ConsistencyError
from camctl.contracts.workflow_errors import _public_json_registry
from camctl.contracts.segment_values import SegmentKey, decode_text_enum
from camctl.history.queries import ReportScopeRequest
from camctl.reporting.stream_validation import (
    CompletionRequirement,
    DeferredField,
    NodeBinding,
    ProjectionDescriptor,
    ReadEnd,
    derive_stream_plan,
)

_SEGMENT_PATHS = (
    ("error", "details", "gaps"),
    ("error", "details", "retained_output_ids"),
    ("error", "details", "default_scope", "output_ids"),
)
_EXPANSION = CompletionRequirement(
    "segment_expansion", ("error", "details"), _SEGMENT_PATHS[1:]
)
_COVERAGE_PATHS = (
    ("result", "coverage", "checked_output_ids"),
    ("result", "coverage", "default_scope", "output_ids"),
)
_COVERAGE_EXPANSION = CompletionRequirement(
    "video_coverage_expansion", ("result", "coverage"), _COVERAGE_PATHS
)
_DEFERRED = {
    "plan": frozenset({"actions"}),
    "action": frozenset({"outputs", "deliveries"}),
    "video_coverage": frozenset({"checked_output_ids"}),
    "video_default_scope": frozenset({"output_ids"}),
    "obtain_result": frozenset({"failures"}),
    "delete_result": frozenset({"items"}),
    "cancel_result": frozenset({"items"}),
    "cancel_item": frozenset({"withdrawals"}),
}


def _segment_fields():
    return tuple(
        DeferredField(
            path, unique_by=_EXPANSION.name if path in _SEGMENT_PATHS[1:] else None
        )
        for path in _SEGMENT_PATHS
    )


def _result_item_descriptor(uri):
    # 两个实际 items 规则分别派生自己的逐项投影，保留原 anyOf/if 接受域。
    if "/cancel_result/" in uri:
        return ProjectionDescriptor("/cancel_item", (DeferredField(("withdrawals",)),))
    if "/delete_result/" in uri:
        return ProjectionDescriptor("/delete_item")
    raise ConsistencyError("未登记的动作结果逐项规则")


@lru_cache(maxsize=1)
def _report_plan():
    failure = ProjectionDescriptor("/obtain_failure", _segment_fields(), (_EXPANSION,))
    action = ProjectionDescriptor(
        "/action",
        (
            *_segment_fields(),
            *(
                DeferredField(path, unique_by=_COVERAGE_EXPANSION.name)
                for path in _COVERAGE_PATHS
            ),
            DeferredField(("result", "failures"), item=failure),
            DeferredField(("result", "items"), item=_result_item_descriptor),
            DeferredField(("outputs",), omit_empty=True),
            DeferredField(("deliveries",), omit_empty=True),
        ),
        (_EXPANSION, _COVERAGE_EXPANSION),
    )
    plan = ProjectionDescriptor(
        "/plan", (DeferredField(("actions",), omit_empty=True, item=action),)
    )
    report = ProjectionDescriptor(
        "/report",
        (
            DeferredField(("plans",), omit_empty=True, item=plan),
            DeferredField(("plan_file_diagnostics",), omit_empty=True),
        ),
    )
    return derive_stream_plan(
        "status-report.schema.json", report, registry=_public_json_registry()
    )


def _pages(selection, entity, parent=None):
    cursor = None
    while True:
        page = selection.read_page(entity, parent_id=parent, cursor=cursor)
        yield from page.items
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def _rows(handle, table, ref=None):
    cursor = None
    while True:
        page = handle.read_page(table, ref=ref, cursor=cursor)
        yield from page.items
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def _merge(facts, table, identity, values):
    if values is None:
        raise ConsistencyError("报告 H 处缺少必要关联事实")
    facts.setdefault(table, {})[identity] = dict(values)


def _primary_facts(repo, entity, identity, boundary):
    from camctl.contracts.enums import load_registry

    table = load_registry()["history_objects"][entity]["table"]
    facts = {}
    _merge(
        facts, table, identity, repo.restore_primary_at_h(entity, identity, boundary)
    )
    return facts


def _action_facts(repo, handle, boundary):
    facts = {"actions": {handle.action_id: handle.primary()}}
    # 报告只投影 H 处唯一发布集合；成员数组仍由下方有界端口逐页展开。
    result_ref = handle.active_result_ref()
    if result_ref is not None:
        _merge(
            facts,
            "segment_sets",
            result_ref.set_id,
            handle.read_result(result_ref),
        )
    # 每个动作至多一份；增长关系只通过下方逐项端口读取。
    for table in ("recording_processing", "device_activities", "auto_preview_links"):
        count = 0
        for row in handle.related_rows(table):
            count += 1
            if count > 1:
                raise ConsistencyError("报告动作的有限关联不唯一")
            _merge(facts, table, row.row_id, row.values)
    count = 0
    for identity, values in repo.related_primary_rows_at_h(
        "state_sync", "action_id", handle.action_id, boundary
    ):
        count += 1
        if count > 1:
            raise ConsistencyError("报告动作的状态同步事实不唯一")
        _merge(facts, "state_syncs", identity, values)
    return facts


def _project(entity, identity, facts):
    return project_public(
        ProjectionInput(entity, identity, facts, deferred_fields=_DEFERRED)
    )


@dataclass
class _SegmentExpansion:
    repo: object
    boundary: object
    binding: object
    error: dict | None
    stack: object
    consumed: bool = False

    def __post_init__(self):
        self.ends = {}
        self.handle = self.ref = self.header = None
        if self.error is None or self.error["code"] not in (
            "capture_segment_gap",
            "default_segment_scope_empty",
            "default_segment_scope_unconfirmed",
        ):
            return
        self.header = validate_segment_error_header(
            self.error["code"], self.error["details"]
        )
        self.handle = self.stack.enter_context(
            self.repo.open_segment_history(self.header.source_action_id, self.boundary)
        )
        self.ref = self.handle.active_result_ref()
        source = self.handle.primary()
        if source["type"] not in (2, 3):
            raise ConsistencyError("分段公开详情必须属于普通录像或原生延时")
        if (
            source["execution_started"]
            and source["status"] in (3, 4, 5, 6)
            and self.ref is None
        ):
            raise ConsistencyError("已执行分段来源缺少正式发布集合")
        if self.ref is not None:
            self.handle.read_result(self.ref)
        if self.header.first_gap is not None:
            first = next(_rows(self.handle, "segment_gaps", self.ref), None)
            if first is None:
                raise ConsistencyError("首缺口没有同 H 的实际关系")
            self.error["details"]["first_gap"] = self._gap(first.values)

    def _output(self, identity, file_id=None):
        output = self.repo.restore_primary_at_h("output", identity, self.boundary)
        if (
            output is None
            or output["source_action_id"] != self.header.source_action_id
            or output["kind"] != 1
            or file_id is not None
            and output["device_file_id"] != file_id
        ):
            raise ConsistencyError("分段详情引用不属于同 H 的实际原片")
        return output

    def _gap(self, values):
        def member(name):
            identity = values[name]
            return (
                None
                if identity is None
                else self.handle.row("segment_members", identity)
            )

        left, right, empty = (
            member(name)
            for name in ("left_member_id", "right_member_id", "empty_member_id")
        )
        gap = SegmentGap(
            SegmentKey(values["start_directory"], values["start_index"]),
            SegmentKey(values["end_directory"], values["end_index"]),
            decode_text_enum(SegmentGapKind, values["kind"], column="segment_gaps.kind"),
            None if left is None else left["identity"],
            None if right is None else right["identity"],
            decode_text_enum(SegmentGapNeighborState, values["neighbors_json"]["left"], column="segment_gaps.neighbors_json.state"),
            decode_text_enum(SegmentGapNeighborState, values["neighbors_json"]["right"], column="segment_gaps.neighbors_json.state"),
            None if empty is None else empty["identity"],
        )
        result = public_gap(gap)
        for side, neighbor in (("left", left), ("right", right)):
            if neighbor is None:
                continue
            found = None
            for identity, output in self.repo.related_primary_rows_at_h(
                "output", "device_file_id", neighbor["file_id"], self.boundary
            ):
                if (
                    output["kind"] == 1
                    and output["source_action_id"] == self.header.source_action_id
                ):
                    if found is not None:
                        raise ConsistencyError("缺口邻接原片身份不唯一")
                    found = identity
            if found is not None:
                self._output(found, neighbor["file_id"])
                result[side]["output_id"] = str(found)
        return result

    def members(self, path):
        if self.header is None:
            return
        if path == _SEGMENT_PATHS[0]:
            if self.ref is None:
                return
            first = True
            for row in _rows(self.handle, "segment_gaps", self.ref):
                value = self._gap(row.values)
                if first and value != self.error["details"].get("first_gap"):
                    raise ConsistencyError("公开首缺口与完整缺口第一项不同")
                first = False
                yield row.values["gap_no"], value
        elif path == _SEGMENT_PATHS[1]:
            if self.ref is None:
                return
            from camctl.contracts.enums import enum_for

            original_role = int(enum_for("segment_members.role").ORIGINAL)
            position = 0
            for row in _rows(self.handle, "segment_members", self.ref):
                member = row.values
                if member["role"] != original_role or member["file_id"] is None:
                    continue
                output_id = None
                for identity, output in self.repo.related_primary_rows_at_h(
                    "output", "device_file_id", member["file_id"], self.boundary
                ):
                    if (
                        output["kind"] == 1
                        and output["source_action_id"] == self.header.source_action_id
                    ):
                        if output_id is not None:
                            raise ConsistencyError("一个视频原片关联了多个正式原片产物")
                        output_id = identity
                if output_id is not None:
                    self._output(output_id, member["file_id"])
                    position += 1
                    yield position, str(output_id)
        else:
            if self.ref is None:
                return
            for row in _rows(self.handle, "segment_scope_members", self.ref):
                output_id = row.values["output_id"]
                member = self.handle.row("segment_members", row.values["member_id"])
                if (
                    output_id is None
                    or member is None
                    or member["set_id"] != self.ref.set_id
                ):
                    raise ConsistencyError("正式默认范围缺少实际原片关联")
                self._output(output_id, member["file_id"])
                yield row.values["ordinal"], str(output_id)

    def consume(self, request):
        if (
            self.consumed
            or request.binding != self.binding
            or request.requirement != _EXPANSION
        ):
            raise ConsistencyError("分段展开回执的对象、H 或消费次数不同")
        self.consumed = True
        counts = dict(request.counts)
        if self.header is None:
            if self.ends or any(counts.values()):
                raise ConsistencyError("普通错误不能具有分段增长详情")
            return
        expected = (
            self.header.gap_count or 0,
            self.header.retained_original_count,
            self.header.scope_count or 0,
        )
        for path, count in zip(_SEGMENT_PATHS, expected):
            end = self.ends.get(path)
            if (
                end is None
                or counts.get(path) != count
                or end.count != count
                or end.binding != request.binding
            ):
                raise ConsistencyError("同 H 的完整增长详情与原有限错误头不同")


@dataclass
class _CoverageExpansion:
    repo: object
    handle: object
    boundary: object
    binding: object
    public: dict | None
    ends: dict = None
    consumed: bool = False

    def __post_init__(self):
        self.ends = {}
        self.ref = self.decision = None
        if self.public is None:
            return
        from camctl.contracts.video_coverage import SegmentDecision, decision_from_document

        self.ref = self.handle.active_result_ref()
        if self.ref is None:
            raise ConsistencyError("公开覆盖没有同 H 正式结果集合")
        values = self.handle.read_result(self.ref)
        if values["assessment_json"] is None:
            raise ConsistencyError("同 H 正式结果集合缺少覆盖判断")
        self.decision = decision_from_document(values["assessment_json"])
        if not isinstance(self.decision, SegmentDecision):
            raise ConsistencyError("公开覆盖没有 Action6 公共分段判断")
        self._validate_projection()

    def _validate_projection(self):
        from camctl.contracts.segment_values import DefaultScopeState

        decision = self.decision
        scope = decision.default_scope
        expected_scope_count = (
            0 if scope.state in (DefaultScopeState.UNKNOWN, DefaultScopeState.EMPTY)
            else scope.member_count
        )
        if type(expected_scope_count) is not int or expected_scope_count < 0:
            raise ConsistencyError("覆盖默认范围成员数无效")
        if decision.checked_count > expected_scope_count:
            raise ConsistencyError("覆盖检查前缀超过同 H 默认视频范围")
        if scope.state is DefaultScopeState.UNKNOWN and decision.checked_count:
            raise ConsistencyError("未知默认视频范围不能引用已检查前缀")
        expected = {
            "metric": decision.metric.value,
            "target": decision.target,
            "confirmed": decision.confirmed,
            "status": decision.coverage_status.value,
            "default_scope": {
                "state": scope.state.value,
                "member_count": scope.member_count,
            },
        }
        if decision.reason is not None:
            reason = {"code": decision.reason}
            expected["reason"] = reason
        if self.public != expected:
            raise ConsistencyError("公开覆盖有限字段与同 H 决策不同")

    def _scope_members(self):
        if self.ref is None:
            return
        for row in _rows(self.handle, "segment_scope_members", self.ref):
            output_id = row.values["output_id"]
            member = self.handle.row("segment_members", row.values["member_id"])
            if (
                output_id is None
                or member is None
                or member["set_id"] != self.ref.set_id
                or member["file_id"] is None
            ):
                raise ConsistencyError("覆盖范围缺少同 H 原片与正式产物关联")
            output = self.repo.restore_primary_at_h("output", output_id, self.boundary)
            if (
                output is None
                or output["source_action_id"] != self.ref.action_id
                or output["kind"] != 1
                or output["device_file_id"] != member["file_id"]
            ):
                raise ConsistencyError("覆盖范围引用不属于同 H 的视频原片")
            yield row.values["ordinal"], str(output_id)

    def members(self, path):
        if self.decision is None:
            return
        if path == _COVERAGE_PATHS[1]:
            yield from self._scope_members()
            return
        remaining = self.decision.checked_count
        for position, output_id in self._scope_members():
            if remaining == 0:
                return
            yield position, output_id
            remaining -= 1
        if remaining:
            raise ConsistencyError("覆盖检查前缀超过同 H 已发布范围")

    def consume(self, request):
        if (
            self.consumed
            or request.binding != self.binding
            or request.requirement != _COVERAGE_EXPANSION
        ):
            raise ConsistencyError("覆盖展开回执的对象、H 或消费次数不同")
        self.consumed = True
        counts = dict(request.counts)
        if self.decision is None:
            if self.ends or any(counts.get(path, 0) for path in _COVERAGE_PATHS):
                raise ConsistencyError("没有覆盖的动作不能带增长覆盖数组")
            return
        scope = self.decision.default_scope
        expected_scope = (
            0 if scope.state.value in ("unknown", "empty") else scope.member_count
        )
        expected = (self.decision.checked_count, expected_scope)
        for path, count in zip(_COVERAGE_PATHS, expected):
            end = self.ends.get(path)
            if (
                end is None
                or counts.get(path) != count
                or end.count != count
                or end.binding != request.binding
            ):
                raise ConsistencyError("同 H 覆盖增长数组与有限判断头不同")


def _coverage_arrays(expansion, stream, gate):
    arrays = {}
    for path in _COVERAGE_PATHS:
        if expansion.decision is None:
            if gate.state(path).value == "undecided":
                gate.omit(path)
        else:
            arrays[path] = (
                expansion.members(path),
                lambda position, value: _plain(stream, position, value),
                lambda end, path=path: expansion.ends.__setitem__(path, end),
            )
    gate.supply_completion(_COVERAGE_EXPANSION.name, expansion)
    return arrays


def _end(gate, path, last, count):
    end = ReadEnd(gate.binding, path, count, last)
    gate.exhaust_array(path, end)
    return end


def _array(stream, gate, path, members, emit):
    gate.open_array(path)
    count, last = 0, None
    yield from stream.open_array(path[-1])
    for position, value in members:
        receipt = yield from emit(position, value)
        gate.accept_item(path, receipt, position=position)
        count, last = count + 1, position
    yield from stream.close_array()
    return _end(gate, path, last, count)


def _plain(stream, position, value):
    yield from stream.item(value)
    return value


def _finite_object(stream, value, *, identity=None):
    yield from stream.open_object(identity=identity)
    for name in sorted(value):
        yield from stream.field(name, value[name])
    yield from stream.close_object()


def _write_fields(stream, gate, value, *, path=(), arrays=None):
    """字段按词典序写出，增长数组只在它所属的对象位置展开。"""
    arrays = {} if arrays is None else arrays
    names = set(value) | {p[len(path)] for p in arrays if p[: len(path)] == path}
    for name in sorted(names):
        current = path + (name,)
        if current in arrays:
            members, emit, done = arrays[current]
            end = yield from _array(stream, gate, current, members, emit)
            if done is not None:
                done(end)
        elif isinstance(value.get(name), dict) and any(
            p[: len(current)] == current for p in arrays
        ):
            yield from stream.open_object(name)
            yield from _write_fields(
                stream, gate, value[name], path=current, arrays=arrays
            )
            yield from stream.close_object()
        else:
            yield from stream.field(name, value[name])


def _expansion_arrays(expansion, stream, gate):
    arrays = {}
    for path in _SEGMENT_PATHS:
        if expansion.header is None:
            if gate.state(path).value == "undecided":
                gate.omit(path)
        else:
            arrays[path] = (
                expansion.members(path),
                lambda position, value: _plain(stream, position, value),
                lambda end, path=path: expansion.ends.__setitem__(path, end),
            )
    gate.supply_completion(_EXPANSION.name, expansion)
    return arrays


def _failure_rows(repo, handle, boundary):
    """按依赖、分支、真实身份续读，匹配权威 failure_union 顺序。"""
    for dependency in handle.related_rows("action_dependencies"):
        selections = tuple(
            islice(
                handle.related_rows(
                    "obtain_source_selections", "dependency_id", dependency.row_id
                ),
                2,
            )
        )
        if len(selections) != 1:
            raise ConsistencyError("执行中取回缺少唯一来源选择")
        selection = selections[0]
        base = {
            "action_dependencies": {dependency.row_id: dependency.values},
            "obtain_source_selections": {selection.row_id: selection.values},
        }
        fragment = _project("source_failure", selection.row_id, base)
        if fragment is not OMIT:
            yield f"{dependency.row_id}:0:{selection.row_id}", fragment
        for item in handle.related_rows(
            "obtain_items", "selection_id", selection.row_id
        ):
            facts = {**base, "obtain_items": {item.row_id: item.values}}
            fragment = _project("item_failure", item.row_id, facts)
            if fragment is not OMIT:
                yield f"{dependency.row_id}:1:{item.row_id}", fragment
        # delivery 身份按主键续读，不采用 item 返回顺序推断。
        for identity, delivery in repo.related_primary_rows_at_h(
            "delivery", "action_id", handle.action_id, boundary
        ):
            items = tuple(
                islice(handle.related_rows("obtain_items", "delivery_id", identity), 2)
            )
            if not items or items[0].values["selection_id"] != selection.row_id:
                continue
            if len(items) != 1:
                raise ConsistencyError("取回交付的条目身份不唯一")
            facts = {"deliveries": {identity: delivery}}
            _merge(
                facts,
                "outputs",
                delivery["output_id"],
                repo.restore_primary_at_h("output", delivery["output_id"], boundary),
            )
            fragment = _project("delivery_failure", identity, facts)
            if fragment is not OMIT:
                yield f"{dependency.row_id}:2:{identity}", fragment


def _result_rows(repo, handle, boundary, kind):
    table, projection = (
        ("cancel_items", "cancel_item")
        if kind == "cancel_task"
        else ("cleanup_items", "delete_item")
    )
    for row in handle.related_rows(table):
        facts = {table: {row.row_id: row.values}}
        if kind == "cancel_task":
            target = row.values["target_action_id"]
            _merge(
                facts,
                "actions",
                target,
                repo.restore_primary_at_h("action", target, boundary),
            )
        yield row.row_id, (row, _project(projection, row.row_id, facts))


def _write_action(repo, selection, stream, plan, binding, identity, boundary):
    from camctl.reporting.generation import _merge_output, _merge_delivery

    with ExitStack() as stack:
        handle = stack.enter_context(repo.open_segment_history(identity, boundary))
        finite = _project("action", identity, _action_facts(repo, handle, boundary))
        expansion = _SegmentExpansion(
            repo, boundary, binding, finite.get("error"), stack
        )
        coverage = _CoverageExpansion(
            repo,
            handle,
            boundary,
            binding,
            finite.get("result", {}).get("coverage"),
        )
        gate = plan.begin(binding, finite)
        arrays = _expansion_arrays(expansion, stream, gate)
        arrays.update(_coverage_arrays(coverage, stream, gate))
        for path in (("result", "failures"), ("result", "items")):
            if path[:-1][0] not in finite or (
                (path[-1] == "failures") != (finite["type"] == "obtain_action_outputs")
            ):
                if gate.state(path).value == "undecided":
                    gate.omit(path)
                continue
            if path[-1] == "failures":

                def emit_failure(position, value, path=path):
                    child = gate.item_plan(path)
                    with ExitStack() as nested:
                        expansion = _SegmentExpansion(
                            repo,
                            boundary,
                            gate.child_binding(path, position),
                            value.get("error"),
                            nested,
                        )
                        sub = child.begin(expansion.binding, value)
                        yield from stream.open_object()
                        yield from _write_fields(
                            stream,
                            sub,
                            value,
                            arrays=_expansion_arrays(expansion, stream, sub),
                        )
                        yield from stream.close_object()
                        return sub.close_node()

                arrays[path] = (
                    _failure_rows(repo, handle, boundary),
                    emit_failure,
                    None,
                )
            elif finite["type"] in ("delete_action_outputs", "cancel_task"):

                def emit_result(position, value, path=path):
                    row, fragment = value
                    pointer = (
                        "/cancel_item"
                        if finite["type"] == "cancel_task"
                        else "/delete_item"
                    )
                    child = gate.item_plan(path, pointer=pointer)
                    sub = child.begin(
                        gate.child_binding(path, position, pointer=pointer), fragment
                    )
                    nested = {}
                    if pointer == "/cancel_item":
                        withdrawals = ("withdrawals",)
                        target = repo.restore_primary_at_h(
                            "action", row.values["target_action_id"], boundary
                        )
                        if target["type"] == 3:

                            def members():
                                for item in handle.related_rows(
                                    "cancel_delivery_items",
                                    "cancel_item_id",
                                    row.row_id,
                                ):
                                    yield (
                                        item.row_id,
                                        _project(
                                            "withdrawal",
                                            item.row_id,
                                            {
                                                "cancel_delivery_items": {
                                                    item.row_id: item.values
                                                }
                                            },
                                        ),
                                    )

                            nested[withdrawals] = (
                                members(),
                                lambda p, v: _plain(stream, p, v),
                                None,
                            )
                        else:
                            sub.omit(withdrawals)
                    yield from stream.open_object()
                    yield from _write_fields(stream, sub, fragment, arrays=nested)
                    yield from stream.close_object()
                    return sub.close_node()

                arrays[path] = (
                    _result_rows(repo, handle, boundary, finite["type"]),
                    emit_result,
                    None,
                )
            else:
                gate.omit(path)
        yield from stream.open_object(identity=identity)
        yield from _write_fields(stream, gate, finite, arrays=arrays)
        for name, entity in projection_structure("action").entity_fields:
            path = (name,)
            identities = iter(_pages(selection, entity, identity))
            first = next(identities, None)
            if first is None:
                gate.omit(path, authority_end=ReadEnd(gate.binding, path, 0, None))
                continue
            gate.open_array(path)
            yield from stream.open_array(name)
            count, last = 0, None
            for child_id in chain((first,), identities):
                facts = {}
                (_merge_output if entity == "output" else _merge_delivery)(
                    repo, facts, child_id, boundary
                )
                value = _project(entity, child_id, facts)
                gate.accept_item(path, value, position=child_id)
                yield from _finite_object(stream, value, identity=child_id)
                count, last = count + 1, child_id
            yield from stream.close_array()
            _end(gate, path, last, count)
        yield from stream.close_object()
        return gate.close_node()


def report_tokens(repo, spec, boundary, stream):
    """完整供给 root/plan/action 的同 H 节点及全部结束证明。"""
    plan = _report_plan()
    with repo.open_report_selection(
        ReportScopeRequest(
            boundary=boundary,
            from_wm=spec.from_wm,
            to_wm=spec.to_wm,
            entity_batch_size=spec.entity_batch_size,
        )
    ) as selection:
        binding = NodeBinding(
            selection.instance_id,
            "report",
            spec.report_id,
            boundary,
            (),
            plan.schema_uri,
            plan.projection.pointer,
        )
        gate = plan.begin(
            binding,
            {
                "report_id": str(spec.report_id),
                "from_wm": spec.from_wm,
                "to_wm": spec.to_wm,
            },
        )
        yield from stream.open()
        for name, entity in projection_structure("report").entity_fields:
            path = (name,)
            identities = iter(_pages(selection, entity))
            first = next(identities, None)
            if first is None:
                gate.omit(path, authority_end=ReadEnd(gate.binding, path, 0, None))
                continue
            gate.open_array(path)
            yield from stream.open_array(name)
            count, last = 0, None
            for identity in chain((first,), identities):
                finite = _project(
                    entity, identity, _primary_facts(repo, entity, identity, boundary)
                )
                if entity == "plan":
                    node = gate.item_plan(path).begin(
                        gate.child_binding(path, identity), finite
                    )
                    yield from stream.open_object(identity=identity)
                    yield from _write_fields(stream, node, finite)
                    action_path = ("actions",)
                    actions = iter(_pages(selection, "action", identity))
                    first_action = next(actions, None)
                    if first_action is None:
                        node.omit(
                            action_path,
                            authority_end=ReadEnd(node.binding, action_path, 0, None),
                        )
                    else:
                        node.open_array(action_path)
                        yield from stream.open_array("actions")
                        n, position = 0, None
                        for action_id in chain((first_action,), actions):
                            action_plan = node.item_plan(action_path)
                            receipt = yield from _write_action(
                                repo,
                                selection,
                                stream,
                                action_plan,
                                node.child_binding(action_path, action_id),
                                action_id,
                                boundary,
                            )
                            node.accept_item(action_path, receipt, position=action_id)
                            n, position = n + 1, action_id
                        yield from stream.close_array()
                        _end(node, action_path, position, n)
                    yield from stream.close_object()
                    value = node.close_node()
                else:
                    value = finite
                    yield from _finite_object(stream, finite, identity=identity)
                gate.accept_item(path, value, position=identity)
                count, last = count + 1, identity
            yield from stream.close_array()
            _end(gate, path, last, count)
        gate.close_node()
        yield from stream.finish()
