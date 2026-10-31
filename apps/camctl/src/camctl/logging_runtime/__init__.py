"""运行日志的通用调用入口。"""

__all__ = ["get_logger", "get_sync_logger"]


def get_logger(name: str):
    from camctl.logging_runtime.logger import get_logger as factory
    return factory(name)


def get_sync_logger(name: str):
    from camctl.logging_runtime.logger import get_sync_logger as factory
    return factory(name)
