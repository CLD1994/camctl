"""一次性命令复用有界日志队列，只向原 stderr 写入。"""

import asyncio
from contextlib import contextmanager
import os
import threading

from camctl.logging_runtime.events import event_logging, log_context
from camctl.logging_runtime.lifecycle import LogRuntime, close_logging
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel
from camctl.logging_runtime.standard_logging import install_standard_logging
from camctl.operations.error_diagnostics import exception_diagnostic


class _StderrWriter:
    def __init__(self, stream):
        self.stream = stream
        self.error = None
        self.lock = threading.Lock()

    def write(self, message):
        with self.lock:
            if self.error is not None:
                return False
            try:
                self.stream.write(message.rstrip("\n") + "\n")
                self.stream.flush()
            except BaseException as error:
                # 消费线程只交付事实；控制异常在主调用收场后继续传播。
                self.error = error
                return False
            return True

    def __call__(self, record):
        return self.write(record.rendered_message())


@contextmanager
def stderr_logging(log_config, err, *, command_name, on_error):
    """日志失败不改变命令结果，原控制异常保持优先级。"""
    writer = _StderrWriter(err)
    runtime = None
    installation = None
    channel = None
    failures = []
    primary = None
    try:
        try:
            level = LogLevel(log_config.level.lower())
            channel = LogChannel(capacity=log_config.queue_capacity,
                low_watermark=log_config.queue_low_watermark,
                high_watermark=log_config.queue_high_watermark, level=level,
                sample_probability=float(log_config.info_sample_probability),
                writer=writer, fallback=writer.write)
            runtime = LogRuntime(channel, level=level, identity=f"pid={os.getpid()}")
            channel.start()
            installation = install_standard_logging(channel, writer.write)
        except Exception as error:
            failures.append(error)
            channel = None
        with event_logging(channel, fallback=writer.write), log_context(command_name=command_name):
            yield
    except BaseException as error:
        primary = error
        raise
    finally:
        if installation is not None:
            try:
                installation.close()
            except BaseException as error:
                failures.append(error)
        if runtime is not None:
            try:
                closed = asyncio.run(close_logging(runtime))
                if closed.close_error is not None:
                    failures.append(RuntimeError(closed.close_error))
            except BaseException as error:
                failures.append(error)
        if writer.error is not None:
            # stderr 已不可用时不向同一流反复诊断普通写入错误。
            if not isinstance(writer.error, Exception):
                failures.append(writer.error)
        selected = primary if primary is not None and not isinstance(primary, Exception) else None
        for failure in failures:
            if isinstance(failure, Exception):
                if writer.error is None:
                    try:
                        on_error("一次性日志通道收场", failure)
                    except Exception:
                        pass  # 诊断通道自身失效不改变业务结果。
                    except BaseException as error:
                        if selected is None:
                            selected = error
                        elif selected is not error:
                            selected.add_note("日志诊断控制异常: " + repr(exception_diagnostic(error, include_message=False)))
            elif selected is None:
                selected = failure
            elif selected is not failure:
                selected.add_note("后续日志控制异常: " + repr(exception_diagnostic(failure, include_message=False)))
        if selected is not None and selected is not primary:
            if primary is not None:
                selected.add_note("此前命令异常: " + repr(exception_diagnostic(primary, include_message=False)))
            raise selected
