"""报告生成编排：固定 H 入选、逐对象恢复与分段编码写文件。

生成器不读取当前时间、配置或设备，只消费冻结依据与历史状态：
按根实体逐个恢复入选子树并分段写出报告字节，不组装整份报告对
象；同一冻结依据与历史数据生成相同字节。状态库或历史解释失败
按其错误分类向上传递，不降级为普通文件失败。
"""

from __future__ import annotations

import hashlib
import os
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Protocol

from camctl.contracts.public_projection import ProjectionInput, project_public
from camctl.contracts.values import ConsistencyError
from camctl.history.queries import PlanSubtree, ReportScope, ReportScopeRequest
from camctl.persistence.repositories.history import HistoryRepository
from camctl.reporting.encoding import (
    ReportDocument,
    ReportStream,
    chunk_bytes,
)

__all__ = [
    "GeneratedFile",
    "GenerationSpec",
    "generate_report_file",
    "verify_frozen_registration",
]

_DEFAULT_BUFFER_SIZE = 64 * 1024


@dataclass(frozen=True)
class GenerationSpec:
    """一次生成任务的全部输入：报告身份、冻结依据与执行参数。"""

    db_path: Path
    report_id: int
    from_wm: int
    to_wm: int
    frozen_event_id: int
    staging_path: Path
    entity_batch_size: int = 32
    event_batch_size: int = 256

    def __post_init__(self) -> None:
        if not isinstance(self.report_id, int) or isinstance(self.report_id, bool) \
                or self.report_id < 1:
            raise ValueError(f"报告身份必须是正整数: {self.report_id!r}")
        for name in ("from_wm", "to_wm", "frozen_event_id",
                     "entity_batch_size", "event_batch_size"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} 必须是非负整数: {value!r}")
        if self.entity_batch_size < 1 or self.event_batch_size < 1:
            raise ValueError("读取批次必须是正整数")


@dataclass(frozen=True)
class GeneratedFile:
    """生成完成的文件事实：位置、长度与摘要；不证明发布。"""

    path: Path
    size_bytes: int
    sha256: str


class _ReportReader(Protocol):
    """生成所需的历史读取端口。"""

    def select_report_scope(self, request: ReportScopeRequest) -> ReportScope: ...

    def restore_entity(self, entity: str, entity_id: int, boundary) -> dict: ...

    def related_entity_ids(self, table: str, column: str, value: int) -> list[int]: ...


def verify_frozen_registration(repository: HistoryRepository, spec: GenerationSpec) -> "HistoryBoundary":
    """核验生成输入与状态库中登记的冻结依据一致，返回完整 H。"""
    from camctl.contracts.history_values import HistoryBoundary

    registered = repository.frozen_registration(spec.report_id)
    if (registered.from_wm, registered.to_wm,
            registered.boundary.last_event_id) != (
            spec.from_wm, spec.to_wm, spec.frozen_event_id):
        raise ConsistencyError(
            f"报告 {spec.report_id} 的冻结依据与生成输入不符:"
            f" 库内 from={registered.from_wm} to={registered.to_wm}"
            f" frozen={registered.boundary.last_event_id}")
    return registered.boundary


def generate_report_file(
    spec: GenerationSpec,
    *,
    repository: HistoryRepository | None = None,
    buffer_size: int = _DEFAULT_BUFFER_SIZE,
) -> GeneratedFile:
    """按冻结依据生成完整报告文件并同步；失败不留下可发布文件。

    入选范围在固定 H 上选择，逐根实体恢复子树后分段写出；每个根
    实体片段独立通过公共 Schema 校验，不回读已写出字节。完成摘
    要并同步文件后返回文件事实；投影或历史错误发生时删除半成品，
    不进入发布流程。
    """
    repo = repository if repository is not None else HistoryRepository(spec.db_path)
    boundary = verify_frozen_registration(repo, spec)
    scope = repo.select_report_scope(ReportScopeRequest(
        boundary=boundary, from_wm=spec.from_wm, to_wm=spec.to_wm,
        entity_batch_size=spec.entity_batch_size))
    document = ReportDocument(
        report_id=str(spec.report_id), from_wm=spec.from_wm, to_wm=spec.to_wm)
    stream = ReportStream(document)
    digest = hashlib.sha256()
    size = 0
    spec.staging_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(spec.staging_path, "wb")
    try:
        def tokens() -> Iterator[bytes]:
            yield from stream.open()
            for subtree in scope.plans:
                facts = _plan_facts(repo, subtree, boundary)
                fragment = project_public(ProjectionInput(
                    "plan", subtree.plan_id, facts, dict(subtree.selection)))
                yield from stream.section("plans", fragment)
            for diagnostic_id in scope.diagnostics:
                facts = _diagnostic_facts(repo, diagnostic_id, boundary)
                fragment = project_public(ProjectionInput(
                    "diagnostic", diagnostic_id, facts, {}))
                yield from stream.section("plan_file_diagnostics", fragment)
            yield from stream.finish()

        for chunk in chunk_bytes(tokens(), buffer_size=buffer_size):
            handle.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        handle.flush()
        os.fsync(handle.fileno())
    except BaseException:
        handle.close()
        spec.staging_path.unlink(missing_ok=True)
        raise
    handle.close()
    return GeneratedFile(
        path=spec.staging_path, size_bytes=size, sha256=digest.hexdigest())


def _merge(facts: dict, rows: Mapping[tuple[str, int], Mapping]) -> None:
    for (table, row_id), values in rows.items():
        facts.setdefault(table, {})[row_id] = dict(values)


def _plan_facts(
    repo: _ReportReader, subtree: PlanSubtree, boundary,
) -> dict[str, dict[int, dict]]:
    """恢复一个计划入选子树的 H 事实：逐实体恢复，不组装整库。"""
    facts: dict[str, dict[int, dict]] = {}
    _merge(facts, repo.restore_entity("plan", subtree.plan_id, boundary))
    for action_id, action_selection in subtree.selection.get("action", {}).items():
        rows = repo.restore_entity("action", action_id, boundary)
        _merge(facts, rows)
        for sync_id in repo.related_entity_ids("state_syncs", "action_id", action_id):
            _merge(facts, repo.restore_entity("state_sync", sync_id, boundary))
        for output_id in action_selection.get("output", {}):
            _merge_output(repo, facts, output_id, boundary)
        for delivery_id in action_selection.get("delivery", {}):
            _merge_delivery(repo, facts, delivery_id, boundary)
        # 取回失败汇总覆盖该动作在 H 的全部关联交付，不受本次报告的
        # 交付增量子集合限制：条目引用的交付行随动作事实一并恢复。
        referenced = sorted({
            int(values["delivery_id"])
            for (table, _row_id), values in rows.items()
            if table == "obtain_items" and values.get("delivery_id") is not None
        })
        for delivery_id in referenced:
            if delivery_id not in action_selection.get("delivery", {}):
                _merge_delivery(repo, facts, delivery_id, boundary)
    return facts


def _merge_output(repo, facts: dict, output_id: int, boundary) -> None:
    rows = repo.restore_entity("output", output_id, boundary)
    _merge(facts, rows)
    output = rows.get(("outputs", output_id))
    if output is None:
        raise ConsistencyError(f"产物 {output_id} 在 H 不存在")
    for column, entity in (
            ("device_file_id", "device_file"),
            ("intermediate_file_id", "intermediate_file")):
        file_id = output.get(column)
        if file_id is not None:
            _merge(facts, repo.restore_entity(entity, int(file_id), boundary))


def _merge_delivery(repo, facts: dict, delivery_id: int, boundary) -> None:
    rows = repo.restore_entity("delivery", delivery_id, boundary)
    _merge(facts, rows)
    delivery = rows.get(("deliveries", delivery_id))
    if delivery is None:
        raise ConsistencyError(f"交付 {delivery_id} 在 H 不存在")
    output_id = delivery.get("output_id")
    if output_id is not None:
        _merge_output(repo, facts, int(output_id), boundary)
    target_ids = sorted({
        int(copy["target_file_id"])
        for (table, _), copy in rows.items()
        if table == "file_copies" and copy.get("target_file_id") is not None
    })
    for target_id in target_ids:
        _merge(facts, repo.restore_entity("intermediate_file", target_id, boundary))


def _diagnostic_facts(repo, diagnostic_id: int, boundary) -> dict[str, dict[int, dict]]:
    facts: dict[str, dict[int, dict]] = {}
    _merge(facts, repo.restore_entity("diagnostic", diagnostic_id, boundary))
    return facts
