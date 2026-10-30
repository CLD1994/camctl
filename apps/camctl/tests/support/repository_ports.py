"""在公开同步仓储方法上安装受约束替身，不改写运行时对象。"""

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
from unittest.mock import patch


_delegating = ContextVar("repository_port_delegation", default=frozenset())


@contextmanager
def repository_port(repository_type, observer, methods, *, matches=None):
    """只拦截指定公开方法；替身的 super／delegate 仍执行原仓储。

    matches(name, args, kwargs) 可把故障限定到原申请身份。方法均为同步
    实例方法；原方法和替身的返回、异常以及参数保持不变。嵌套作用域
    中后安装的匹配替身优先，退出后恢复上一层。
    """
    names = tuple(methods)
    if not names or len(set(names)) != len(names):
        raise ValueError("须提供不重复的公开仓储方法")
    operations = []
    for name in names:
        if not isinstance(name, str) or name.startswith("_"):
            raise ValueError("只能拦截公开仓储方法")
        original = getattr(repository_type, name)
        replacement = getattr(observer, name)
        if not callable(original) or not callable(replacement):
            raise TypeError("仓储方法和替身都必须可调用")
        operations.append((name, original, replacement))

    with ExitStack() as patches:
        for name, original, replacement in operations:
            def intercepted(instance, *args, _name=name, _original=original,
                            _replacement=replacement, **kwargs):
                active = _delegating.get()
                if repository_type in active or (
                    matches is not None and not matches(_name, args, kwargs)
                ):
                    return _original(instance, *args, **kwargs)
                token = _delegating.set(active | {repository_type})
                try:
                    return _replacement(*args, **kwargs)
                finally:
                    _delegating.reset(token)

            patches.enter_context(patch.object(
                repository_type, name, wraps(original)(intercepted)))
        yield observer
