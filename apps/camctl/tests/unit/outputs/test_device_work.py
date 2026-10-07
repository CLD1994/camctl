"""设备工作调度让路决策表的单元测试。

到时拍摄优先：持机会的读取为其让路（本轮结束读取，下轮派发拍
摄）；设备被拍摄活动占用时读取等待；空闲设备授予合格读取工作；
无拍摄工作的在途读取继续推进且不重复授予；已取消的工作不进入普
通授予（由取消收场流程推进）。驱动声明拍摄与读取并行兼容时拍摄
不阻塞读取：在途读取继续、到时拍摄与新的读取授予同轮共存，一次
一份拷贝的互斥保持。
"""

from __future__ import annotations

from camctl.outputs.dispatch import DeviceFacts, DeviceWork, decide_device_work


class TestDecideDeviceWork:
    def test_idle_device_grants_reads(self):
        work = decide_device_work(DeviceFacts(
            device_id="cam-1", due_captures=(), held_captures=(),
            grantable_reads=(41,), held_reads=()))
        assert work == DeviceWork("cam-1", (), (41,), (), ())

    def test_due_capture_takes_priority_over_reads(self):
        work = decide_device_work(DeviceFacts(
            device_id="cam-1", due_captures=(11,), held_captures=(),
            grantable_reads=(41,), held_reads=()))
        assert work.dispatch_captures == (11,)
        assert work.grant_reads == () and work.yield_reads == ()
        assert work.resume_reads == ()

    def test_held_read_yields_before_due_capture_dispatch(self):
        """不兼容时本次读取结束再优先到时拍摄。"""
        work = decide_device_work(DeviceFacts(
            device_id="cam-1", due_captures=(11,), held_captures=(),
            grantable_reads=(41,), held_reads=(40,)))
        assert work == DeviceWork("cam-1", (), (), (40,), ())

    def test_capture_occupancy_blocks_reads(self):
        work = decide_device_work(DeviceFacts(
            device_id="cam-1", due_captures=(), held_captures=(12,),
            grantable_reads=(41,), held_reads=(40,)))
        assert work == DeviceWork("cam-1", (), (), (), ())

    def test_held_read_continues_without_due_capture(self):
        """无拍摄工作：已开始的读取继续推进，不重复授予其他读取。"""
        work = decide_device_work(DeviceFacts(
            device_id="cam-1", due_captures=(), held_captures=(),
            grantable_reads=(41,), held_reads=(40,)))
        assert work == DeviceWork("cam-1", (), (), (), (40,))


class TestDecideDeviceWorkParallel:
    """驱动声明拍摄与读取并行兼容时的调度分支。"""

    def test_parallel_held_read_continues_with_due_capture(self):
        """声明并行时在途读取不为到时拍摄让路：拍摄派发、读取继续。"""
        work = decide_device_work(DeviceFacts(
            device_id="cam-1", due_captures=(11,), held_captures=(),
            grantable_reads=(41,), held_reads=(40,),
            capture_read_parallel=True))
        assert work == DeviceWork("cam-1", (11,), (), (), (40,), True)

    def test_parallel_grants_reads_during_capture_occupancy(self):
        """声明并行时拍摄占用不再阻塞读取：候选照常授予。"""
        work = decide_device_work(DeviceFacts(
            device_id="cam-1", due_captures=(), held_captures=(12,),
            grantable_reads=(41,), held_reads=(),
            capture_read_parallel=True))
        assert work == DeviceWork("cam-1", (), (41,), (), (), True)

    def test_parallel_due_capture_grants_new_reads(self):
        """到时拍摄与新读取授予同轮共存：拍摄优先是派发顺序。"""
        work = decide_device_work(DeviceFacts(
            device_id="cam-1", due_captures=(11,), held_captures=(),
            grantable_reads=(41,), held_reads=(),
            capture_read_parallel=True))
        assert work == DeviceWork("cam-1", (11,), (41,), (), (), True)

    def test_parallel_occupied_read_still_blocks_new_grants(self):
        """并行不改变一次一份拷贝：在途读取继续时不重复授予。"""
        work = decide_device_work(DeviceFacts(
            device_id="cam-1", due_captures=(), held_captures=(12,),
            grantable_reads=(41,), held_reads=(40,),
            capture_read_parallel=True))
        assert work == DeviceWork("cam-1", (), (), (), (40,), True)
