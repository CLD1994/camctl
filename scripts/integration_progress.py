"""隔离运行入口使用的 pytest 观测插件，不改变测试选择与结果。"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import pytest

_directory = None
_stream = None
_started = None
_primary = {}
_subchecks = Counter()


def _write(record):
    record = {**record, 'utc': datetime.now(timezone.utc).isoformat(), 'pid': os.getpid()}
    _stream.write(json.dumps(record, ensure_ascii=False) + '\n')
    _stream.flush()
    temporary = _directory / 'progress.next'
    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    temporary.replace(_directory / 'progress.json')


def pytest_configure(config):
    global _directory, _stream, _started
    # 配置路径在此消费，安装版 CLI 的环境不会携带观测器配置。
    _directory = Path(os.environ.pop('CAMCTL_INTEGRATION_PROGRESS'))
    _stream = (_directory / 'events.jsonl').open('w', encoding='utf-8')
    _started = time.monotonic()
    _write({'status': 'collecting', 'args': list(config.invocation_params.args)})


def pytest_collection_finish(session):
    (_directory / 'inventory.json').write_text(json.dumps(
        [item.nodeid for item in session.items], ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
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
    (_directory / 'cases.json').write_text(json.dumps(cases, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    _write({'status': 'finished', 'exit_code': int(exitstatus),
            'collected': session.testscollected,
            'outcomes': dict(Counter(case['outcome'] for case in cases)),
            'subchecks': dict(_subchecks),
            'elapsed_seconds': time.monotonic() - _started})


def pytest_unconfigure(config):
    if _stream is not None:
        _stream.close()
