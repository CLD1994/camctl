"""目录绑定切换的责任分类、目录树核对与三路径共同保存。

切换只经显式 init 发生：先按数据库责任分类判定资格，再核对原三
目录只剩空目录树、新三目录不存在或同样只剩空目录树，最后一次元
信息事务重新核对旧绑定与责任并整体保存新三路径。失败保持原绑
定；提交结果未知时按重读的完整绑定判定，不混用新旧目录。
"""

from __future__ import annotations

import enum
import os
import sqlite3
from dataclasses import dataclass
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Iterable

from camctl.persistence.runtime import StateDatabaseError

#: 目录树扫描的单批条目数上限：分批消费目录项，不无界积累。
_SCAN_BATCH = 256


@dataclass(frozen=True)
class SwitchFacts:
    """切换资格判定依赖的数据库责任计数。"""

    required_intermediates: int
    pending_cleanups: int
    promoted_pending_output_cleanup: int
    unfinished_runs: int
    open_deliveries: int
    open_withdrawals: int
    open_reports: int


def load_switch_facts(connection: sqlite3.Connection) -> SwitchFacts:
    """读取当前库的切换责任计数；结构不可读时不给"无责任"结论。"""
    try:
        required = connection.execute(
            "SELECT COUNT(*) FROM intermediate_files"
            " WHERE retention_state = 1").fetchone()[0]
        releasable = connection.execute(
            "SELECT COUNT(*) FROM intermediate_files"
            " WHERE retention_state = 2 AND cleanup_state <> 4").fetchone()[0]
        promoted = connection.execute(
            "SELECT COUNT(*) FROM intermediate_files AS i"
            " JOIN outputs AS o ON o.intermediate_file_id = i.id"
            " WHERE i.retention_state = 3 AND o.availability <> 3").fetchone()[0]
        runs = connection.execute(
            "SELECT COUNT(*) FROM operation_runs WHERE status IN (1,2)"
        ).fetchone()[0]
        deliveries = connection.execute(
            "SELECT COUNT(*) FROM deliveries WHERE status IN (1,2,3,4)"
        ).fetchone()[0]
        withdrawals = connection.execute(
            "SELECT COUNT(*) FROM deliveries WHERE withdrawal_state IN (2,6)"
        ).fetchone()[0]
        reports = connection.execute(
            "SELECT COUNT(*) FROM reports WHERE status IN (1,2,3,5)"
        ).fetchone()[0]
    except sqlite3.Error as error:
        raise StateDatabaseError(f"切换责任检查不可靠: {error}") from error
    return SwitchFacts(
        required_intermediates=required,
        pending_cleanups=releasable,
        promoted_pending_output_cleanup=promoted,
        unfinished_runs=runs,
        open_deliveries=deliveries,
        open_withdrawals=withdrawals,
        open_reports=reports,
    )


def classify_switch_eligibility(facts: SwitchFacts) -> str | None:
    """按阻止切换分类返回首个阻止原因；责任全部结束时为 None。"""
    if facts.required_intermediates:
        return (f"{facts.required_intermediates} 个中间文件仍为 REQUIRED，"
                "先完成所属读取、处理、登记或交接")
    if facts.pending_cleanups:
        return f"{facts.pending_cleanups} 个可释放中间文件的清理未完成"
    if facts.promoted_pending_output_cleanup:
        return (f"{facts.promoted_pending_output_cleanup} 个已提升中间文件的"
                "正式产物尚未清理")
    if facts.unfinished_runs:
        return f"{facts.unfinished_runs} 个设备运行尚未结束"
    if facts.open_deliveries:
        return f"{facts.open_deliveries} 个交付尚未完成发布"
    if facts.open_withdrawals:
        return f"{facts.open_withdrawals} 个交付撤回尚未结束"
    if facts.open_reports:
        return f"{facts.open_reports} 个报告责任仍须本地完成或恢复"
    return None


def scan_directory_tree(root: Path, *, required: bool) -> str | None:
    """核对目录树内没有文件、符号链接或无法识别的对象。

    required 为真时目录必须存在且可检查；为假时允许不存在（新目
    录可由切换的准备步骤创建）。目录项按固定批次读取，遇到首个
    阻止项即返回诊断；纯空目录树通过。
    """
    try:
        if root.is_symlink():
            return f"目录不能是符号链接: {root}"
        if not root.exists():
            if required:
                return f"原目录不存在，不能确认内容为空: {root}"
            return None
        if not root.is_dir():
            return f"路径已被非目录对象占用: {root}"
        pending = [root]
        while pending:
            current = pending.pop()
            try:
                with os.scandir(current) as entries:
                    while True:
                        batch = list(islice(entries, _SCAN_BATCH))
                        if not batch:
                            break
                        for entry in batch:
                            if entry.is_symlink():
                                return f"存在符号链接对象: {entry.path}"
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(Path(entry.path))
                                continue
                            if entry.is_file(follow_symlinks=False):
                                return f"仍存在文件: {entry.path}"
                            return f"存在无法识别的对象: {entry.path}"
            except OSError as error:
                return f"目录不能可靠检查: {current}: {error}"
        return None
    except OSError as error:
        return f"目录不能可靠检查: {root}: {error}"


def scan_original_directories(roots: Iterable[Path]) -> str | None:
    """原三目录必须存在且只剩空目录树；首个阻止项即返回诊断。"""
    for root in roots:
        blocked = scan_directory_tree(root, required=True)
        if blocked is not None:
            return blocked
    return None


def scan_new_directories(roots: Iterable[Path]) -> str | None:
    """新三目录须不存在或只剩空目录树，不能接管已有文件。"""
    for root in roots:
        blocked = scan_directory_tree(root, required=False)
        if blocked is not None:
            return blocked
    return None


def check_distinct_roots(roots: Iterable[Path]) -> str | None:
    """三目录不得相同、互相包含或经实际别名指向同一位置。"""
    actual = list(roots)
    normalized: list[str] = []
    for root in actual:
        try:
            normalized.append(os.path.normcase(str(root.resolve())))
        except OSError as error:
            return f"目录无法核实: {root}: {error}"
    for i in range(len(normalized)):
        for j in range(i + 1, len(normalized)):
            first, second = normalized[i], normalized[j]
            if first == second:
                return f"目录指向同一位置: {actual[i]} 与 {actual[j]}"
            if (first.startswith(second + os.sep)
                    or second.startswith(first + os.sep)):
                return f"目录互相包含: {actual[i]} 与 {actual[j]}"
    return None


def _device_of(path: Path) -> int:
    """目录所在设备号；Windows 恒为 0，原子移动语义按 Linux 部署核对。"""
    return os.stat(path).st_dev


def check_atomic_move_support(roots: Iterable[Path]) -> str | None:
    """三目录须位于同一文件系统，支持 staging 到 ready 的原子移动。

    不存在的目录按最近存在的祖先判定所属设备。
    """
    actual = list(roots)
    devices: list[int] = []
    for root in actual:
        probe = root
        try:
            while not probe.exists():
                probe = probe.parent
        except OSError as error:
            return f"目录无法核实: {root}: {error}"
        try:
            devices.append(_device_of(probe))
        except OSError as error:
            return f"目录无法核实: {probe}: {error}"
    if len(set(devices)) > 1:
        return f"目录不在同一文件系统，无法支持原子移动: {actual}"
    return None


def canonical_to_path(canonical: str) -> Path:
    """把保存的规范绑定字符串还原为本机路径。

    目标部署为恒等；Windows 开发环境按保存时的 "/<盘符>:/..." 约
    定还原，与 initialization._canonical_binding 的映射互逆。
    """
    if os.name != "nt":
        return Path(canonical)
    parts = PurePosixPath(canonical).parts
    if (len(parts) < 2 or parts[0] != "/" or len(parts[1]) != 2
            or parts[1][1] != ":"):
        raise StateDatabaseError(
            f"保存的绑定路径无法还原为本机路径: {canonical}")
    return Path(f"{parts[1][0].upper()}:/", *parts[2:])


class SwitchCommitObservation(enum.Enum):
    """绑定保存事务结束后的三路径重读结果。"""

    COMPLETED = "completed"
    NOT_COMPLETED = "not_completed"
    INCONSISTENT = "inconsistent"


def classify_commit_observation(
    actual: Iterable[str], old: Iterable[str], new: Iterable[str]
) -> SwitchCommitObservation:
    """三路径均为新值表示完成；均为旧值表示未完成；混合组合报错。"""
    actual_tuple = tuple(actual)
    if actual_tuple == tuple(new):
        return SwitchCommitObservation.COMPLETED
    if actual_tuple == tuple(old):
        return SwitchCommitObservation.NOT_COMPLETED
    return SwitchCommitObservation.INCONSISTENT


class SwitchCommitError(StateDatabaseError):
    """绑定保存事务未能可靠完成；原绑定未被证明已经改变。"""


def switch_directory_binding(
    owned, old_binding: Iterable[str], new_binding: Iterable[str]
) -> SwitchCommitObservation:
    """一次元信息事务重新核对旧绑定与责任后三路径共同保存。

    提交确认后重读核实整体为新值；提交异常时尽力回滚并按重读的
    完整绑定分类结果。重新核对发现责任、绑定变化、数据库错误或
    混合组合时抛出 SwitchCommitError，由调用方按保持原绑定报告。
    """
    old_tuple = tuple(old_binding)
    new_tuple = tuple(new_binding)
    connection = owned.connection
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as error:
        raise SwitchCommitError(f"绑定保存事务无法开始: {error}") from error
    try:
        row = connection.execute(
            "SELECT staging_path, ready_path, processing_path"
            " FROM database_metadata WHERE id = 1").fetchone()
        if row is None or tuple(row) != old_tuple:
            raise SwitchCommitError(
                f"原绑定已变化，未保存新绑定: {tuple(row) if row else None}")
        blocked = classify_switch_eligibility(load_switch_facts(connection))
        if blocked is not None:
            raise SwitchCommitError(f"事务内重新核对发现未结束责任: {blocked}")
        cursor = connection.execute(
            "UPDATE database_metadata SET staging_path = ?, ready_path = ?,"
            " processing_path = ? WHERE id = 1 AND staging_path = ?"
            " AND ready_path = ? AND processing_path = ?",
            (*new_tuple, *old_tuple))
        if cursor.rowcount != 1:
            raise SwitchCommitError("原绑定核对失败，未保存新绑定")
        connection.execute("COMMIT")
    except sqlite3.Error as error:
        _try_rollback(connection)
        observation = _observe_binding(connection, old_tuple, new_tuple)
        raise SwitchCommitError(
            f"绑定保存事务失败: {error}；重读判定 {observation.value}") from error
    except SwitchCommitError:
        _try_rollback(connection)
        raise
    observation = _observe_binding(connection, old_tuple, new_tuple)
    if observation is not SwitchCommitObservation.COMPLETED:
        raise SwitchCommitError(
            f"绑定保存后重读结果异常: {observation.value}")
    return observation


def _try_rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚未完成事务；失败保留连接原始状态供重读诊断。"""
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _observe_binding(
    connection: sqlite3.Connection,
    old_binding: tuple[str, ...],
    new_binding: tuple[str, ...],
) -> SwitchCommitObservation:
    """重读保存的三路径并按完整绑定分类；不可读时报告错误。"""
    try:
        row = connection.execute(
            "SELECT staging_path, ready_path, processing_path"
            " FROM database_metadata WHERE id = 1").fetchone()
    except sqlite3.Error as error:
        raise SwitchCommitError(f"绑定保存结果无法重读: {error}") from error
    if row is None:
        raise SwitchCommitError("绑定保存结果无法重读: 元信息缺失")
    return classify_commit_observation(tuple(row), old_binding, new_binding)
