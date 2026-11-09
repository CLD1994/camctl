"""按实际文件身份限定标准描述符操作的故障注入。"""

import os
from pathlib import Path


def fail_fd_operation(monkeypatch, operation: str, path: Path, failure: OSError, *, after=False):
    """仅对指向 path 的描述符报告故障；after 用于已关闭后的错误。"""
    real_operation = getattr(os, operation)

    def injected(fd, *args):
        try:
            matches = os.path.samefile(fd, path)
        except OSError:
            matches = False
        if matches:
            if after:
                real_operation(fd, *args)
            raise failure
        return real_operation(fd, *args)

    monkeypatch.setattr(os, operation, injected)
