"""并行入口的真实进程契约测试；不运行产品集成套件。"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

RUNNER = Path(__file__).resolve().parents[1] / 'run-integration.py'


class IntegrationRunnerTest(unittest.TestCase):
    def test_isolated_concurrency_progress_exact_assignment_and_failure(self):
        # 删除隔离、重复调度、隐藏失败或仅用日志尾行表示进度，都会破坏这些行为。
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'input'
            source.mkdir()
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            tests = source / 'apps/camctl/tests/integration'
            for name, assertion in [('first', 'True'), ('second', 'False')]:
                directory = tests / name
                directory.mkdir(parents=True)
                (directory / 'test_world.py').write_text(
                    'import time\nfrom pathlib import Path\n'
                    'def test_world():\n'
                    '    resource = Path("apps/camctl/src/camctl/_resources/sentinel")\n'
                    '    resource.parent.mkdir(parents=True, exist_ok=True)\n'
                    f'    resource.write_text({name!r})\n'
                    '    time.sleep(2)\n'
                    f'    assert resource.read_text() == {name!r}\n'
                    f'    assert {assertion}\n', encoding='utf-8')
            subprocess.run(['git', 'add', '.'], cwd=source, check=True)
            subprocess.run(['git', '-c', 'user.name=runner-test', '-c',
                            'user.email=runner@example.invalid', 'commit', '-qm', 'fixture'],
                           cwd=source, check=True)
            cache = root / 'cache'
            cache.mkdir()
            output = root / 'output'
            command = [sys.executable, str(RUNNER), '--source', str(source),
                       '--python', sys.executable, '--uv-cache', str(cache),
                       '--output', str(output), '--jobs', '2', '--scope', 'camctl',
                       '--target', 'apps/camctl/tests/integration/first/test_world.py',
                       '--target', 'apps/camctl/tests/integration/first/test_world.py::test_world',
                       '--target', 'apps/camctl/tests/integration/second']
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            observed = False
            deadline = time.monotonic() + 30
            while process.poll() is None and time.monotonic() < deadline:
                summary = output / 'summary.json'
                if summary.exists():
                    state = json.loads(summary.read_text())
                    running = [task for task in state['tasks'] if task['status'] == 'running']
                    if len(running) == 2 and all(task.get('progress', {}).get('nodeid') for task in running):
                        observed = True
                        break
                time.sleep(.05)
            stdout, _ = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 1, stdout)
            self.assertTrue(observed, stdout)
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(len(summary['tasks']), 2)
            self.assertEqual(sorted(task['exit_code'] for task in summary['tasks']), [0, 1])
            self.assertEqual(len({task['cwd'] for task in summary['tasks']}), 2)
            for task in summary['tasks']:
                inventory = json.loads((Path(task['artifacts']) / 'inventory.json').read_text())
                self.assertEqual(len(inventory), 1)
                self.assertEqual(task['progress']['status'], 'finished')
            self.assertFalse((source / 'apps/camctl/src/camctl/_resources').exists())

    def test_nested_unittest_checks_do_not_inflate_primary_results(self):
        # 将子检查当成独立 pytest 实例会让结果总数超过真实收集清单。
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'input'
            directory = source / 'apps/camctl/tests/integration/matrix'
            directory.mkdir(parents=True)
            (directory / 'test_matrix.py').write_text(
                'import unittest\nimport pytest\n'
                'class Matrix(unittest.TestCase):\n'
                '    def test_matrix(self):\n'
                '        for number in (1, 2):\n'
                '            with self.subTest(number=number):\n'
                '                self.assertGreater(number, 0)\n'
                '@pytest.fixture\ndef broken_teardown():\n'
                '    yield\n    raise RuntimeError("teardown")\n'
                'def test_both_failures(broken_teardown):\n    assert False\n', encoding='utf-8')
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            subprocess.run(['git', 'add', '.'], cwd=source, check=True)
            subprocess.run(['git', '-c', 'user.name=runner-test', '-c',
                            'user.email=runner@example.invalid', 'commit', '-qm', 'fixture'],
                           cwd=source, check=True)
            cache = root / 'cache'
            cache.mkdir()
            output = root / 'output'
            result = subprocess.run([sys.executable, str(RUNNER), '--source', str(source),
                                     '--scope', 'camctl', '--python', sys.executable,
                                     '--uv-cache', str(cache), '--output', str(output), '--jobs', '1'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            task = json.loads((output / 'summary.json').read_text())['tasks'][0]
            self.assertEqual(task['progress']['collected'], 2)
            self.assertEqual(task['progress']['outcomes'], {'passed': 1, 'failed': 1})
            self.assertEqual(task['progress']['subchecks'], {'passed': 2})

    def test_function_selector_already_contains_parameter_instances(self):
        target = 'tests/integration/test_osmo_builtin_timelapse_roundtrip.py::test_installed_osmo_capture_recovery_and_available_delivery'
        result = subprocess.run([sys.executable, str(RUNNER), '--list', '--target', target,
                                 '--target', target + '[record-preview]'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]['targets'], [target])

    def test_unknown_target_is_rejected_before_execution(self):
        result = subprocess.run([sys.executable, str(RUNNER), '--list', '--target',
                                 'tests/integration/does_not_exist.py'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('does_not_exist.py', result.stderr)


if __name__ == '__main__':
    unittest.main()
