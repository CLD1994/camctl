"""pytest 与跨组件 CLI 接缝共用的测试侧预算入口。"""
from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import subprocess
import sys
import uuid

import pytest

import camctl_memory as memory


_receipt_directories: set[Path] = set()
_checked_receipts: set[Path] = set()


def pytest_addoption(parser):
    group = parser.getgroup("camctl-memory")
    group.addoption("--camctl-memory-limit", default=memory.DEFAULT_MEMORY_LIMIT,
                    help="每个被测 camctl 及其全部后代共享的内存预算")
    group.addoption("--camctl-memory-cgroup-root", default=None,
                    help="可创建专属预算组的 Linux 委派目录")


def pytest_configure(config):
    root = config.getoption("camctl_memory_cgroup_root")
    try:
        memory.configure_budget(config.getoption("camctl_memory_limit"),
                                None if root is None else Path(root).absolute())
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error


def receipt_directory(root):
    directory = Path(root).absolute() / "memory-budget"
    _receipt_directories.add(directory)
    return directory


def _receipt_path(root):
    return receipt_directory(root) / f"camctl-{uuid.uuid4().hex}.json"


def _budget():
    config = memory.get_budget_config()
    memory.ensure_memory_budget_available(config)
    return config


def run_camctl(command, *, receipt_root, **kwargs):
    """只执行调用方明确指定的 camctl，不识别或拦截无关工具。"""
    return memory.run_budgeted(command, budget=_budget(),
                               receipt_path=_receipt_path(receipt_root), **kwargs)


def start_camctl(command, *, receipt_root, **kwargs):
    return memory.BudgetedPopen(command, budget=_budget(),
                               receipt_path=_receipt_path(receipt_root), **kwargs)


def run_test_command(command, *, camctl=False, receipt_root, **kwargs):
    """混合工具辅助只依据显式身份分流，不猜测可执行文件名称。"""
    if camctl:
        return run_camctl(command, receipt_root=receipt_root, **kwargs)
    return subprocess.run(command, **kwargs)


def memory_launcher_command(command, *, receipt_root):
    """host 自身在组外；其测试代理只给明确的 CLI 子树施加预算。"""
    budget = _budget()
    result = [sys.executable, str(Path(__file__).resolve()),
              "--camctl-memory-limit", f"{budget.limit_bytes}B",
              "--receipt-directory", str(receipt_directory(receipt_root))]
    if budget.cgroup_root is not None:
        result.extend(["--camctl-memory-cgroup-root", str(budget.cgroup_root)])
    return [*result, "--", *map(str, command)]


def write_budgeted_launcher(command, *, receipt_root, target):
    command = memory_launcher_command(command, receipt_root=receipt_root)
    target = Path(target)
    target.write_text('#!/bin/sh\nexec ' + shlex.join(command) + ' "$@"\n', encoding="utf-8")
    target.chmod(0o700)
    return target


def budgeted_host_launcher(launcher, *, receipt_root):
    target = Path(receipt_root) / f"memory-launcher-{uuid.uuid4().hex}.sh"
    return write_budgeted_launcher([launcher], receipt_root=receipt_root, target=target)


def _finalize_registered_receipts():
    """处理本次登记的所有收据和启动错误，返回累积诊断。"""
    directories = tuple(_receipt_directories)
    _receipt_directories.clear()
    errors = []
    for directory in directories:
        try:
            receipts = list(directory.glob("camctl-*.json"))
        except OSError as error:
            errors.append(f"无法列出预算收据 {directory}：{error}")
            receipts = []
        for path in receipts:
            if path in _checked_receipts:
                continue
            try:
                memory.finalize_budget_receipts([path])
            except (memory.BudgetEnvironmentError, memory.MemoryBudgetExceeded, OSError) as error:
                errors.append(f"{path}：{error}")
            finally:
                _checked_receipts.add(path)
        try:
            for path in directory.glob("camctl-*.failure"):
                try:
                    errors.append(path.read_text(encoding="utf-8"))
                except OSError as error:
                    errors.append(f"无法读取预算代理错误 {path}：{error}")
        except OSError as error:
            errors.append(f"无法列出预算代理错误 {directory}：{error}")
    return errors


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    yield
    errors = _finalize_registered_receipts()
    if errors:
        pytest.fail("camctl 预算代理报告资源或环境错误：" + "; ".join(errors), pytrace=False)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    try:
        yield
    finally:
        # KeyboardInterrupt 可以跳过逐例 teardown；内置 sessionfinish 先收场 fixtures。
        errors = _finalize_registered_receipts()
        if errors:
            message = "camctl 预算代理报告资源或环境错误：" + "; ".join(errors)
            reporter = session.config.pluginmanager.get_plugin("terminalreporter")
            if reporter is not None:
                reporter.write_line(message, red=True)
            else:
                print(message, file=sys.stderr)
            # 不抛新异常，保留原先的中断退出码和 fixture 收场异常。
            if session.exitstatus == pytest.ExitCode.OK:
                session.exitstatus = pytest.ExitCode.TESTS_FAILED


def launcher_main(argv=None):
    parser = argparse.ArgumentParser(description="保持 PID 的测试 host 受限 camctl 启动入口")
    parser.add_argument("--camctl-memory-limit", default=memory.DEFAULT_MEMORY_LIMIT)
    parser.add_argument("--camctl-memory-cgroup-root", type=lambda value: Path(value).absolute())
    parser.add_argument("--receipt-directory", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("缺少明确的 camctl 命令")
    budget = memory.configure_budget(args.camctl_memory_limit, args.camctl_memory_cgroup_root)
    args.receipt_directory.mkdir(parents=True, exist_ok=True)
    receipt = args.receipt_directory / f"camctl-{uuid.uuid4().hex}.json"
    try:
        memory.exec_budgeted(command, budget=budget, receipt_path=receipt)
    except (memory.BudgetEnvironmentError, memory.MemoryBudgetExceeded) as error:
        receipt.with_suffix(".failure").write_text(
            f"{type(error).__name__}: {error}", encoding="utf-8")
        raise
    raise RuntimeError("exec_budgeted 意外返回，未执行实际 camctl")


if __name__ == "__main__":
    raise SystemExit(launcher_main())
