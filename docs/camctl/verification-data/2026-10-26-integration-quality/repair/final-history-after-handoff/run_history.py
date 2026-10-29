"""沿仓库原隔离运行器执行两项历史验收，仅补充原生 JUnit XML 收据。"""
import importlib.util
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
evidence = Path(__file__).resolve().parent
runner = repo / 'scripts/run-integration.py'
spec = importlib.util.spec_from_file_location('integration_runner', runner)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
sys.path.insert(0, str(runner.parent))
spec.loader.exec_module(module)
original_pytest_arguments = module.memory_pytest_arguments


def pytest_arguments_with_xml(args):
    return [*original_pytest_arguments(args), '--junitxml', str(evidence / 'pytest-results.xml'),
            '-o', 'junit_logging=system-out', '-o', 'junit_log_passing_tests=true']


module.memory_pytest_arguments = pytest_arguments_with_xml
sys.argv = [str(runner), '--source', str(repo), '--scope', 'camctl', '--jobs', '1',
            '--python', sys.executable, '--camctl-memory-limit', '2GiB',
            '--target', 'apps/camctl/tests/integration/history/test_report_selection.py::test_large_window_releases_overflow_once_and_keyset_pages_do_not_rescan_window',
            '--target', 'apps/camctl/tests/integration/history/test_record_segment_history.py::test_large_growth_crosses_page_and_disk_cache_capacity',
            '--output', str(evidence / 'run')]
sys.exit(module.main())
