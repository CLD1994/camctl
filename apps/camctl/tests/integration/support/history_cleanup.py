"""历史集成测试保留主要异常并报告资源收场错误。"""

import asyncio
import traceback


async def close_executor(executor, timeout=20):
    """有界等待真实关闭；超时不得取消正在结束的工作线程。"""
    task = asyncio.create_task(executor.close())
    await asyncio.wait_for(asyncio.shield(task), timeout)


def finish_cleanup(primary, errors):
    """主要异常保持原对象；无主要异常时上交首个收场错误。"""
    if primary is not None:
        for error in errors:
            if error is not primary:
                primary.add_note("资源收场错误：\n" + "".join(traceback.format_exception(error)))
    elif errors:
        for error in errors[1:]:
            errors[0].add_note("后续资源收场错误：\n" + "".join(traceback.format_exception(error)))
        raise errors[0]
