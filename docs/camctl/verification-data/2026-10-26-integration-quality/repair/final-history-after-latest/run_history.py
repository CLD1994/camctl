"""在同一冻结源码中顺序运行原两项容量和直接相关历史用例。"""
import importlib.util
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
evidence = Path(__file__).resolve().parent
prior = Path('/tmp/camctl-quality-repair-evidence/final-history')
related = json.loads((prior / 'related-targets.json').read_text())['targets']
primary = [
    'apps/camctl/tests/integration/history/test_report_selection.py::test_large_window_releases_overflow_once_and_keyset_pages_do_not_rescan_window',
    'apps/camctl/tests/integration/history/test_record_segment_history.py::test_large_growth_crosses_page_and_disk_cache_capacity',
]
targets = primary + related
(evidence / 'targets.json').write_text(json.dumps({'primary':primary,'related':related},indent=2)+'\n')
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
            '--output', str(evidence / 'run')]
for target in targets:
    sys.argv.extend(('--target',target))
sys.exit(module.main())
