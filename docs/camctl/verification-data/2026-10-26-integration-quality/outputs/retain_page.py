"""让真实发现器错误保留已返回页，只验证现有集成断言的证伪能力。"""
import cProfile
from pathlib import Path
import sys

import pytest

from camctl.outputs.read_discovery import ReadDiscoveryOwner


class RetainPage:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        real_reads = ReadDiscoveryOwner.reads

        def retained_reads(owner, *args, **kwargs):
            page = real_reads(owner, *args, **kwargs)
            owner._quality_retained_page = page
            return page

        ReadDiscoveryOwner.reads = retained_reads
        try:
            yield
        finally:
            ReadDiscoveryOwner.reads = real_reads


profiler = cProfile.Profile()
result = profiler.runcall(pytest.main, sys.argv[1:], plugins=[RetainPage()])
profiler.dump_stats(str(Path(__file__).with_name('retained-page.prof')))
raise SystemExit(result)
