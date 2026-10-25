"""隔离运行入口使用的 pytest 观测插件，不改变测试选择与结果。"""
from __future__ import annotations

import builtins
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import pytest

# 产品测试会替换这些共享模块上的函数；观测器保留自己的原始调用边界。
_open = builtins.open
_replace = os.replace
_dumps = json.dumps
_monotonic = time.monotonic
_getpid = os.getpid
_now = datetime.now
_utc = timezone.utc

_paths = {}
_stream = None
_started = None
_primary = {}
_subchecks = Counter()


def _write_json(name, value):
    target = _paths[name]
    temporary = target + '.next'
    with _open(temporary, 'w', encoding='utf-8') as stream:
        stream.write(_dumps(value, ensure_ascii=False, indent=2) + '\n')
    _replace(temporary, target)


def _write(record):
    record = {**record, 'utc': _now(_utc).isoformat(), 'pid': _getpid()}
    _stream.write(_dumps(record, ensure_ascii=False) + '\n')
    _stream.flush()
    _write_json('progress.json', record)


def pytest_configure(config):
    global _paths, _stream, _started
    # 配置路径在此消费，安装版 CLI 的环境不会携带观测器配置。
    directory = Path(os.environ.pop('CAMCTL_INTEGRATION_PROGRESS'))
    _paths = {name: str(directory / name) for name in (
        'events.jsonl', 'progress.json', 'inventory.json', 'cases.json')}
    _stream = _open(_paths['events.jsonl'], 'w', encoding='utf-8')
    _started = _monotonic()
    _write({'status': 'collecting', 'args': list(config.invocation_params.args)})


def pytest_collection_finish(session):
    _write_json('inventory.json', [item.nodeid for item in session.items])
    _write({'status': 'collected', 'collected': len(session.items)})


def pytest_runtest_logstart(nodeid, location):
    _write({'status': 'running', 'phase': 'setup', 'nodeid': nodeid})


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    _write({'status': 'running', 'phase': 'setup', 'nodeid': item.nodeid})


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    _write({'status': 'running', 'phase': 'call', 'nodeid': item.nodeid})


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item):
    _write({'status': 'running', 'phase': 'teardown', 'nodeid': item.nodeid})


def pytest_runtest_logreport(report):
    nested = hasattr(report, 'context')
    record = {'status': 'running', 'nodeid': report.nodeid, 'phase': report.when,
              'outcome': report.outcome, 'duration_seconds': report.duration,
              'record': 'subcheck' if nested else 'phase'}
    if nested:
        _subchecks[report.outcome] += 1
        if report.failed:
            case = _primary.setdefault(report.nodeid, {'nodeid': report.nodeid, 'phases': {}, 'outcome': None})
            case['outcome'] = 'failed'
        record['context'] = repr(report.context)
    else:
        case = _primary.setdefault(report.nodeid, {'nodeid': report.nodeid, 'phases': {}, 'outcome': None})
        case['phases'][report.when] = {'outcome': report.outcome, 'duration_seconds': report.duration}
        if report.failed:
            case['outcome'] = 'failed'
        elif report.skipped and case['outcome'] != 'failed':
            case['outcome'] = 'skipped'
        elif report.when == 'call' and case['outcome'] is None:
            case['outcome'] = 'passed'
    _write(record)


def pytest_sessionfinish(session, exitstatus):
    cases = list(_primary.values())
    for case in cases:
        case['duration_seconds'] = sum(phase['duration_seconds'] for phase in case['phases'].values())
    _write_json('cases.json', cases)
    _write({'status': 'finished', 'exit_code': int(exitstatus),
            'collected': session.testscollected,
            'outcomes': dict(Counter(case['outcome'] for case in cases)),
            'subchecks': dict(_subchecks),
            'elapsed_seconds': _monotonic() - _started})


def pytest_unconfigure(config):
    if _stream is not None:
        _stream.close()
