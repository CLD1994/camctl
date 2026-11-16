"""通过真实 pytest 验证测试类别与标记表达式的组合。"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1]


class TestKindSelection(unittest.TestCase):
    def test_component_root_loads_selector_without_explicit_plugin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / 'scripts'
            project = root / 'apps/camctl'
            scripts.mkdir()
            project.mkdir(parents=True)
            for name in ('test_kind_plugin.py', 'camctl_memory_plugin.py', 'camctl_memory.py'):
                shutil.copy2(SCRIPTS / name, scripts / name)
            shutil.copy2(SCRIPTS.parent / 'apps/camctl/conftest.py', project / 'conftest.py')
            (project / 'pyproject.toml').write_text('[tool.pytest.ini_options]\n')
            (project / 'test_kind.py').write_text(
                'import pytest\ndef test_functional(): pass\n'
                '@pytest.mark.performance\ndef test_perf(): pass\n')
            environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
            environment.pop('PYTHONPATH', None)
            for options, selected in (([], 'test_functional'), (['--test-kind=performance'], 'test_perf')):
                result = subprocess.run([sys.executable, '-m', 'pytest', str(project / 'test_kind.py'),
                                         '--collect-only', '-q', *options], cwd=root,
                                        env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                nodes = [line.rsplit('::', 1)[-1] for line in result.stdout.splitlines() if '::' in line]
                self.assertEqual(nodes, [selected])

    def test_selection_and_mark_expression(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'test_mixed.py').write_text(
                'import pytest\n'
                'def test_functional(): pass\n'
                '@pytest.mark.performance\ndef test_perf(): pass\n'
                '@pytest.mark.acceptance\ndef test_accept(): pass\n'
                '@pytest.mark.acceptance\nclass TestAccept:\n'
                '    def test_accept_member(self): pass\n'
                '@pytest.mark.performance\nclass TestPerf:\n'
                '    def test_member(self): pass\n'
            )
            (root / 'test_module.py').write_text(
                'import pytest\npytestmark = pytest.mark.performance\n'
                'def test_module(): pass\n'
            )
            (root / 'test_accept_module.py').write_text(
                'import pytest\npytestmark = pytest.mark.acceptance\n'
                'def test_accept_module(): pass\n'
            )
            environment = dict(os.environ, PYTHONPATH=str(SCRIPTS), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
            for options, expected, code in (
                ([], ['test_functional'], 0),
                (['--test-kind=performance'], ['test_perf', 'test_member', 'test_module'], 0),
                (['--test-kind=acceptance'], ['test_accept', 'test_accept_member', 'test_accept_module'], 0),
                (['--test-kind=all'], ['test_functional', 'test_perf', 'test_member', 'test_module', 'test_accept', 'test_accept_member', 'test_accept_module'], 0),
                (['--test-kind=acceptance', '-m', 'performance'], [], 5),
                (['--test-kind=all', '-m', 'acceptance'], ['test_accept', 'test_accept_member', 'test_accept_module'], 0),
                (['-m', 'performance'], [], 5),
                (['--test-kind=all', '-m', 'performance'], ['test_perf', 'test_member', 'test_module'], 0),
            ):
                with self.subTest(options=options):
                    result = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'test_kind_plugin',
                                             '--collect-only', '-q', *options], cwd=root,
                                            env=environment, capture_output=True, text=True)
                    self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                    nodes = [line.rsplit('::', 1)[-1] for line in result.stdout.splitlines() if '::' in line]
                    self.assertCountEqual(nodes, expected)


if __name__ == '__main__':
    unittest.main()
