"""有限目录维护的轮次提示及其显式退役。"""

import os
import re
import stat
from pathlib import Path

TURN_NAME = '.download-cleanup-turn'
TURN_TEMP = '.download-cleanup-turn.tmp'


def read_cleanup_cursor(staging, name):
    """读取固定格式游标；不存在时从零开始。"""
    path = Path(staging) / name
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                     | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return 0, None
    with os.fdopen(fd, 'rb') as stream:
        observed = os.fstat(stream.fileno())
        if not stat.S_ISREG(observed.st_mode):
            raise ValueError('照片工作目录维护游标不是普通文件')
        raw = stream.read(33)
    if re.fullmatch(rb'[0-9]{1,19}\n', raw) is None:
        raise ValueError('照片工作目录维护游标格式无效')
    return int(raw), observed


def retire_cleanup_turn(staging):
    """在部署方已停止运行、资格已核实后退役唯一轮次提示。"""
    path = Path(staging) / TURN_NAME
    _, observed = read_cleanup_cursor(staging, TURN_NAME)
    if observed is None:
        return
    current = path.lstat()
    if (not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino)):
        raise ValueError('下载维护轮次提示身份发生变化')
    os.unlink(path)
