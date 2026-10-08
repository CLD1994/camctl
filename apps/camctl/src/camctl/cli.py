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


class _Parser(argparse.ArgumentParser):
    """参数错误转为 CommandLineError，退出码契约由 CLI 统一处理。"""

    def error(self, message: str) -> None:
        raise CommandLineError(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status:
            raise CommandLineError(message or "命令行参数不合法")
        raise SystemExit(status)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="camctl", add_help=True)
    parser.add_argument("--version", action="store_true", help="输出版本并退出")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--config", dest="config_path", default=None)

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


def parse_command(argv: Sequence[str]) -> Command:
    """解析命令行；语法与参数错误统一为 CommandLineError。"""
    arguments = list(argv)
    if "--version" in arguments:
        return Command(kind=CommandKind.VERSION, plan_path=None, config_path=None)
    try:
        parsed = _build_parser().parse_args(arguments)
    except SystemExit as error:  # --help 等不带命令的退出路径。
        raise CommandLineError(f"命令行未表达可执行命令: {arguments}") from error
    kind = CommandKind(parsed.command)
    plan_path = getattr(parsed, "plan_path", None)
    if kind is CommandKind.SUBMIT and not plan_path:
        raise CommandLineError("submit 需要计划输入文件路径")
    if kind in (CommandKind.INIT, CommandKind.DESCRIBE) and plan_path:
        raise CommandLineError(f"{kind.value} 不接受计划输入文件")
    return Command(kind=kind, plan_path=plan_path, config_path=parsed.config_path,
                   host_notification_fd=getattr(parsed, "host_notification_fd", None))


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
        command = parse_command(list(sys.argv[1:] if argv is None else argv))
    except CommandLineError as error:
        print(f"camctl: {error}", file=err)
        return 1
    if command.kind is CommandKind.VERSION:
        out.write(VERSION + "\n")
        return 0
    if command.kind is CommandKind.DESCRIBE:
        return _run_describe(command, out, err)
    if command.kind is CommandKind.INIT:
        return _run_init(command, out, err)
    if command.kind in (CommandKind.RUN, CommandKind.SUBMIT):
        return _run_session_command(command, out, err)
    print(f"camctl: 未知的命令 {command.kind}", file=err)
    return 1


def _run_session_command(command: Command, out: TextIO, err: TextIO) -> int:
    from camctl.motor.notification import open_notification_writer

    # 在读取配置、输入和启动任何协作者前取得写端所有权并禁止继承。
    writer = (open_notification_writer(command.host_notification_fd)
              if command.kind is CommandKind.RUN else None)
    try:
        return _execute_session_command(command, out, err, writer)
    finally:
        if writer is not None:
            writer.close()


def _execute_session_command(command: Command, out: TextIO, err: TextIO,
                             writer) -> int:
    import asyncio

    from camctl.acceptance.schema import RuleError
    from camctl.acceptance.input import FileInputReader, parse_input, read_input
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.application import ConfigAdapter
    from camctl.bootstrap.config import ConfigError
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command


    try:
        adapter = ConfigAdapter(home=Path.home())
        config = adapter.load(_config_arg(command))
    except (ConfigError, OSError) as error:
        print(f"camctl {command.kind.value}: {error}", file=err)
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
        )
    except (FileNotFoundError, OSError, RuleError) as error:
        print(f"camctl {command.kind.value}: {error}", file=err)
        return 1

    try:
        outcome = asyncio.run(execute_command(deps, source))
    except Exception as error:  # 会话装配外溢的异常按会话错误收口。
        close_runtime(deps)
        print(f"camctl {command.kind.value}: 会话错误: {error}", file=err)
        return 1
    close_runtime(deps)
    try:
        payload = encode_session_result(
            outcome,
            requires_needs_run=command.kind is CommandKind.SUBMIT,
        ).decode("utf-8")
    except ResultChannelError as error:
        print(f"camctl {command.kind.value}: 结果不符合命令契约: {error}", file=err)
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

    try:
        from camctl.devices.catalog import build_catalog, default_driver_definitions

        adapter = ConfigAdapter(home=Path.home())
        config = adapter.load(_config_arg(command))
        catalog = build_catalog(config, default_driver_definitions())
        document = describe(config, catalog)
        payload = encode_describe_document(document).decode("utf-8")
    except (ConfigError, RuleError, SchemaRuleError, SchemaValidationError, OSError) as error:
        print(f"camctl describe: {error}", file=err)
        return 1
    out.write(payload)
    return 0


def _run_init(command: Command, out: TextIO, err: TextIO) -> int:
    from camctl.bootstrap.application import ConfigAdapter
    from camctl.bootstrap.config import ConfigError
    from camctl.persistence.initialization import InitOutcome, initialize_state

    try:
        adapter = ConfigAdapter(home=Path.home())
        config = adapter.load(_config_arg(command))
        result = initialize_state(config, adapter.state_db_path(config))
    except (ConfigError, OSError) as error:
        print(f"camctl init: {error}", file=err)
        return 1
    if result.outcome is InitOutcome.FAILED:
        print(f"camctl init: {result.detail}", file=err)
        return 1
    # init 的 stdout 保持为空。
    return 0


def _config_arg(command: Command) -> Path | None:
    return Path(command.config_path) if command.config_path else None
