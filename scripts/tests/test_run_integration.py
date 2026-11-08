"""并行入口的真实进程契约测试；不运行产品集成套件。"""
from __future__ import annotations

import errno
import importlib.util
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

RUNNER = Path(__file__).resolve().parents[1] / 'run-integration.py'


class IntegrationRunnerTest(unittest.TestCase):
    def test_success_releases_readonly_trees_without_changing_external_symlink_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            external = root / 'external'
            external.mkdir()
            sentinel = external / 'sentinel'
            sentinel.write_text('external content')
            sentinel.chmod(0o400)
            external.chmod(0o500)
            try:
                self._fixture_repository(source, 'test_readonly.py',
                    'import os\nfrom pathlib import Path\n'
                    'def test_readonly(tmp_path):\n'
                    '    for base in (Path.cwd(), tmp_path, Path(os.environ["UV_CACHE_DIR"])):\n'
                    '        directory = base / "owned-readonly"\n'
                    '        directory.mkdir()\n'
                    '        (directory / "request.json").write_text("owned")\n'
                    '        (directory / "request.json").chmod(0o400)\n'
                    f'        (directory / "external").symlink_to({str(external)!r}, target_is_directory=True)\n'
                    '        locked = directory / "locked"\n'
                    '        locked.mkdir()\n'
                    '        (locked / "value").write_text("owned")\n'
                    '        locked.chmod(0o000)\n'
                    '        directory.chmod(0o500)\n')
                original = (external.stat().st_mode, sentinel.stat().st_mode, sentinel.read_bytes())
                result, output = self._run_fixture(root, source)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                task, = json.loads((output / 'summary.json').read_text())['tasks']
                self.assertEqual((task['status'], task['exit_code']), ('passed', 0))
                self.assertEqual(task['progress']['outcomes'], {'passed': 1})
                self.assertFalse(Path(task['cwd']).exists())
                self.assertFalse((Path(task['cwd']).parent / 'tmp').exists())
                self.assertFalse(Path(task['worker_cache']).exists())
                self.assertEqual((external.stat().st_mode, sentinel.stat().st_mode, sentinel.read_bytes()), original)
            finally:
                external.chmod(0o700)

    def test_cleanup_propagates_real_non_permission_error(self):
        spec = importlib.util.spec_from_file_location('integration_runner_cleanup', RUNNER)
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(RUNNER.parent))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / 'not-a-directory'
            file.write_text('keep')
            with self.assertRaises(NotADirectoryError) as caught:
                module.remove_owned_tree(file)
            self.assertEqual(caught.exception.errno, errno.ENOTDIR)
            self.assertEqual(file.read_text(), 'keep')

    def test_failed_task_keeps_readonly_source_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            self._fixture_repository(source, 'test_fails.py',
                'from pathlib import Path\n'
                'def test_fails():\n'
                '    directory = Path.cwd() / "owned-readonly"\n'
                '    directory.mkdir()\n'
                '    (directory / "evidence").write_text("preserve")\n'
                '    directory.chmod(0o500)\n'
                '    assert False\n')
            retained = None
            try:
                result, output = self._run_fixture(root, source)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                task, = json.loads((output / 'summary.json').read_text())['tasks']
                self.assertEqual((task['status'], task['exit_code']), ('failed', 1))
                retained = Path(task['cwd']) / 'owned-readonly'
                self.assertEqual(retained.stat().st_mode & 0o777, 0o500)
                self.assertEqual((retained / 'evidence').read_text(), 'preserve')
                self.assertTrue((Path(task['cwd']).parent / 'tmp').is_dir())
            finally:
                if retained is not None and retained.exists():
                    retained.chmod(0o700)

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
                    f'    time.sleep({1 if name == "first" else 4})\n'
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
            released_while_other_runs = False
            while process.poll() is None and time.monotonic() < deadline:
                state = json.loads((output / 'summary.json').read_text())
                passed = [task for task in state['tasks'] if task['status'] == 'passed']
                running = [task for task in state['tasks'] if task['status'] == 'running']
                if passed and running and not Path(passed[0]['cwd']).exists():
                    released_while_other_runs = True
                    self.assertTrue(Path(running[0]['cwd']).exists())
                    self.assertTrue(Path(running[0]['worker_cache']).exists())
                    break
                time.sleep(.05)
            stdout, _ = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 1, stdout)
            self.assertTrue(observed, stdout)
            self.assertTrue(released_while_other_runs, stdout)
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(len(summary['tasks']), 2)
            self.assertEqual(sorted(task['exit_code'] for task in summary['tasks']), [0, 1])
            self.assertEqual(len({task['cwd'] for task in summary['tasks']}), 2)
            for task in summary['tasks']:
                inventory = json.loads((Path(task['artifacts']) / 'inventory.json').read_text())
                self.assertEqual(len(inventory), 1)
                self.assertEqual(task['progress']['status'], 'finished')
                self.assertEqual(Path(task['cwd']).exists(), task['status'] == 'failed')
                self.assertFalse(Path(task['worker_cache']).exists())
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
            task = json.loads((output / 'summary.json').read_text())['tasks'][0]
            self.assertEqual(task['progress']['outcomes'], {'passed': 1})
            self.assertFalse(Path(task['client_candidate']).exists())
            self.assertFalse(Path(task['cwd']).exists())
            self.assertFalse(Path(task['worker_cache']).exists())
            self.assertFalse((output / 'snapshot').exists())

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
            self.assertFalse(any((output / 'collection').glob('*/source')))
            self.assertTrue(Path(plan[0]['collection'], 'inventory.json').is_file())

    def test_failed_worker_cache_is_reused_then_removed_after_later_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            self._fixture_repository(source, 'test_unused.py', 'def test_unused():\n    assert True\n')
            for name, value in (('a_failed', 'False'), ('b_passed', 'True')):
                path = source / 'apps/camctl/tests/integration' / name
                path.mkdir()
                (path / 'test_task.py').write_text(f'def test_task():\n    assert {value}\n')
            cache, output = root / 'cache', root / 'output'
            cache.mkdir()
            result = subprocess.run([sys.executable, str(RUNNER), '--source', str(source), '--python', sys.executable,
                '--uv-cache', str(cache), '--output', str(output), '--scope', 'camctl', '--jobs', '1'],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            failed, passed = json.loads((output / 'summary.json').read_text())['tasks']
            self.assertEqual((failed['status'], passed['status']), ('failed', 'passed'))
            self.assertEqual(failed['worker_cache'], passed['worker_cache'])
            self.assertFalse(Path(failed['worker_cache']).exists())
            self.assertTrue(Path(failed['cwd']).is_dir())
            self.assertFalse(Path(passed['worker_cache']).exists())
            self.assertFalse(Path(passed['cwd']).exists())

    def test_existing_external_budget_path_keeps_root_targets_in_each_task_source(self):
        for shards in (1, 2):
            with self.subTest(shards=shards), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / 'input'
                self._fixture_repository(source, 'test_matrix.py',
                    'import json\nfrom pathlib import Path\nimport pytest\n'
                    '@pytest.mark.parametrize("value", ["a", "b", "new"])\n'
                    'def test_value(value, pytestconfig):\n'
                    '    work = Path.cwd().resolve()\n'
                    '    assert pytestconfig.rootpath == work\n'
                    '    assert Path(__file__).resolve().is_relative_to(work)\n'
                    '    marker = work.parent / "artifacts/executed"\n'
                    '    marker.mkdir(exist_ok=True)\n'
                    '    (marker / (value + ".json")).write_text(json.dumps({\n'
                    '        "file": str(Path(__file__).resolve()), "rootdir": str(pytestconfig.rootpath)}))\n',
                    history=True)
                # pytest 在插件选项解析前可把已存在的外部目录纳入 rootdir 推断。
                # 不启动受限 camctl；真实参数仍经预算插件传入并完成配置。
                delegated = root / 'external-budget-directory'
                delegated.mkdir()
                result, output = self._run_fixture(root, source, '--root-shards', str(shards),
                    '--camctl-memory-cgroup-root', str(delegated))
                plan = json.loads((output / 'plan.json').read_text())
                expected = ['tests/integration/test_matrix.py::test_value[' + name + ']'
                            for name in ('a', 'b', 'new')]
                if shards == 2:
                    self.assertCountEqual([node for task in plan for node in task['targets']], expected)
                    collected = json.loads((Path(plan[0]['collection']) / 'inventory.json').read_text())
                    self.assertCountEqual(collected, expected)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                summary = json.loads((output / 'summary.json').read_text())
                tasks = summary['tasks']
                self.assertEqual(len(tasks), shards)
                self.assertEqual(len({task['cwd'] for task in tasks}), shards)
                actual = []
                for task in tasks:
                    work = Path(task['cwd']).resolve()
                    for target in task['targets']:
                        file = Path(target.partition('::')[0])
                        self.assertFalse(file.is_absolute())
                        self.assertTrue((work / file).resolve().is_relative_to(work))
                    inventory = json.loads((Path(task['artifacts']) / 'inventory.json').read_text())
                    self.assertEqual(task['progress']['outcomes'], {'passed': len(inventory)})
                    self.assertFalse(work.exists())
                    markers = list((Path(task['artifacts']) / 'executed').glob('*.json'))
                    self.assertEqual(len(markers), len(inventory))
                    for marker in markers:
                        receipt = json.loads(marker.read_text())
                        self.assertEqual(Path(receipt['rootdir']), work)
                        self.assertEqual(Path(receipt['file']), work / 'tests/integration/test_matrix.py')
                    actual.extend(inventory)
                self.assertCountEqual(actual, expected)
                self.assertEqual(len(actual), len(set(actual)))
                self.assertFalse((source / 'executed').exists())
                self.assertFalse(any((output / 'collection').glob('*/source/executed')))

    def test_failed_short_history_shards_current_nodes_but_passed_history_stays_whole(self):
        for historical_status, expected_tasks in (('failed', 2), ('passed', 1)):
            with self.subTest(historical_status=historical_status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / 'input'
                self._fixture_repository(source, 'test_matrix.py',
                    'import pytest\n'
                    '@pytest.mark.parametrize("value", range(3), ids=["a","b","new"])\n'
                    'def test_value(value):\n    assert value >= 0\n', history=True)
                timing = source / 'docs/camctl/verification-data/2026-10-25-integration-timing'
                (timing / 'cross-component-files.csv').write_text('name,total_s\ntests/integration/test_matrix.py,6\n')
                (timing / 'cross-component-cases.csv').write_text(
                    'nodeid,status,total_s\n'
                    'tests/integration/test_matrix.py::test_value[a],passed,5\n'
                    f'tests/integration/test_matrix.py::test_value[b],{historical_status},1\n')
                result, output = self._run_fixture(root, source, '--root-shards', '2')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                plan = json.loads((output / 'plan.json').read_text())
                self.assertEqual(len(plan), expected_tasks)
                summary = json.loads((output / 'summary.json').read_text())
                inventory = [node for task in summary['tasks'] for node in json.loads(
                    (Path(task['artifacts']) / 'inventory.json').read_text())]
                expected = ['tests/integration/test_matrix.py::test_value[' + name + ']' for name in ('a', 'b', 'new')]
                self.assertCountEqual(inventory, expected)
                self.assertEqual(len(inventory), len(set(inventory)))
                self.assertTrue(all(task['historical_failure_observed'] == (historical_status == 'failed') for task in plan))
                if historical_status == 'passed':
                    self.assertFalse((output / 'collection').exists())

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
            (timing / 'cross-component-cases.csv').write_text('nodeid,status,total_s\n' + ''.join(
                f'tests/integration/test_matrix.py::test_value[{name}],passed,{seconds}\n'
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
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(summary['status'], 'interrupted')
            task = summary['tasks'][0]
            self.assertTrue(Path(task['cwd']).exists())
            self.assertFalse(Path(task['worker_cache']).exists())

    def test_interrupt_waits_for_cache_copy_before_removing_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            self._fixture_repository(source, 'test_wait.py', 'def test_wait():\n    assert True\n')
            cache, modules, output, binaries = root / 'cache', root / 'node_modules', root / 'output', root / 'bin'
            for path in (cache, modules, binaries):
                path.mkdir()
            (cache / 'item').write_text('cached')
            wrapper = binaries / 'cp'
            wrapper.write_text('#!/bin/sh\nset -e\ntouch "$COPY_STARTED"\nsleep 2\n/usr/bin/cp "$@"\n'
                               'cmp "$COPY_SOURCE" "$COPY_TARGET"\ntouch "$COPY_FINISHED"\n')
            wrapper.chmod(0o755)
            environment = os.environ.copy()
            environment.update(PATH=str(binaries) + os.pathsep + environment['PATH'],
                               COPY_STARTED=str(root / 'started'), COPY_FINISHED=str(root / 'finished'),
                               COPY_SOURCE=str(cache / 'item'), COPY_TARGET=str(output / 'workers/0/uv-cache/item'))
            process = subprocess.Popen([sys.executable, str(RUNNER), '--source', str(source), '--python', sys.executable,
                '--uv-cache', str(cache), '--node-modules', str(modules), '--output', str(output), '--jobs', '1'],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=environment)
            deadline = time.monotonic() + 15
            while process.poll() is None and not (root / 'started').exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue((root / 'started').exists())
            process.send_signal(signal.SIGINT)
            stdout, _ = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 130, stdout)
            self.assertTrue((root / 'finished').exists(), stdout)
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(summary['status'], 'interrupted')
            self.assertFalse(Path(summary['tasks'][0]['worker_cache']).exists())

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

    def test_collection_prepare_interrupt_reaps_owned_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            self._fixture_repository(source, 'test_matrix.py', 'def test_value():\n    assert True\n', history=True)
            script = source / 'apps/camctl/scripts/sync_resources.py'
            script.parent.mkdir()
            script.write_text('import time\ntime.sleep(60)\n')
            cache, output = root / 'cache', root / 'output'
            cache.mkdir()
            process = subprocess.Popen([
                sys.executable, str(RUNNER), '--source', str(source), '--python', sys.executable,
                '--uv-cache', str(cache), '--output', str(output), '--jobs', '2', '--plan-only',
            ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            pid = None
            deadline = time.monotonic() + 15
            while process.poll() is None and time.monotonic() < deadline:
                receipts = list(output.glob('collection/*/artifacts/prepare-command.json'))
                if receipts:
                    pid = json.loads(receipts[0].read_text())['pid']
                    break
                time.sleep(.05)
            self.assertIsNotNone(pid)
            process.send_signal(signal.SIGINT)
            stdout, _ = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 130, stdout)
            self.assertFalse(Path(f'/proc/{pid}').exists(), stdout)
            self.assertEqual(json.loads((output / 'summary.json').read_text())['status'], 'interrupted')

    def test_parent_path_cannot_escape_source(self):
        result = subprocess.run([sys.executable, str(RUNNER), '--list', '--target', '../outside/test.py'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('../outside/test.py', result.stderr)

    def test_observer_keeps_artifacts_when_product_file_operations_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            self._fixture_repository(source, 'test_product_fault.py',
                'import builtins\nimport os\nfrom pathlib import Path\nimport pytest\n'
                '@pytest.mark.parametrize("fault", ["replace","path-open","path-write","builtin-open"])\n'
                'def test_expected_product_failure(monkeypatch, tmp_path, fault):\n'
                '    def denied(*args, **kwargs):\n        raise PermissionError("product fault")\n'
                '    if fault == "replace":\n'
                '        monkeypatch.setattr(os, "replace", denied)\n'
                '        operation = lambda: os.replace(tmp_path / "a", tmp_path / "b")\n'
                '    elif fault == "path-open":\n'
                '        monkeypatch.setattr(Path, "open", denied)\n'
                '        operation = lambda: (tmp_path / "a").open("w")\n'
                '    elif fault == "path-write":\n'
                '        monkeypatch.setattr(Path, "write_text", denied)\n'
                '        operation = lambda: (tmp_path / "a").write_text("x")\n'
                '    else:\n'
                '        monkeypatch.setattr(builtins, "open", denied)\n'
                '        operation = lambda: open(tmp_path / "a", "w")\n'
                '    with pytest.raises(PermissionError, match="product fault"):\n        operation()\n')
            result, output = self._run_fixture(root, source)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            task = json.loads((output / 'summary.json').read_text())['tasks'][0]
            artifacts = Path(task['artifacts'])
            self.assertEqual(task['progress']['outcomes'], {'passed': 4})
            self.assertEqual(len(json.loads((artifacts / 'inventory.json').read_text())), 4)
            cases = json.loads((artifacts / 'cases.json').read_text())
            self.assertEqual(len(cases), 4)
            for case in cases:
                self.assertEqual(case['outcome'], 'passed')
                self.assertEqual(set(case['phases']), {'setup', 'call', 'teardown'})
            events = [json.loads(line) for line in (artifacts / 'events.jsonl').read_text().splitlines()]
            self.assertEqual(events[-1]['status'], 'finished')
            self.assertEqual(sum(event.get('record') == 'phase' for event in events), 12)

    def test_python_cache_is_private_and_external_dependency_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            self._fixture_repository(source, 'test_private_cache.py',
                'import os\nimport sys\nfrom pathlib import Path\nimport jsonschema\n'
                'def test_private_cache():\n'
                '    repo = Path(__file__).resolve().parents[2]\n'
                '    prefix = Path(os.environ["PYTHONPYCACHEPREFIX"]).resolve()\n'
                '    assert not prefix.is_relative_to(repo)\n'
                '    assert not sys.dont_write_bytecode\n'
                '    assert "PYTHONDONTWRITEBYTECODE" not in os.environ\n'
                '    assert str(repo) not in os.environ["PYTHONPYCACHEPREFIX"]\n'
                '    cache = Path(jsonschema.__cached__)\n'
                '    assert cache.is_relative_to(prefix) and cache.is_file()\n')
            shared = Path(sys.prefix) / 'lib/python3.11/site-packages/jsonschema'
            before = {str(path): (path.stat().st_size, path.stat().st_mtime_ns) for path in shared.rglob('*.pyc')}
            # 用户继承环境中的设置不能使任务再次禁写或采用外部缓存。
            prior = {name: os.environ.get(name) for name in ('PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX')}
            os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
            os.environ['PYTHONPYCACHEPREFIX'] = str(root / 'unowned-cache')
            try:
                result, output = self._run_fixture(root, source)
            finally:
                for name, value in prior.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            task = json.loads((output / 'summary.json').read_text())['tasks'][0]
            self.assertEqual(task['progress']['outcomes'], {'passed': 1})
            self.assertFalse((root / 'unowned-cache').exists())
            after = {str(path): (path.stat().st_size, path.stat().st_mtime_ns) for path in shared.rglob('*.pyc')}
            self.assertEqual(before, after)

    def test_snapshot_omits_evidence_but_retains_timing_and_dirty_source_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            self._fixture_repository(source, 'test_snapshot.py',
                'import subprocess\nfrom pathlib import Path\n'
                'def test_snapshot():\n'
                '    root = Path.cwd()\n'
                '    assert (root / "protocol/marker").read_text() == "dirty"\n'
                '    assert (root / "protocol/new").read_text() == "new"\n'
                '    assert not (root / "protocol/deleted").exists()\n'
                '    assert not (root / "docs/camctl/verification-data/old/output.log").exists()\n'
                '    assert (root / "docs/contract.md").read_text() == "contract"\n'
                '    status = subprocess.check_output(["git", "status", "--short"], text=True)\n'
                '    assert "docs/camctl/verification-data/old/output.log" in status\n', history=True)
            evidence = source / 'docs/camctl/verification-data/old/output.log'
            evidence.parent.mkdir(parents=True)
            evidence.write_text('old')
            (source / 'docs/contract.md').write_text('contract')
            (source / 'protocol/deleted').write_text('deleted')
            timing = source / 'docs/camctl/verification-data/2026-10-25-integration-timing'
            (timing / 'camctl-files.csv').write_text('name,total_s\n')
            subprocess.run(['git', 'add', '.'], cwd=source, check=True)
            subprocess.run(['git', '-c', 'user.name=runner-test', '-c', 'user.email=runner@example.invalid',
                            'commit', '-qm', 'inputs'], cwd=source, check=True)
            (source / 'protocol/marker').write_text('dirty')
            (source / 'protocol/new').write_text('new')
            (source / 'protocol/deleted').unlink()
            result, output = self._run_fixture(root, source)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            identity = json.loads((output / 'source.json').read_text())
            paths = {item['path'] for item in identity['files']}
            self.assertNotIn('docs/camctl/verification-data/old/output.log', paths)
            self.assertIn('protocol/new', paths)
            self.assertNotIn('protocol/deleted', paths)
            for name in ('camctl-files.csv', 'cross-component-files.csv', 'cross-component-cases.csv'):
                self.assertIn(f'docs/camctl/verification-data/2026-10-25-integration-timing/{name}', paths)
            self.assertIn('protocol/marker', identity['status'])
            self.assertIn('excluded tracked files appear deleted', identity['snapshot_scope']['git_index'])

    def test_output_inside_source_is_not_in_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            self._fixture_repository(source, 'test_simple.py', 'def test_simple():\n    assert True\n')
            cache, modules, output = root / 'cache', root / 'node_modules', source / 'own-output'
            cache.mkdir()
            modules.mkdir()
            alias = root / 'source-alias'
            alias.symlink_to(source, target_is_directory=True)
            result = subprocess.run([sys.executable, str(RUNNER), '--source', str(source), '--python', sys.executable,
                '--uv-cache', str(cache), '--node-modules', str(modules), '--output', str(alias / 'own-output'), '--jobs', '1'],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            identity = json.loads((output / 'source.json').read_text())
            self.assertEqual(identity['snapshot_scope']['excluded_output_directory'], 'own-output/')
            self.assertFalse(any(item['path'].startswith('own-output/') for item in identity['files']))

    def test_unknown_target_is_rejected_before_execution(self):
        result = subprocess.run([sys.executable, str(RUNNER), '--list', '--target',
                                 'tests/integration/does_not_exist.py'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('does_not_exist.py', result.stderr)


if __name__ == '__main__':
    unittest.main()
