"""并行入口的真实进程契约测试；不运行产品集成套件。"""
from __future__ import annotations

import json
import signal
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
                       '--target', './apps/camctl/tests/integration/second/',
                       '--target', 'apps/camctl/tests/integration/second/test_world.py::test_world']
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
                '    def test_only_nested_failure(self):\n'
                '        with self.subTest(case="first"):\n            self.fail("nested")\n'
                '        with self.subTest(case="later"):\n            self.assertTrue(True)\n'
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
            self.assertEqual(task['progress']['collected'], 3)
            self.assertEqual(task['progress']['outcomes'], {'passed': 1, 'failed': 2})
            self.assertEqual(task['progress']['subchecks'], {'passed': 3, 'failed': 1})

    def test_function_selector_already_contains_parameter_instances(self):
        target = 'tests/integration/test_osmo_builtin_timelapse_roundtrip.py::test_installed_osmo_capture_recovery_and_available_delivery'
        result = subprocess.run([sys.executable, str(RUNNER), '--list', '--target', target,
                                 '--target', target + '[record-preview]', '--target', './' + target,
                                 '--target', 'tests/integration/../integration/' + target.rsplit('/', 1)[-1]], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]['targets'], [target])

    def test_hasselblad_uses_built_external_candidate_from_current_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'input'
            self._fixture_repository(source, 'test_hasselblad_photo_roundtrip.py',
                'import os\nfrom pathlib import Path\n'
                'def test_candidate():\n'
                '    repo = Path(__file__).resolve().parents[2]\n'
                '    candidate = Path(os.environ["CAMCTL_HB_TEST_CLIENT_ROOT"]).resolve()\n'
                '    assert candidate != repo and repo not in candidate.parents\n'
                '    assert (candidate / "apps/client/dist/web/built").read_text() == "current"\n'
                '    assert (candidate / "protocol/marker").read_text() == "current"\n'
                '    assert (candidate / "tests/integration/client_driver.ts").read_text() == "current"\n'
                '    (candidate / "protocol/marker").write_text("changed")\n'
                '    assert (repo / "protocol/marker").read_text() == "current"\n')
            result, output = self._run_fixture(root, source)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads((output / 'summary.json').read_text())['tasks'][0]['progress']['outcomes'], {'passed': 1})

    def test_root_shards_use_current_collection_including_unmeasured_nodes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'input'
            self._fixture_repository(source, 'test_matrix.py',
                'import pytest\n'
                '@pytest.mark.parametrize("value", range(7), ids=["a","b","c","d","e","f","new"])\n'
                'def test_value(value):\n    assert value >= 0\n',
                history=True)
            result, output = self._run_fixture(root, source, '--root-shards', '2')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            plan = json.loads((output / 'plan.json').read_text())
            self.assertEqual(len(plan), 2)
            nodes = [node for task in plan for node in task['targets']]
            expected = ['tests/integration/test_matrix.py::test_value[' + name + ']' for name in ('a','b','c','d','e','f','new')]
            self.assertCountEqual(nodes, expected)
            self.assertEqual(len(nodes), len(set(nodes)))
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(sum(task['progress']['collected'] for task in summary['tasks']), 7)

    def _fixture_repository(self, source, filename, content, history=False):
        for directory in ('apps/camctl/tests/integration', 'apps/client', 'protocol', 'tests/integration'):
            (source / directory).mkdir(parents=True, exist_ok=True)
        (source / 'apps/camctl/tests/integration/.gitkeep').touch()
        (source / 'tests/integration' / filename).write_text(content)
        (source / 'tests/integration/client_driver.ts').write_text('current')
        (source / 'protocol/marker').write_text('current')
        (source / 'apps/client/package.json').write_text(json.dumps({'scripts': {'build': 'node build.cjs'}}))
        (source / 'apps/client/build.cjs').write_text(
            "require('fs').mkdirSync('dist/web',{recursive:true});"
            "require('fs').writeFileSync('dist/web/built','current')")
        if history:
            timing = source / 'docs/camctl/verification-data/2026-10-25-integration-timing'
            timing.mkdir(parents=True)
            (timing / 'cross-component-files.csv').write_text('name,total_s\ntests/integration/test_matrix.py,930\n')
            (timing / 'cross-component-cases.csv').write_text('nodeid,total_s\n' + ''.join(
                f'tests/integration/test_matrix.py::test_value[{name}],{seconds}\n'
                for name, seconds in zip(('a','b','c','d','e','f'), (300,240,180,120,60,30))))
        subprocess.run(['git', 'init', '-q', str(source)], check=True)
        subprocess.run(['git', 'add', '.'], cwd=source, check=True)
        subprocess.run(['git', '-c', 'user.name=runner-test', '-c', 'user.email=runner@example.invalid', 'commit', '-qm', 'fixture'], cwd=source, check=True)

    def _run_fixture(self, root, source, *options):
        cache, modules, output = root / 'cache', root / 'node_modules', root / 'output'
        cache.mkdir()
        modules.mkdir()
        result = subprocess.run([sys.executable, str(RUNNER), '--source', str(source), '--python', sys.executable,
                                 '--uv-cache', str(cache), '--node-modules', str(modules), '--output', str(output),
                                 '--jobs', '2', *options], capture_output=True, text=True)
        return result, output

    def test_prepare_interrupt_reaps_owned_process_and_saves_actual_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            self._fixture_repository(source, 'test_wait.py', 'def test_wait():\n    assert True\n')
            script = source / 'apps/camctl/scripts/sync_resources.py'
            script.parent.mkdir()
            script.write_text('import time\ntime.sleep(60)\n')
            cache, modules, output = root / 'cache', root / 'node_modules', root / 'output'
            cache.mkdir()
            modules.mkdir()
            process = subprocess.Popen([sys.executable, str(RUNNER), '--source', str(source), '--python', sys.executable,
                '--uv-cache', str(cache), '--node-modules', str(modules), '--output', str(output), '--jobs', '1'],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            pid = None
            deadline = time.monotonic() + 15
            while process.poll() is None and time.monotonic() < deadline:
                if (output / 'summary.json').exists():
                    state = json.loads((output / 'summary.json').read_text())
                    pid = state['tasks'][0].get('preparation_pid')
                    if pid:
                        break
                time.sleep(.05)
            self.assertIsNotNone(pid)
            process.send_signal(signal.SIGINT)
            stdout, _ = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 130, stdout)
            self.assertFalse(Path(f'/proc/{pid}').exists(), stdout)
            self.assertEqual(json.loads((output / 'summary.json').read_text())['status'], 'interrupted')

    def test_snapshot_reports_unsupported_symbolic_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            self._fixture_repository(source, 'test_simple.py', 'def test_simple():\n    assert True\n')
            outside = root / 'outside'
            outside.write_text('external')
            (source / 'protocol/link').symlink_to(outside)
            result, _ = self._run_fixture(root, source)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn('protocol/link', result.stderr)
            self.assertEqual(outside.read_text(), 'external')

    def test_parent_path_cannot_escape_source(self):
        result = subprocess.run([sys.executable, str(RUNNER), '--list', '--target', '../outside/test.py'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('../outside/test.py', result.stderr)

    def test_unknown_target_is_rejected_before_execution(self):
        result = subprocess.run([sys.executable, str(RUNNER), '--list', '--target',
                                 'tests/integration/does_not_exist.py'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('does_not_exist.py', result.stderr)


if __name__ == '__main__':
    unittest.main()
