"""轻量记录本节点的准备和检查阶段；不修改生产协作者。"""
from __future__ import annotations

import functools
import json
import os
import time
from pathlib import Path

import pytest


def _write(event):
    with Path(os.environ['CAMCTL_HISTORY_PREPARATION_TIMING']).open('a') as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + '\n')


def pytest_collection_modifyitems(items):
    for item in items:
        module = item.module
        if getattr(module, '_history_preparation_timed', False):
            continue
        if module.__name__.endswith('test_report_selection'):
            module._history_preparation_timed = True
            module._history_preparation_seconds = 0.0
            original_submit = module._submit_many
            original_finish = module._finish_many

            @functools.wraps(original_submit)
            def submit(owned, count, request_id):
                started = time.perf_counter()
                _write({'phase': 'accept_actions', 'count': count, 'request_id': request_id, 'state': 'started'})
                try:
                    return original_submit(owned, count, request_id)
                finally:
                    elapsed = time.perf_counter() - started
                    module._history_preparation_seconds += elapsed
                    _write({'phase': 'accept_actions', 'count': count, 'request_id': request_id, 'seconds': elapsed, 'state': 'finished'})

            @functools.wraps(original_finish)
            async def finish(owned, count, *, action_id=1):
                started = time.perf_counter()
                _write({'phase': 'finish_outputs', 'count': count, 'state': 'started'})
                try:
                    return await original_finish(owned, count, action_id=action_id)
                finally:
                    elapsed = time.perf_counter() - started
                    module._history_preparation_seconds += elapsed
                    _write({'phase': 'finish_outputs', 'count': count, 'seconds': elapsed, 'state': 'finished'})

            module._submit_many = submit
            module._finish_many = finish


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    started = time.perf_counter()
    _write({'phase': 'call', 'nodeid': item.nodeid, 'state': 'started'})
    outcome = yield
    elapsed = time.perf_counter() - started
    preparation = getattr(item.module, '_history_preparation_seconds', 0.0)
    _write({'phase': 'call', 'nodeid': item.nodeid, 'state': 'finished',
            'seconds': elapsed, 'preparation_seconds': preparation,
            'checks_seconds': elapsed - preparation,
            'failed': outcome.excinfo is not None})
