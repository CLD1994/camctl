"""真实管道用例的进程期限和描述符收场。"""
import errno
import multiprocessing
import os


def run_pipe_case(case):
    process = multiprocessing.get_context("spawn").Process(target=case)
    process.start()
    try:
        process.join(10)
        assert not process.is_alive(), "管道操作未在期限内结束"
        assert process.exitcode == 0, f"管道进程退出码：{process.exitcode}"
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        assert not process.is_alive(), "管道进程未能退出"
        process.close()


def close_if_same(fd, identity):
    """只关闭仍指向测试创建对象的描述符。"""
    try:
        current = os.fstat(fd)
    except OSError as error:
        if error.errno == errno.EBADF:
            return
        raise
    if (current.st_dev, current.st_ino, current.st_mode) == (
            identity.st_dev, identity.st_ino, identity.st_mode):
        os.close(fd)
