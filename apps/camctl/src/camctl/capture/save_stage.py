"""同一原申请的提交、回执消费和内存应用由一个必需阶段推进。"""

import asyncio

from camctl.persistence.business import settle_required


class RequiredSaveStage:
    """并发入口加入正在执行的阶段；等待者取消不撤销该阶段。"""

    def __init__(self):
        self._task = None

    async def run(self, operation):
        if self._task is None:
            self._task = asyncio.create_task(self._run(operation))
        return await settle_required(self._task)

    async def _run(self, operation):
        try:
            return await operation()
        finally:
            # 成功后的申请由所属流程结束；未知及失败仍可沿原申请再次核实。
            self._task = None
