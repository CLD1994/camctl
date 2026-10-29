"""CLI 命令解析与机器结果编码。

stdout 只承载每条命令规定的最终结果：run/submit 输出一行 JSON
会话结果，describe 输出完整能力说明，init 不写 stdout。命令语法
错误按退出码 1 处理（适配 argparse 默认的 2），诊断写入 stderr。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

from camctl.contracts.schemas import SchemaRuleError, SchemaValidationError, validate_document
from camctl.session.outcome import SessionOutcome

__all__ = [
    "Command",
    "CommandKind",
    "CommandLineError",
    "ResultChannelError",
    "encode_describe_document",
    "encode_session_result",
    "main",
    "parse_command",
]

VERSION = "0.1.0"

_CAPABILITIES_SCHEMA = "protocol/capabilities.schema.json"


class CommandLineError(ValueError):
    """命令语法或参数不合法；退出码为 1，不使用 argparse 默认的 2。"""


class ResultChannelError(ValueError):
    """结果消息不满足命令契约，不能写入 stdout。"""


class CommandKind(Enum):
    INIT = "init"
    RUN = "run"
    SUBMIT = "submit"
    DESCRIBE = "describe"
    VERSION = "version"


@dataclass(frozen=True)
class Command:
    kind: CommandKind
    plan_path: str | None
    config_path: str | None
    host_notification_fd: int | None = None
    upgrade_software_recovery: bool = False
    upgrade_capture_preparation: bool = False
    upgrade_output_scope: bool = False


class _Parser(argparse.ArgumentParser):
    """参数错误转为 CommandLineError，退出码契约由 CLI 统一处理。"""

    def __init__(self, *args, stdout: TextIO | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._stdout = stdout

    def print_help(self, file: TextIO | None = None) -> None:
        output = file if file is not None else self._stdout if self._stdout is not None else sys.stdout
        output.write(self.format_help())

    def error(self, message: str) -> None:
        raise CommandLineError(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status:
            raise CommandLineError(message or "命令行参数不合法")
        raise SystemExit(status)


def _build_parser(stdout: TextIO | None = None) -> argparse.ArgumentParser:
    parser = _Parser(prog="camctl", add_help=True, stdout=stdout)
    parser.add_argument("--version", action="store_true", help="输出版本并退出")
    subparsers = parser.add_subparsers(dest="command", required=True,
                                    parser_class=partial(_Parser, stdout=stdout))

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--config", dest="config_path", default=None)
    upgrade = init_parser.add_mutually_exclusive_group()
    upgrade.add_argument("--upgrade-software-recovery", action="store_true", help="备份并显式升级软件恢复格式")
    upgrade.add_argument("--upgrade-capture-preparation", action="store_true", help="备份并显式升级拍摄准备格式")
    upgrade.add_argument("--upgrade-output-scope", action="store_true", help="备份并显式升级输出范围格式")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("plan_path", nargs="?")
    run_parser.add_argument("--config", dest="config_path", default=None)
    run_parser.add_argument("--host-notification-fd", type=_notification_fd,
                            default=None, help="主程序通知管道的写入描述符")

    submit_parser = subparsers.add_parser("submit")
    submit_parser.add_argument("plan_path")
    submit_parser.add_argument("--config", dest="config_path", default=None)

    describe_parser = subparsers.add_parser("describe")
    describe_parser.add_argument("--config", dest="config_path", default=None)
    return parser


def _notification_fd(raw: str) -> int:
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 10:
        raise argparse.ArgumentTypeError("通知描述符必须是非标准流整数")
    value = int(raw)
    if not 3 <= value <= 2147483647:
        raise argparse.ArgumentTypeError("通知描述符范围为 3～2147483647")
    return value


def parse_command(argv: Sequence[str], *, stdout: TextIO | None = None) -> Command:
    """解析命令行；帮助输出后以 SystemExit(0) 正常结束，参数错误为 CommandLineError。"""
    arguments = list(argv)
    if "--version" in arguments:
        return Command(kind=CommandKind.VERSION, plan_path=None, config_path=None)
    try:
        parsed = _build_parser(stdout=stdout).parse_args(arguments)
    except SystemExit as error:  # --help 等不带命令的退出路径。
        if error.code == 0:
            raise
        raise CommandLineError(f"命令行未表达可执行命令: {arguments}") from error
    kind = CommandKind(parsed.command)
    plan_path = getattr(parsed, "plan_path", None)
    if kind is CommandKind.SUBMIT and not plan_path:
        raise CommandLineError("submit 需要计划输入文件路径")
    if kind in (CommandKind.INIT, CommandKind.DESCRIBE) and plan_path:
        raise CommandLineError(f"{kind.value} 不接受计划输入文件")
    return Command(kind=kind, plan_path=plan_path, config_path=parsed.config_path,
                   host_notification_fd=getattr(parsed, "host_notification_fd", None),
                   upgrade_software_recovery=getattr(parsed, "upgrade_software_recovery", False),
                   upgrade_capture_preparation=getattr(parsed, "upgrade_capture_preparation", False),
                   upgrade_output_scope=getattr(parsed, "upgrade_output_scope", False))


def encode_session_result(
    outcome: SessionOutcome, *, requires_needs_run: bool = False
) -> bytes:
    """把会话结果编码为单行 UTF-8 JSON（行末恰好一个换行符）。

    submit 的成功结果必须携带布尔 body.needs_run；run 的成功结果
    不携带 body。违反命令契约的消息拒绝编码，不写入 stdout。
    """
    if outcome.succeeded:
        if requires_needs_run:
            if not isinstance(outcome.needs_run, bool):
                raise ResultChannelError("submit 成功结果必须携带布尔 needs_run")
            message: dict[str, Any] = {
                "kind": "succeeded",
                "body": {"needs_run": outcome.needs_run},
            }
        else:
            if outcome.needs_run is not None:
                raise ResultChannelError("run 成功结果不携带 body")
            message = {"kind": "succeeded"}
    else:
        message = {
            "kind": "error",
            "body": {"reason": outcome.reason, "details": dict(outcome.details)},
        }
    return _encode_json_line(message)


def encode_describe_document(document: Mapping[str, Any]) -> bytes:
    """校验并编码完整能力说明；序列化前失败不产生任何 stdout 输出。

    能力说明中的参数规则可包含精确 Decimal（倍数、默认值），编码
    使用精确数值写法，不经过 float。
    """
    from camctl.persistence.transaction import encode_json_value

    validate_document(_CAPABILITIES_SCHEMA, document)
    encoded = encode_json_value(document)
    if "\n" in encoded:  # pragma: no cover - 精确编码器不产生裸换行
        raise ResultChannelError("结果消息包含换行符")
    return (encoded + "\n").encode("utf-8")


def _encode_json_line(message: Mapping[str, Any]) -> bytes:
    encoded = json.dumps(message, ensure_ascii=False, separators=(", ", " : "))
    if "\n" in encoded:  # pragma: no cover - json.dumps 不产生裸换行
        raise ResultChannelError("结果消息包含换行符")
    return (encoded + "\n").encode("utf-8")


def main(
    argv: Sequence[str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """CLI 入口：返回进程退出码；正常路径只输出一次最终结果。"""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        command = parse_command(list(sys.argv[1:] if argv is None else argv), stdout=out)
    except CommandLineError as error:
        _diagnose_error(err, "camctl: 命令解析失败", error)
        return 1
    except BaseException as error:
        if isinstance(error, SystemExit) and error.code == 0:
            return 0
        _diagnose_error(err, "camctl: 命令解析异常", error)
        raise
    try:
        if command.kind is CommandKind.VERSION:
            out.write(VERSION + "\n")
            return 0
        if command.kind is CommandKind.DESCRIBE:
            return _run_describe(command, out, err)
        if command.kind is CommandKind.INIT:
            return _run_init(command, out, err)
        if command.kind in (CommandKind.RUN, CommandKind.SUBMIT):
            return _run_session_command(command, out, err)
        raise CommandLineError(f"未知的命令 {command.kind}")
    except BaseException as error:
        # 包括协作者启动前及资源关闭后的错误；此处已没有可用日志拥有者。
        _diagnose_error(err, f"camctl {command.kind.value}: 命令失败", error)
        if not isinstance(error, Exception):
            raise
    return 1


def _diagnose_error(err: TextIO, stage: str, error: BaseException) -> None:
    """初始化前及日志关闭后直接保留完整异常图，不污染 stdout。"""
    from camctl.operations.error_diagnostics import exception_diagnostic
    from camctl.logging_runtime.formatting import encode_record
    from camctl.logging_runtime.models import LogLevel

    if getattr(error, "_camctl_cli_diagnostic_attempted", False):
        return
    error._camctl_cli_diagnostic_attempted = True
    payload = {"stage": "cli", "message": stage}
    try:
        payload["exception"] = exception_diagnostic(error)
        encoded = encode_record(LogLevel.ERROR, payload)
    except Exception as format_error:
        payload["exception"] = exception_diagnostic(error, include_message=False)
        payload["format_error"] = exception_diagnostic(format_error, include_message=False)
        try:
            encoded = encode_record(LogLevel.ERROR, payload)
        except Exception:
            encoded = repr(payload)
    try:
        print(encoded, file=err)
    except Exception as diagnostic_error:
        try:
            encoded = json.dumps(exception_diagnostic(diagnostic_error), ensure_ascii=False, separators=(",", ":"))
        except Exception:
            encoded = repr(exception_diagnostic(diagnostic_error, include_message=False))
        error.add_note(f"stderr 诊断写入失败: {encoded}")


def _run_session_command(command: Command, out: TextIO, err: TextIO) -> int:
    from camctl.motor.notification import open_notification_writer

    # 在读取配置、输入和启动任何协作者前取得写端所有权并禁止继承。
    writer = (open_notification_writer(command.host_notification_fd)
              if command.kind is CommandKind.RUN else None)
    primary_error: BaseException | None = None
    try:
        return _execute_session_command(command, out, err, writer)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if writer is not None:
            try:
                writer.close()
            except BaseException as close_error:
                if primary_error is None:
                    raise
                from camctl.bootstrap.lifecycle import _attach_runtime_close_note

                selected = (close_error
                            if isinstance(primary_error, Exception)
                            and not isinstance(close_error, Exception)
                            else primary_error)
                _attach_runtime_close_note(
                    selected, "通知管道关闭后的其他错误",
                    primary_error if selected is close_error else close_error)
                try:
                    _diagnose_error(err, "通知管道关闭诊断", close_error)
                except BaseException as diagnostic_error:
                    if (isinstance(selected, Exception)
                            and not isinstance(diagnostic_error, Exception)):
                        _attach_runtime_close_note(
                            diagnostic_error, "此前命令错误", selected)
                        raise
                    _attach_runtime_close_note(
                        selected, "通知管道关闭诊断写入错误", diagnostic_error)
                if selected is close_error:
                    raise


def _execute_session_command(command: Command, out: TextIO, err: TextIO,
                             writer) -> int:
    import asyncio

    from camctl.acceptance.schema import RuleError
    from camctl.acceptance.input import FileInputReader, parse_input, read_input
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.application import ConfigAdapter
    from camctl.bootstrap.config import ConfigError
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
    from camctl.capture.recovery import RecoveryBoundary


    try:
        adapter = ConfigAdapter(home=Path.home())
        config = adapter.load(_config_arg(command))
        from camctl.devices.drivers.builtin import register_builtin_drivers
        register_builtin_drivers(config)
    except (ConfigError, RuleError, OSError) as error:
        _diagnose_error(err, f"camctl {command.kind.value}: 配置加载失败", error)
        return 1

    source = None
    if command.plan_path is not None:
        read = asyncio.run(read_input(command.plan_path, FileInputReader()))
        source = parse_input(read)

    try:
        deps = build_runtime(
            CommandMode.SUBMIT if command.kind is CommandKind.SUBMIT else CommandMode.RUN,
            config,
            host_notifications=writer,
            # 正式 run 的部署调用方须先完成旧进程组收场。
            recovery_boundary=(RecoveryBoundary.HOST_LOCAL_SETTLED
                               if command.kind is CommandKind.RUN
                               else RecoveryBoundary.UNCONFIRMED),
        )
    except (FileNotFoundError, OSError, RuleError) as error:
        _diagnose_error(err, f"camctl {command.kind.value}: 运行时装配失败", error)
        return 1

    try:
        from camctl.logging_runtime.performance import performance_logging

        log_runtime = getattr(deps, "log_runtime", None)
        with performance_logging(None if log_runtime is None else log_runtime.channel):
            outcome = asyncio.run(execute_command(deps, source))
    except BaseException as error:  # 装配后异常也须先释放 CLI 所有资源。
        from camctl.bootstrap.lifecycle import (
            _attach_session_diagnostic_error,
        )

        selected_error = error
        try:
            close_runtime(deps, primary_error=error)
        except BaseException as close_error:
            if isinstance(error, Exception) and not isinstance(close_error, Exception):
                selected_error = close_error
                _attach_session_diagnostic_error(selected_error, "此前命令异常", error)
            else:
                _attach_session_diagnostic_error(error, "命令资源关闭异常", close_error)
        try:
            _diagnose_error(err, f"camctl {command.kind.value}: 会话错误", selected_error)
        except BaseException as diagnostic_error:
            if isinstance(selected_error, Exception) and not isinstance(diagnostic_error, Exception):
                _attach_session_diagnostic_error(diagnostic_error, "此前命令异常", selected_error)
                raise
            _attach_session_diagnostic_error(selected_error, "会话错误诊断写入异常", diagnostic_error)
        if selected_error is not error:
            raise selected_error.with_traceback(selected_error.__traceback__)
        if not isinstance(error, Exception):
            raise
        return 1
    close_runtime(deps)
    try:
        payload = encode_session_result(
            outcome,
            requires_needs_run=command.kind is CommandKind.SUBMIT,
        ).decode("utf-8")
    except ResultChannelError as error:
        _diagnose_error(err, f"camctl {command.kind.value}: 结果不符合命令契约", error)
        return 1
    out.write(payload)
    return 0 if outcome.succeeded else 1


def _run_describe(command: Command, out: TextIO, err: TextIO) -> int:
    from camctl.acceptance.schema import RuleError
    from camctl.bootstrap.application import (
        ConfigAdapter,
        EmptyCapabilityCatalog,
        describe,
    )
    from camctl.bootstrap.config import ConfigError
    from camctl.logging_runtime.one_shot import stderr_logging
    from camctl.logging_runtime.events import measure_stage

    try:
        from camctl.devices.catalog import build_catalog, default_driver_definitions
        from camctl.devices.drivers.builtin import register_builtin_drivers

        adapter = ConfigAdapter(home=Path.home())
        config = adapter.load(_config_arg(command))
        with stderr_logging(config.log, err, command_name="describe",
                on_error=lambda stage, error: _diagnose_error(err, stage, error)):
            with measure_stage("describe"):
                register_builtin_drivers(config)
                catalog = build_catalog(config, default_driver_definitions())
                document = describe(config, catalog)
                payload = encode_describe_document(document).decode("utf-8")
    except (ConfigError, RuleError, SchemaRuleError, SchemaValidationError, OSError) as error:
        _diagnose_error(err, "camctl describe: 能力说明导出失败", error)
        return 1
    out.write(payload)
    return 0


def _run_init(command: Command, out: TextIO, err: TextIO) -> int:
    from camctl.bootstrap.application import ConfigAdapter
    from camctl.bootstrap.config import ConfigError
    from camctl.logging_runtime.one_shot import stderr_logging
    from camctl.logging_runtime.events import measure_stage, record_event
    from camctl.persistence.initialization import (
        InitOutcome,
        initialize_state,
    )

    try:
        adapter = ConfigAdapter(home=Path.home())
        config = adapter.load(_config_arg(command))
        with stderr_logging(config.log, err, command_name="init",
                on_error=lambda stage, error: _diagnose_error(err, stage, error)):
            with measure_stage("initialization"):
                result = initialize_state(config, adapter.state_db_path(config),
                    upgrade_software_recovery=command.upgrade_software_recovery,
                    upgrade_capture_preparation=command.upgrade_capture_preparation,
                    upgrade_output_scope=command.upgrade_output_scope)
            record_event("initialization", event="result", outcome=result.outcome.value)
    except (ConfigError, OSError) as error:
        _diagnose_error(err, "camctl init: 初始化失败", error)
        return 1
    if result.outcome is InitOutcome.FAILED:
        errors = (() if result.error is None else (result.error,)) + result.secondary_errors
        error = (BaseExceptionGroup(result.detail, errors) if len(errors) > 1
                 else errors[0] if errors else RuntimeError(result.detail))
        _diagnose_error(err, f"camctl init: {result.detail}", error)
        return 1
    # init 的 stdout 保持为空。
    return 0


def _config_arg(command: Command) -> Path | None:
    return Path(command.config_path) if command.config_path else None
