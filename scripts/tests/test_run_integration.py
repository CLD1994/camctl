"""并行入口的真实进程契约测试；不运行产品集成套件。"""
from __future__ import annotations

import errno
import importlib.util
import json
import os
import shutil
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

RUNNER = Path(__file__).resolve().parents[1] / 'run-integration.py'


class IntegrationRunnerTest(unittest.TestCase):
    def _host_build_fixture(self, root, *, failing=False):
        source = root / 'input'
        body = (
            'import json, os, subprocess, sys, time\nfrom pathlib import Path\n'
            'def test_host(host_demo):\n'
            '    private = Path(host_demo)\n'
            '    shared = Path(os.environ["CAMCTL_HOST_DEMO_BUILD_DIR"]) / "build"\n'
            '    receipt = Path(os.environ["TEST_HOST_RECEIPTS"]) / Path(__file__).stem\n'
            '    assert subprocess.check_output([str(private)], text=True).strip() == "frozen"\n'
            '    assert sys.executable in (private.parent / "camctl-launcher.sh").read_text()\n'
            '    for name in ("host-demo", "libcamctl_host.a"):\n'
            '        assert (private.parent / name).stat().st_ino != (shared / name).stat().st_ino\n'
            '        assert not (shared / name).stat().st_mode & 0o222\n'
            '    receipt.write_text(json.dumps({"private": str(private.parent)}))\n'
            '    deadline = time.monotonic() + 20\n'
            '    while len(list(receipt.parent.glob("test_host_*"))) < 2:\n'
            '        assert time.monotonic() < deadline\n        time.sleep(.02)\n'
            '    (private.parent / "libcamctl_host.a").write_bytes(b"private")\n'
            '    assert (shared / "libcamctl_host.a").read_bytes() != b"private"\n')
        self._fixture_repository(source, 'test_host_a.py', body)
        (source / 'tests/integration/test_host_b.py').write_text(body)
        for name in ('conftest.py', '_wsl_host_demo.py'):
            shutil.copy2(RUNNER.parent.parent / 'tests/integration' / name, source / 'tests/integration' / name)
        with (source / 'tests/integration/conftest.py').open('a') as stream:
            stream.write('\n@pytest.fixture(scope="session", autouse=True)\ndef change_live_source():\n'
                         '    import os\n    from pathlib import Path\n'
                         '    Path(os.environ["TEST_LIVE_SOURCE"]).write_text(\'const char *value(void) { return "changed"; }\\n\')\n')
        host = source / 'apps/host-demo'
        host.mkdir()
        (host / 'CMakeLists.txt').write_text(
            'cmake_minimum_required(VERSION 3.10)\nproject(test_host C)\n'
            'add_library(camctl_host STATIC lib.c)\nadd_executable(host-demo main.c)\n'
            'target_link_libraries(host-demo PRIVATE camctl_host)\n')
        (host / 'lib.c').write_text('const char *value(void) { return "frozen"; }\n')
        (host / 'main.c').write_text('#include <stdio.h>\nconst char *value(void);\nint main(void) { puts(value()); return 0; }\n')
        binaries, receipts = root / 'bin', root / 'receipts'
        binaries.mkdir()
        receipts.mkdir()
        cmake = binaries / 'cmake'
        cmake.write_text('#!/bin/sh\nif [ "$1" = "-S" ]; then\n'
                         '  echo configure >> "$TEST_HOST_CMAKE_CALLS"\n' +
                         ('  echo injected-build-failure >&2; exit 19\n' if failing else '') +
                         'fi\nexec /usr/bin/cmake "$@"\n')
        cmake.chmod(0o755)
        return source, {'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
                        'TEST_HOST_RECEIPTS': str(receipts), 'TEST_HOST_CMAKE_CALLS': str(root / 'calls'),
                        'TEST_LIVE_SOURCE': str(host / 'lib.c')}

    def _run_host_fixture(self, root, source, environment, *options):
        previous = {name: os.environ.get(name) for name in environment}
        os.environ.update(environment)
        try:
            return self._run_fixture(root, source, '--scope', 'root', *options)
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_host_build_is_shared_only_within_round_and_copies_are_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, environment = self._host_build_fixture(root)
            result, output = self._run_host_fixture(root, source, environment)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / 'calls').read_text().splitlines(), ['configure'])
            receipts = [json.loads(path.read_text()) for path in (root / 'receipts').iterdir()]
            self.assertEqual(len(receipts), 2)
            self.assertNotEqual(receipts[0]['private'], receipts[1]['private'])
            self.assertTrue(all(not Path(item['private']).exists() for item in receipts))
            self.assertFalse((output / 'host-demo-build').exists())
            summary = json.loads((output / 'summary.json').read_text())
            self.assertTrue(all(task['status'] == 'passed' for task in summary['tasks']))
            self.assertEqual(summary['host_demo_build']['status'], 'ready')
            self.assertEqual(summary['host_demo_build']['build_count'], 1)
            self.assertGreater(summary['host_demo_build']['wall_seconds'], 0)
            (source / 'apps/host-demo/lib.c').write_text('const char *value(void) { return "frozen"; }\n')
            for receipt in (root / 'receipts').iterdir():
                receipt.unlink()
            second = subprocess.run([sys.executable, str(RUNNER), '--source', str(source), '--python', sys.executable,
                '--uv-cache', str(root / 'cache'), '--node-modules', str(root / 'node_modules'),
                '--output', str(root / 'output-second'), '--jobs', '2', '--scope', 'root'],
                capture_output=True, text=True, env={**os.environ, **environment})
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual((root / 'calls').read_text().splitlines(), ['configure', 'configure'])
            self.assertFalse((root / 'output-second/host-demo-build').exists())

    def test_direct_pytest_builds_current_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, environment = self._host_build_fixture(root)
            direct = source / 'tests/integration/test_direct.py'
            direct.write_text('import subprocess\n'
                              'def test_direct(host_demo):\n'
                              '    assert subprocess.check_output([host_demo], text=True).strip() == "changed"\n')
            temporary_build = root / 'direct-tmp'
            temporary_build.mkdir()
            environment.update(PYTHONPATH=str(RUNNER.parent), TMPDIR=str(temporary_build))
            direct_env = {**os.environ, **environment}
            direct_env.pop('CAMCTL_HOST_DEMO_BUILD_DIR', None)
            direct_env.pop('CAMCTL_HOST_DEMO_SOURCE', None)
            direct_env.pop('CAMCTL_HOST_DEMO_RECEIPT', None)
            result = subprocess.run([sys.executable, '-m', 'pytest', '-q', str(direct)], cwd=source,
                                    env=direct_env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / 'calls').read_text().splitlines(), ['configure'])
            self.assertFalse(list(temporary_build.glob('camctl-host-build-*')))

    def test_host_build_failure_propagates_and_does_not_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, environment = self._host_build_fixture(root, failing=True)
            result, output = self._run_host_fixture(root, source, environment)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual((root / 'calls').read_text().splitlines(), ['configure'])
            tasks = json.loads((output / 'summary.json').read_text())['tasks']
            self.assertTrue(all(task['status'] == 'failed' for task in tasks))
            receipt = json.loads((output / 'summary.json').read_text())['host_demo_build']
            self.assertEqual((receipt['status'], receipt['build_count']), ('failed', 1))
            logs = ''.join((Path(task['artifacts']) / 'output.log').read_text() for task in tasks)
            self.assertIn('injected-build-failure', logs)
            self.assertIn('本轮 host-demo 构建未完成或失败', logs)
            self.assertFalse((output / 'host-demo-build').exists())
            self.assertFalse(list(output.glob('tasks/*/tmp/camctl-host-build-*')))

    def test_host_copy_failure_propagates_without_rebuilding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, environment = self._host_build_fixture(root)
            conftest = source / 'tests/integration/conftest.py'
            with conftest.open('a') as stream:
                stream.write('\n@pytest.fixture(scope="session", autouse=True)\ndef deny_host_copy():\n'
                             '    import host_demo_build\n'
                             '    original = host_demo_build.shutil.copyfile\n'
                             '    def deny(*args, **kwargs):\n        raise OSError("copy denied")\n'
                             '    host_demo_build.shutil.copyfile = deny\n'
                             '    yield\n    host_demo_build.shutil.copyfile = original\n')
            result, output = self._run_host_fixture(root, source, environment)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual((root / 'calls').read_text().splitlines(), ['configure'])
            tasks = json.loads((output / 'summary.json').read_text())['tasks']
            self.assertTrue(all(task['status'] == 'failed' for task in tasks))
            self.assertTrue(all('copy denied' in (Path(task['artifacts']) / 'output.log').read_text() for task in tasks))
            self.assertFalse((output / 'host-demo-build').exists())
            self.assertFalse(list(output.glob('tasks/*/tmp/camctl-host-build-*')))

    def test_interrupt_stops_build_descendants_before_shared_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, environment = self._host_build_fixture(root)
            cmake = root / 'bin/cmake'
            cmake.write_text('#!/bin/sh\ntrap "" INT\necho $$ > "$TEST_HOST_CMAKE_CALLS"\n'
                             'sleep 60 &\necho $! >> "$TEST_HOST_CMAKE_CALLS"\nwait\n')
            cache, modules, output = root / 'cache', root / 'node_modules', root / 'output'
            cache.mkdir()
            modules.mkdir()
            process = subprocess.Popen([sys.executable, str(RUNNER), '--source', str(source), '--python', sys.executable,
                '--uv-cache', str(cache), '--node-modules', str(modules), '--output', str(output),
                '--jobs', '1', '--scope', 'root'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                env={**os.environ, **environment})
            deadline = time.monotonic() + 20
            while not (root / 'calls').exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue((root / 'calls').exists())
            process.send_signal(signal.SIGINT)
            stdout, _ = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 130, stdout)
            for pid in (root / 'calls').read_text().splitlines():
                status = Path(f'/proc/{pid}/stat')
                # 已杀死但尚待 init 回收的僵尸不能继续写构建目录。
                self.assertTrue(not status.exists() or status.read_text().split(') ')[1][0] == 'Z', pid)
            self.assertFalse((output / 'host-demo-build').exists())
            self.assertFalse(list(output.glob('tasks/*/tmp/camctl-host-build-*')))
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(summary['status'], 'interrupted')
            self.assertEqual(summary['host_demo_build']['build_count'], 1)
            self.assertIn(summary['host_demo_build']['status'], ('building', 'failed'))
            self.assertGreater(summary['host_demo_build']['wall_seconds'], 0)

    def test_host_build_is_not_started_for_plan_or_unrelated_test(self):
        for options in (('--plan-only', '--force-root-shards'), ('--target', 'tests/integration/test_other.py')):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, environment = self._host_build_fixture(root)
                (source / 'tests/integration/test_other.py').write_text('def test_other(): pass\n')
                result, output = self._run_host_fixture(root, source, environment, *options)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse((root / 'calls').exists())
                self.assertFalse((output / 'host-demo-build').exists())
                self.assertEqual(json.loads((output / 'summary.json').read_text())['host_demo_build'],
                                 {'status': 'not_requested', 'build_count': 0})

    def test_kind_selection_matches_shards_and_recorded_cases(self):
        cases = (((), {'test_a', 'test_b'}, 'functional'),
                 (('--test-kind', 'performance'), {'test_p', 'test_q'}, 'performance'),
                 (('--test-kind', 'acceptance'), {'test_x', 'test_y'}, 'acceptance'),
                 (('--test-kind', 'all'), {'test_a', 'test_b', 'test_p', 'test_q', 'test_x', 'test_y'}, None))
        for options, expected, kind in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temporary:
                root, source = Path(temporary), Path(temporary) / 'input'
                self._fixture_repository(source, 'test_matrix.py',
                    'import pytest\ndef test_a(): pass\ndef test_b(): pass\n'
                    '@pytest.mark.performance\ndef test_p(): pass\n'
                    '@pytest.mark.performance\ndef test_q(): pass\n'
                    '@pytest.mark.acceptance\ndef test_x(): pass\n'
                    '@pytest.mark.acceptance\ndef test_y(): pass\n')
                shutil.copy2(RUNNER.parent.parent / 'conftest.py', source / 'conftest.py')
                shutil.copy2(RUNNER.parent.parent / 'apps/camctl/conftest.py', source / 'apps/camctl/conftest.py')
                result, output = self._run_fixture(root, source, '--scope', 'root',
                    '--force-root-shards', '--root-shards', '2', *options)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                summary = json.loads((output / 'summary.json').read_text())
                self.assertEqual(summary['test_kind'], kind or 'all')
                plan = json.loads((output / 'plan.json').read_text())
                collected = json.loads((Path(plan[0]['collection']) / 'inventory.json').read_text())
                assigned = [node for task in plan for node in task['targets']]
                self.assertCountEqual(assigned, collected)
                records = [case for task in summary['tasks'] for case in json.loads(
                    (Path(task['artifacts']) / 'cases.json').read_text())]
                self.assertCountEqual([case['nodeid'].split('::')[-1] for case in records], expected)
                for case in records:
                    name = case['nodeid'].split('::')[-1]
                    self.assertEqual(case['test_kind'], 'performance' if name in ('test_p', 'test_q') else 'acceptance' if name in ('test_x', 'test_y') else 'functional')

    def test_empty_selection_and_invalid_node_are_distinct(self):
        for options, expected_status, expected_exit in (
            ((), 'not_selected', 1),
            (('--force-root-shards',), 'not_selected', 1),
            (('--test-kind', 'acceptance', '--force-root-shards'), 'not_selected', 1),
            (('--target', 'tests/integration/test_matrix.py::test_p'), 'not_selected', 1),
            (('--target', 'tests/integration/test_matrix.py::test_missing'), 'failed', 1),
        ):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temporary:
                root, source = Path(temporary), Path(temporary) / 'input'
                self._fixture_repository(source, 'test_matrix.py',
                    'import pytest\n@pytest.mark.performance\ndef test_p(): pass\n')
                result, output = self._run_fixture(root, source, '--scope', 'root', *options)
                self.assertEqual(result.returncode, expected_exit, result.stdout + result.stderr)
                summary = json.loads((output / 'summary.json').read_text())
                self.assertEqual(summary['status'], expected_status if expected_status == 'not_selected' else 'finished')
                task, = summary['tasks']
                self.assertEqual(task['status'], expected_status)
                self.assertEqual(task['exit_code'], 5 if expected_status == 'not_selected' else 4)

    def test_specialized_kinds_exclude_node_and_empty_tasks_do_not_hide_passes(self):
        for kind in ('performance', 'acceptance'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root, source = Path(temporary), Path(temporary) / 'input'
                self._fixture_repository(source, 'test_performance.py',
                    f'import pytest\n@pytest.mark.{kind}\ndef test_perf(): pass\n')
                (source / 'tests/integration/test_functional.py').write_text('def test_functional(): pass\n')
                (source / 'tests/integration/ordinary.test.mjs').write_text('throw Error("must not run")\n')
                result, output = self._run_fixture(root, source, '--test-kind', kind)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                summary = json.loads((output / 'summary.json').read_text())
                self.assertEqual(sorted(task['status'] for task in summary['tasks']), ['not_selected', 'passed'])
                self.assertTrue(all(task['scope'] == 'root' for task in summary['tasks']))

    def test_shard_collection_error_is_not_empty_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            self._fixture_repository(source, 'test_broken.py', 'raise RuntimeError("collection failure")\n')
            result, output = self._run_fixture(root, source, '--scope', 'root', '--force-root-shards')
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(summary['status'], 'failed')
            self.assertIn('收集失败', summary['error'])

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
                'import os, subprocess\nfrom pathlib import Path\nfrom client_test_build import prepare_standard_client\n'
                'def test_candidate():\n'
                '    repo = Path(__file__).resolve().parents[2]\n'
                '    candidate = Path(os.environ["CAMCTL_HB_TEST_CLIENT_ROOT"]).resolve()\n'
                '    assert candidate != repo and repo not in candidate.parents\n'
                '    prepare_standard_client(candidate / "apps/client", lambda command, *, cwd: subprocess.run(command, cwd=cwd, check=True))\n'
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

    def test_hasselblad_observer_does_not_build_standard_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            self._fixture_repository(source, 'test_hasselblad_photo_roundtrip.py',
                                     'def test_observer(): pass\n')
            result, output = self._run_fixture(root, source)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = json.loads((output / 'summary.json').read_text())
            for name in ('host_demo_build', 'python_wheel_build', 'client_web_build'):
                self.assertEqual(summary[name], {'status': 'not_requested', 'build_count': 0})

    def test_root_shards_use_current_collection_including_unmeasured_nodes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'input'
            self._fixture_repository(source, 'test_matrix.py',
                'import pytest\n'
                '@pytest.mark.parametrize("value", range(7), ids=["a","b","c","d","e","f","new"])\n'
                'def test_value(value):\n    assert value >= 0\n',
                history=True)
            result, output = self._run_fixture(root, source, '--timing-history', str(root / 'history'), '--root-shards', '2')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            plan = json.loads((output / 'plan.json').read_text())
            self.assertEqual(len(plan), 2)
            self.assertCountEqual([task['estimated_seconds'] for task in plan], [480, 451])
            nodes = [node for task in plan for node in task['targets']]
            expected = ['tests/integration/test_matrix.py::test_value[' + name + ']' for name in ('a','b','c','d','e','f','new')]
            self.assertCountEqual(nodes, expected)
            self.assertEqual(len(nodes), len(set(nodes)))
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(sum(task['progress']['collected'] for task in summary['tasks']), 7)
            self.assertFalse(any((output / 'collection').glob('*/source')))
            self.assertTrue(Path(plan[0]['collection'], 'inventory.json').is_file())

    def test_timing_history_orders_discovered_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            self._fixture_repository(source, 'test_a.py', 'def test_value():\n    assert True\n', history=True)
            (source / 'tests/integration/test_z.py').write_text('def test_value():\n    assert True\n')
            (root / 'history/cross-component-cases.csv').write_text(
                'nodeid,status,total_s\ntests/integration/test_z.py::test_value,passed,4\n')
            result, output = self._run_fixture(root, source, '--timing-history', str(root / 'history'),
                                               '--root-shards', '1', '--plan-only')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            plan = json.loads((output / 'plan.json').read_text())
            self.assertEqual([task['path'] for task in plan],
                             ['tests/integration/test_z.py', 'tests/integration/test_a.py'])
            self.assertEqual([task['estimated_seconds'] for task in plan], [4, 0])

    def test_timing_input_is_explicit_and_excluded_from_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            self._fixture_repository(source, 'test_matrix.py',
                'import pytest\n@pytest.mark.parametrize("value", [1, 2])\n'
                'def test_value(value):\n    assert value\n', history=True)
            legacy = source / 'docs/camctl/verification-data/history'
            legacy.mkdir(parents=True)
            for path in (root / 'history').iterdir():
                (legacy / path.name).write_bytes(path.read_bytes())
            result, output = self._run_fixture(root, source, '--plan-only', '--root-shards', '2')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            task, = json.loads((output / 'plan.json').read_text())
            self.assertEqual(task['estimated_seconds'], 0)
            self.assertFalse(task['historical_failure_observed'])
            self.assertFalse((output / 'collection').exists())
            self.assertFalse(any(item['path'].startswith('docs/camctl/verification-data/')
                                 for item in json.loads((output / 'source.json').read_text())['files']))

    def test_invalid_explicit_timing_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / 'history'
            for problem in ('missing_directory', 'missing_file', 'bad_header', 'bad_seconds'):
                with self.subTest(problem=problem):
                    if problem != 'missing_directory':
                        directory.mkdir(exist_ok=True)
                        (directory / 'camctl-files.csv').write_text('name,total_s\n')
                        (directory / 'cross-component-files.csv').write_text('name,total_s\n')
                        (directory / 'cross-component-cases.csv').write_text('nodeid,status,total_s\n')
                        if problem == 'missing_file':
                            (directory / 'camctl-files.csv').unlink()
                        elif problem == 'bad_header':
                            (directory / 'camctl-files.csv').write_text('name\n')
                        elif problem == 'bad_seconds':
                            (directory / 'camctl-files.csv').write_text('name,total_s\ntest.py,nan\n')
                    result = subprocess.run([sys.executable, str(RUNNER), '--list',
                        '--timing-history', str(directory)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(str(directory), result.stderr)

    def test_duplicate_timing_columns_and_keys_are_rejected(self):
        file_headers = {'camctl-files.csv': 'name,total_s',
                        'cross-component-files.csv': 'name,total_s',
                        'cross-component-cases.csv': 'nodeid,status,total_s'}
        cases = []
        for name, header in file_headers.items():
            for field in header.split(','):
                cases.append((name, header + ',' + field + '\n', None))
        for name in ('camctl-files.csv', 'cross-component-files.csv'):
            cases.append((name, 'name,total_s\ntest.py,1\ntest.py,2\n', None))
        cases.append(('cross-component-cases.csv',
                      'nodeid,status,total_s\ntest.py::test_a,passed,1\ntest.py::test_a,failed,2\n', None))
        cases.append(('cross-component-files.csv', 'name,total_s\ntest.py,2\n',
                      ('camctl-files.csv', 'name,total_s\ntest.py,1\n')))
        for name, content, additional in cases:
            with self.subTest(name=name, content=content, additional=additional), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                for file, header in file_headers.items():
                    (directory / file).write_text(header + '\n')
                (directory / name).write_text(content)
                if additional:
                    (directory / additional[0]).write_text(additional[1])
                result = subprocess.run([sys.executable, str(RUNNER), '--list',
                    '--timing-history', str(directory)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(str(directory / name), result.stderr)

    def test_local_timing_files_are_not_copied_into_source_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            self._fixture_repository(source, 'test_matrix.py', 'def test_value():\n    assert True\n', history=True)
            local = source / 'local-timing'
            local.mkdir()
            for path in (root / 'history').iterdir():
                (local / path.name).write_bytes(path.read_bytes())
            result, output = self._run_fixture(root, source, '--timing-history', str(local), '--plan-only')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            files = json.loads((output / 'source.json').read_text())['files']
            self.assertFalse(any(item['path'].startswith('local-timing/') for item in files))

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
                result, output = self._run_fixture(root, source, '--timing-history', str(root / 'history'), '--root-shards', str(shards),
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
                timing = root / 'history'
                (timing / 'cross-component-files.csv').write_text('name,total_s\ntests/integration/test_matrix.py,6\n')
                (timing / 'cross-component-cases.csv').write_text(
                    'nodeid,status,total_s\n'
                    'tests/integration/test_matrix.py::test_value[a],passed,5\n'
                    f'tests/integration/test_matrix.py::test_value[b],{historical_status},1\n')
                result, output = self._run_fixture(root, source, '--timing-history', str(root / 'history'), '--root-shards', '2')
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

    def test_explicit_root_sharding_uses_selected_current_nodes(self):
        target = 'tests/integration/test_matrix.py::test_value'
        expected = [target + '[' + name + ']' for name in ('a', 'b', 'new')]
        cases = (
            ('default', (), 1),
            ('forced', ('--force-root-shards',), 2),
            ('one-shard', ('--force-root-shards', '--root-shards', '1'), 1),
            ('one-node', ('--force-root-shards', '--target', expected[0]), 1),
        )
        for case, options, count in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / 'input'
                self._fixture_repository(source, 'test_matrix.py',
                    'import pytest\n'
                    '@pytest.mark.parametrize("value", range(3), ids=["a","b","new"])\n'
                    'def test_value(value):\n    assert value >= 0\n')
                selection = () if case == 'one-node' else (
                    '--target', target, '--target', expected[0])
                preview = () if case == 'forced' else ('--plan-only',)
                result, output = self._run_fixture(root, source, '--scope', 'root',
                    '--root-shards', '2', *selection, *options, *preview)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                plan = json.loads((output / 'plan.json').read_text())
                self.assertEqual(len(plan), count)
                self.assertTrue(all(not task['historical_failure_observed'] for task in plan))
                if case == 'forced':
                    assigned = [node for task in plan for node in task['targets']]
                    self.assertCountEqual(assigned, expected)
                    self.assertEqual(len(assigned), len(set(assigned)))
                    inventory = json.loads((Path(plan[0]['collection']) / 'inventory.json').read_text())
                    self.assertCountEqual(inventory, expected)
                    summary = json.loads((output / 'summary.json').read_text())
                    actual = [node for task in summary['tasks'] for node in json.loads(
                        (Path(task['artifacts']) / 'inventory.json').read_text())]
                    self.assertCountEqual(actual, expected)
                    self.assertEqual(len(actual), len(set(actual)))
                elif case == 'one-node':
                    self.assertEqual(plan[0]['targets'], [expected[0]])
                    collected = json.loads((output / 'collection/root-test_matrix/artifacts/inventory.json').read_text())
                    self.assertEqual(collected, [expected[0]])
                else:
                    self.assertEqual(plan[0]['targets'], [target])
                    self.assertFalse((output / 'collection').exists())

    def test_force_sharding_only_changes_selected_root_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source = Path(temporary), Path(temporary) / 'input'
            self._fixture_repository(source, 'test_matrix.py',
                'import pytest\n'
                '@pytest.mark.parametrize("value", [1, 2])\n'
                'def test_value(value):\n    assert value\n')
            component = source / 'apps/camctl/tests/integration/example'
            component.mkdir()
            (component / 'test_component.py').write_text('def test_value():\n    assert True\n')
            (source / 'tests/integration/example.test.mjs').write_text('// Node task\n')
            result, output = self._run_fixture(root, source, '--force-root-shards',
                                               '--root-shards', '2', '--plan-only')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            plan = json.loads((output / 'plan.json').read_text())
            self.assertEqual([task['scope'] for task in plan].count('root'), 2)
            for scope in ('camctl', 'node'):
                task, = [task for task in plan if task['scope'] == scope]
                self.assertEqual(task['targets'], [task['path']])
                self.assertNotIn('shard', task)

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
            timing = source.parent / 'history'
            timing.mkdir()
            (timing / 'camctl-files.csv').write_text('name,total_s\n')
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
                '--timing-history', str(root / 'history'),
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

    def test_snapshot_omits_evidence_and_timing_and_retains_dirty_source_identity(self):
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
                '    assert not (root / "docs/camctl/verification-data/history/camctl-files.csv").exists()\n'
                '    assert (root / "docs/contract.md").read_text() == "contract"\n'
                '    status = subprocess.check_output(["git", "status", "--short"], text=True)\n'
                '    assert "docs/camctl/verification-data/old/output.log" in status\n')
            evidence = source / 'docs/camctl/verification-data/old/output.log'
            evidence.parent.mkdir(parents=True)
            evidence.write_text('old')
            (source / 'docs/contract.md').write_text('contract')
            (source / 'protocol/deleted').write_text('deleted')
            timing = source / 'docs/camctl/verification-data/history'
            timing.mkdir()
            for name in ('camctl-files.csv', 'cross-component-files.csv', 'cross-component-cases.csv'):
                (timing / name).write_text('evidence')
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
                self.assertNotIn(f'docs/camctl/verification-data/history/{name}', paths)
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
