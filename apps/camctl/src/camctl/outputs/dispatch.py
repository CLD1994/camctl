"""统一设备工作调度：拍摄优先与读取让路的决策层。

到时拍摄优先于读取：持机会的在途读取为到时拍摄让路（本轮结束本
次读取，下轮派发拍摄）；设备被拍摄活动占用时读取等待；空闲设备
按候选授予读取工作。驱动声明拍摄与读取并行兼容时拍摄不阻塞读取：
在途读取继续、到时拍摄与新的读取授予同轮共存。已取消的工作不进
入普通授予，由取消收场流程推进；取消清理的成员收场经取消链
（N1—N6）驱动。
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from typing import Container

__all__ = [
    "DeviceFacts",
    "DeviceWork",
    "decide_device_work",
    "plan_device_work",
]


@dataclass(frozen=True)
class DeviceFacts:
    """一个设备的调度事实。"""

    device_id: str
    #: 已到时间的拍摄动作（未取消、执行中）。
    due_captures: tuple[int, ...]
    #: 未结束且仍持占用的拍摄活动，包括所属动作已有终态的活动。
    held_captures: tuple[int, ...]
    #: 未持有读取机会且所属动作合格的读取工作（交付拷贝）。
    grantable_reads: tuple[int, ...]
    #: 已持有本设备读取机会的在途读取工作。
    held_reads: tuple[int, ...]
    #: 驱动声明该设备拍摄与读取可并行（缺省不并行）。
    capture_read_parallel: bool = False
    software_recovery: bool = False


@dataclass(frozen=True)
class DeviceWork:
    """一个设备本轮的工作计划。"""

    device_id: str
    #: 本轮派发的到时拍摄动作。
    dispatch_captures: tuple[int, ...]
    #: 本轮授予的读取工作。
    grant_reads: tuple[int, ...]
    #: 为到时拍摄让路、需要结束本次读取的在途工作。
    yield_reads: tuple[int, ...]
    #: 无拍摄工作时继续推进的在途读取。
    resume_reads: tuple[int, ...]
    #: 该设备声明拍摄与读取并行（回显给消费方判断推进条件）。
    capture_read_parallel: bool = False


def decide_device_work(facts: DeviceFacts) -> DeviceWork:
    """按拍摄优先与读取让路规则决定本轮设备工作。

    设备兼容性按驱动声明判定。未声明并行（缺省）时拍摄与读取不
    并行——持机会读取遇到到时拍摄时先结束本次读取，拍摄在下一轮
    派发；设备被拍摄活动占用时读取不推进；无拍摄工作的在途读取继
    续占用机会并推进，不重复授予其他读取。声明并行时拍摄不阻塞
    读取：在途读取继续、到时拍摄与新的读取授予同轮共存（拍摄优
    先表达为派发顺序，不推迟读取）；一次一份拷贝的互斥不因并行
    声明改变，在途读取继续时不重复授予。
    """
    if facts.software_recovery:
        return DeviceWork(facts.device_id, (), (), facts.held_reads, ())
    if not facts.capture_read_parallel:
        if facts.held_reads and facts.due_captures:
            return DeviceWork(
                facts.device_id, (), (), facts.held_reads, ())
        if facts.due_captures:
            return DeviceWork(facts.device_id, facts.due_captures, (), (), ())
        if facts.held_captures:
            return DeviceWork(facts.device_id, (), (), (), ())
        if facts.held_reads:
            return DeviceWork(facts.device_id, (), (), (), facts.held_reads)
        return DeviceWork(facts.device_id, (), facts.grantable_reads, (), ())
    return DeviceWork(
        facts.device_id,
        facts.due_captures,
        () if facts.held_reads else facts.grantable_reads,
        (),
        facts.held_reads,
        True)


def plan_device_work(connection, now_us: int, capture_read_parallel: Container[str] = (),
                     *, candidates=None, limit=None) -> tuple[DeviceWork, ...]:
    """本批设备读取事实；当前持有者独立查询，不依赖它是否在本页。"""
    from camctl.outputs.read_discovery import (
        DEFAULT_BATCH_SIZE, ReadDiscoveryScope, database_identity, device_holder, read_candidates,
    )

    if candidates is None:
        scope = ReadDiscoveryScope(database_identity(connection), "delivery", now_us, 0)
        candidates = read_candidates(connection, scope, limit=DEFAULT_BATCH_SIZE if limit is None else limit).items
        with closing(connection.execute(
            "SELECT DISTINCT a.device_id FROM actions a JOIN device_activities d ON d.action_id=a.id"
            " WHERE d.activity_state<>3 AND d.occupancy_state=1 ORDER BY a.device_id LIMIT ?",
            (DEFAULT_BATCH_SIZE if limit is None else limit,),
        )) as cursor:
            activity_devices = tuple(row[0] for row in cursor.fetchall())
    else:
        activity_devices = ()
    devices = dict.fromkeys(candidate.device_id for candidate in candidates if candidate.device_id is not None)
    devices.update(dict.fromkeys(activity_devices))
    from camctl.capture.software_recovery import recovery_locked
    work = []
    for device_id in devices:
        # 读取消费者只需要知道是否存在拍摄阻挡，不加载该设备所有活动。
        with closing(connection.execute(
            "SELECT a.id FROM actions a JOIN device_activities d ON d.action_id=a.id"
            " WHERE a.device_id=? AND a.status=2 AND a.cancel_requested=0"
            " AND a.type IN (1,2,3) AND a.scheduled_at<=?"
            " AND d.activity_state<>3 AND d.occupancy_state=1 ORDER BY a.id LIMIT 1",
            (device_id, now_us),
        )) as cursor:
            due = cursor.fetchone()
        with closing(connection.execute(
            "SELECT a.id FROM actions a JOIN device_activities d ON d.action_id=a.id"
            " WHERE a.device_id=? AND d.activity_state<>3 AND d.occupancy_state=1"
            " ORDER BY a.id LIMIT 1", (device_id,),
        )) as cursor:
            held_capture = cursor.fetchone()
        holder = device_holder(connection, device_id)
        delivery_holder = None
        if holder is not None:
            with closing(connection.execute(
                "SELECT delivery_id FROM file_copies WHERE id=?", (holder,),
            )) as cursor:
                if cursor.fetchone()[0] is not None:
                    delivery_holder = holder
        reads = tuple(candidate.copy_id for candidate in candidates if candidate.device_id == device_id and not candidate.held)
        work.append(decide_device_work(DeviceFacts(
            device_id, () if due is None else (due[0],),
            () if held_capture is None else (held_capture[0],), reads if holder is None else (),
            () if delivery_holder is None else (delivery_holder,), device_id in capture_read_parallel,
            recovery_locked(connection, device_id))))
    return tuple(work)
