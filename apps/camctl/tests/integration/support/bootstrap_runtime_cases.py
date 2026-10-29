"""运行配置与资源拥有者用例的正式默认会话装配。"""

import asyncio
from dataclasses import replace

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.lifecycle import build_runtime
from camctl.devices.evidence import EvidenceContract
from camctl.operations.recovery import RecoveryBoundary

from .session import capture_entry, ResultDriver, run_default_session


def configured_session(config, catalog, driver, results, evidence, *,
                       photo_check=None, extra_ms=0, stop_config=None, stopper=None):
    """本次配置先成为正式输入，默认生命周期再创建全部流程与线程。"""
    devices = {name: dict(value) for name, value in config.devices.items()}
    if photo_check is not None:
        devices['cam-1']['result_check'] = {
            'max_attempts': photo_check.max_attempts,
            'call_timeout_s': photo_check.timeout_s,
            'retry_interval_s': photo_check.retry_interval_s,
        }
    if 'cam-2' in devices:
        devices['cam-2']['capture'] = {'extra_wait_ms': extra_ms}
    if stop_config is not None:
        devices['cam-1']['recording'] = {
            'max_stop_attempts': stop_config.max_attempts,
            'stop_timeout_s': stop_config.timeout_s,
            'stop_retry_interval_s': stop_config.retry_interval_s,
        }
    entry = capture_entry(driver, evidence.with_contract(EvidenceContract(
        'adb_foreground_recovery', 1, 'control', frozenset())),
        stopper=stopper, results_driver=ResultDriver(results))
    entry = replace(entry, declaration=replace(entry.declaration,
        adb_foreground_recovery_operations=frozenset({'control', 'stop', 'result'})))
    current = replace(config, devices=devices)
    deps = build_runtime(CommandMode.RUN, current, catalog=catalog,
                         recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED)
    task = asyncio.create_task(run_default_session(deps, None, entry, poll_interval_s=0.1))
    return deps, task
