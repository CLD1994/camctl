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
    #: 仍有占用中拍摄活动的动作（非终态）。
    held_captures: tuple[int, ...]
    #: 未持有读取机会且所属动作合格的读取工作（交付拷贝）。
    grantable_reads: tuple[int, ...]
    #: 已持有本设备读取机会的在途读取工作。
    held_reads: tuple[int, ...]
    #: 驱动声明该设备拍摄与读取可并行（缺省不并行）。
    capture_read_parallel: bool = False


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


#: file_copies.verification_state 的完成态（MATCHED 与源摘要不可用降级）。
_VERIFICATION_DONE = (3, 5)
#: deliveries.status 的取消与撤回终态。
_DELIVERY_INACTIVE = (7, 8)
#: device_activities.occupancy_state 的 HELD。
_OCCUPIED = 1


def plan_device_work(
        connection, now_us: int,
        capture_read_parallel: Container[str] = ()) -> tuple[DeviceWork, ...]:
    """从当前投影装配各设备的调度事实并给出本轮工作计划。

    已取消动作的读取工作不进入普通授予（取消联动由取消收场流程
    推进）；在途读取以其持有的读取机会归属设备；capture_read_
    parallel 提供声明拍摄与读取并行的设备集合（驱动声明面），未
    提供的设备按不并行让路。
    """
    devices: dict[str, dict[str, tuple[int, ...]]] = {}
    with closing(connection.execute(
        "SELECT a.device_id, a.id FROM actions a"
        " JOIN device_activities d ON d.action_id = a.id"
        " WHERE a.status = 2 AND a.cancel_requested = 0"
        " AND a.type IN (1, 2, 3) AND a.scheduled_at <= ?"
        " AND d.occupancy_state = ?"
        " ORDER BY a.device_id, a.plan_id, a.input_index", (now_us, _OCCUPIED),
    )) as cursor:
        for device_id, action_id in cursor.fetchall():
            _bucket(devices, device_id)["due"] = _bucket(devices, device_id)[
                "due"] + (int(action_id),)
    with closing(connection.execute(
        "SELECT a.device_id, a.id FROM actions a"
        " JOIN device_activities d ON d.action_id = a.id"
        " WHERE a.status = 2 AND d.activity_state <> 3"
        " AND d.occupancy_state = ?"
        " ORDER BY a.device_id, a.id", (_OCCUPIED,),
    )) as cursor:
        due = {action_id for device in devices.values()
               for action_id in device["due"]}
        for device_id, action_id in cursor.fetchall():
            if int(action_id) in due:
                continue
            _bucket(devices, device_id)["held"] = _bucket(devices, device_id)[
                "held"] + (int(action_id),)
    with closing(connection.execute(
        "SELECT c.slot_device_id, c.id FROM file_copies c"
        " JOIN deliveries d ON d.id = c.delivery_id"
        " JOIN actions a ON a.id = d.action_id"
        " WHERE c.verification_state NOT IN (?, ?)"
        " AND c.slot_device_id IS NOT NULL"
        " AND a.status = 2 AND a.cancel_requested = 0"
        " ORDER BY c.id", _VERIFICATION_DONE,
    )) as cursor:
        for device_id, copy_id in cursor.fetchall():
            _bucket(devices, device_id)["held_reads"] = _bucket(
                devices, device_id)["held_reads"] + (int(copy_id),)
    with closing(connection.execute(
        "SELECT obs.device_id, c.id FROM file_copies c"
        " JOIN deliveries d ON d.id = c.delivery_id"
        " JOIN actions a ON a.id = d.action_id"
        " JOIN device_files f ON f.id = c.source_device_file_id"
        " JOIN actions obs ON obs.id = f.observer_action_id"
        " WHERE c.verification_state NOT IN (?, ?)"
        " AND d.status NOT IN (?, ?)"
        " AND c.slot_device_id IS NULL" 
        " AND a.status = 2 AND a.cancel_requested = 0"
        " AND obs.device_id IS NOT NULL"
        " ORDER BY a.plan_id, a.input_index, c.id",
        (*_VERIFICATION_DONE, *_DELIVERY_INACTIVE),
    )) as cursor:
        for device_id, copy_id in cursor.fetchall():
            _bucket(devices, device_id)["grantable"] = _bucket(
                devices, device_id)["grantable"] + (int(copy_id),)
    return tuple(
        decide_device_work(DeviceFacts(
            device_id=device_id,
            due_captures=buckets.get("due", ()),
            held_captures=buckets.get("held", ()),
            grantable_reads=buckets.get("grantable", ()),
            held_reads=buckets.get("held_reads", ()),
            capture_read_parallel=device_id in capture_read_parallel))
        for device_id, buckets in sorted(devices.items()))


def _bucket(devices: dict, device_id: str) -> dict[str, tuple[int, ...]]:
    return devices.setdefault(
        device_id, {"due": (), "held": (), "grantable": (), "held_reads": ()})
