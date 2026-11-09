"""观察真实受管进程端口的退出与输出收场。"""


class ObservedManagedProcess:
    def __init__(self, process):
        self._delegate = process
        self.exit = None
        self.output_finished = False

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    async def wait(self):
        result = await self._delegate.wait()
        self.exit = result
        return result

    async def wait_output(self):
        result = await self._delegate.wait_output()
        self.output_finished = True
        return result
