"""仅用于本轮窄范围计时；不改测试输入、规模、断言或生产设置。"""
import functools
import inspect
import json
import os
from pathlib import Path
import time
import pytest

_CURRENT = None
_OUT = Path(os.environ['QUALITY_PHASE_FILE'])

def emit(stage, seconds, args=()):
    with _OUT.open('a') as stream:
        stream.write(json.dumps({'nodeid': _CURRENT, 'stage': stage, 'seconds': seconds,
                                 'detail': args}, ensure_ascii=False, default=str) + '\n')

def wrap(module, name, label, details=None):
    old = getattr(module, name)
    def detail(args, kwargs):
        return details(args, kwargs) if details else ()
    if inspect.iscoroutinefunction(old):
        @functools.wraps(old)
        async def timed(*args, **kwargs):
            start = time.perf_counter()
            try:
                return await old(*args, **kwargs)
            finally:
                emit(label, time.perf_counter()-start, detail(args, kwargs))
    elif inspect.isgeneratorfunction(old):
        @functools.wraps(old)
        def timed(*args, **kwargs):
            start = time.perf_counter()
            try:
                yield from old(*args, **kwargs)
            finally:
                emit(label, time.perf_counter()-start, detail(args, kwargs))
    else:
        @functools.wraps(old)
        def timed(*args, **kwargs):
            start = time.perf_counter()
            try:
                return old(*args, **kwargs)
            finally:
                emit(label, time.perf_counter()-start, detail(args, kwargs))
    setattr(module, name, timed)


def pytest_collection_modifyitems(items):
    modules = {item.module for item in items}
    for module in modules:
        if module.__name__.endswith('test_report_selection'):
            wrap(module, '_submit_many', 'prepare.submit_many', lambda a,k: {'count': a[1], 'request': a[2]})
            wrap(module, '_finish_many', 'prepare.finish_many', lambda a,k: {'count': a[1]})
            wrap(module, '_read', 'target.read_all', lambda a,k: {'entity': a[1], 'parent': a[2] if len(a)>2 else None})
        if module.__name__.endswith('test_record_segment_history'):
            wrap(module, '_large_fixture', 'prepare.large_fixture', lambda a,k: {'members': a[1]})
            wrap(module, '_rows', 'target.rows', lambda a,k: {'table': a[1]})
        if module.__name__.endswith('test_read_discovery_default'):
            wrap(module, '_save_photos', 'prepare.save_photos')
            wrap(module, '_accept', 'prepare_or_inflight.accept', lambda a,k: {'request': a[1]})
            from camctl.bootstrap import lifecycle
            wrap(lifecycle, 'build_runtime', 'target.build_runtime')
            wrap(lifecycle, 'execute_command', 'target.execute_command')
            wrap(lifecycle, 'close_runtime', 'target.close_runtime')
    if any(m.__name__.endswith('test_record_segment_history') for m in modules):
        from camctl.history import snapshots
        from camctl.persistence.repositories.history import HistoryRepository
        wrap(snapshots, 'prepare_snapshot', 'target.prepare_snapshot')
        wrap(HistoryRepository, 'open_segment_history', 'target.open_segment_history')

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global _CURRENT
    _CURRENT = item.nodeid
    yield

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    start = time.perf_counter()
    yield
    emit('pytest.setup', time.perf_counter()-start)

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    start = time.perf_counter()
    yield
    emit('pytest.call', time.perf_counter()-start)

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    start = time.perf_counter()
    yield
    emit('pytest.teardown', time.perf_counter()-start)
