#!/usr/bin/env python3
"""在独立源码副本与工具环境中并行运行完整或指定范围的集成测试。"""
from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time

import camctl_memory
from camctl_memory import DEFAULT_MEMORY_LIMIT, parse_memory_limit
from test_kind_plugin import TEST_KINDS, DEFAULT_TEST_KIND
from round_test_build import BUILD_NAMES, read_build_summaries

CAMCTL = 'apps/camctl/tests/integration'
ROOT = 'tests/integration'
HB_MODULE = ROOT + '/test_hasselblad_photo_roundtrip.py'
ROOT_SHARD_SECONDS = 600
TIMING_FILES = ('camctl-files.csv', 'cross-component-files.csv', 'cross-component-cases.csv')
VERIFICATION_DATA = 'docs/camctl/verification-data/'


def utc():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    temporary = path.with_suffix('.next')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def discover(source):
    tasks = []
    for directory in sorted((source / CAMCTL).iterdir()):
        if directory.is_dir() and any(directory.rglob('test_*.py')):
            tasks.append({'id': 'camctl/' + directory.name, 'scope': 'camctl',
                          'path': directory.relative_to(source).as_posix()})
    for pattern, scope in [('test_*.py', 'root'), ('*.test.mjs', 'node')]:
        for path in sorted((source / ROOT).glob(pattern)):
            tasks.append({'id': scope + '/' + path.stem, 'scope': scope,
                          'path': path.relative_to(source).as_posix()})
    return tasks


def select(tasks, scope, targets):
    selected = [task.copy() for task in tasks if scope == 'all' or task['scope'] == scope]
    if not targets:
        return [{**task, 'targets': [task['path']]} for task in selected]
    assigned = {}
    canonical = []
    for target in targets:
        file, separator, node = target.partition('::')
        file = os.path.normpath(file)
        if os.path.isabs(file) or file == '..' or file.startswith('../'):
            raise ValueError(f'目标必须位于源码根内: {target}')
        canonical.append(file + separator + node)
    for target in dict.fromkeys(canonical):
        file, _, node = target.partition('::')
        matches = [task for task in selected if file == task['path'] or
                   (task['scope'] == 'camctl' and file.startswith(task['path'] + '/'))]
        if len(matches) != 1 or (matches and matches[0]['scope'] == 'node' and node):
            raise ValueError(f'目标不在所选范围内: {target}')
        task = matches[0]
        assigned.setdefault(task['id'], {**task, 'targets': []})['targets'].append(target)
    for task in assigned.values():
        # 文件或目录已包含的节点不再单独传入 pytest，避免重复执行。
        task['targets'] = [target for target in task['targets'] if not any(
            target != other and (target.startswith(other + '/') or target.startswith(other + '::') or target.startswith(other + '['))
            for other in task['targets'])]
    return [assigned[task['id']] for task in selected if task['id'] in assigned]


def covers(parent, child):
    return child == parent or any(child.startswith(parent + boundary) for boundary in ('/', '::', '['))


def load_timing_history(directory):
    """读取显式提供的调度输入；未提供时使用空历史。"""
    if directory is None:
        return {}, {}, set()
    if not directory.is_dir():
        raise ValueError(f'计时历史目录不存在或不是目录: {directory}')
    costs, cases, failed_modules = {}, {}, set()
    for name in TIMING_FILES:
        path = directory / name
        required = {'nodeid', 'status', 'total_s'} if name == 'cross-component-cases.csv' else {'name', 'total_s'}
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError(f'计时历史列缺失: {path}')
            if any(reader.fieldnames.count(field) > 1 for field in required):
                raise ValueError(f'计时历史必要列重复: {path}')
            for line, row in enumerate(reader, start=2):
                key = row['nodeid'] if name == 'cross-component-cases.csv' else row['name']
                try:
                    seconds = float(row['total_s'])
                except (TypeError, ValueError) as error:
                    raise ValueError(f'计时历史耗时无效: {path}:{line}') from error
                if not key or not math.isfinite(seconds) or seconds < 0:
                    raise ValueError(f'计时历史记录无效: {path}:{line}')
                target = cases if name == 'cross-component-cases.csv' else costs
                if key in target:
                    raise ValueError(f'计时历史记录重复: {path}:{line}: {key}')
                if name == 'cross-component-cases.csv':
                    if not row['status']:
                        raise ValueError(f'计时历史状态无效: {path}:{line}')
                    cases[key] = seconds
                    if row['status'] == 'failed':
                        failed_modules.add(key.partition('::')[0])
                else:
                    costs[key] = seconds
    return costs, cases, failed_modules


def order_by_measurement(tasks, history):
    """既有记录只用于排程；未测目标仍完整执行。"""
    costs, cases, failed_modules = history
    for task in tasks:
        task['estimated_seconds'] = sum(cost for path, cost in costs.items()
                                        if path == task['path'] or path.startswith(task['path'] + '/'))
        if task['scope'] == 'root':
            task['historical_failure_observed'] = task['path'] in failed_modules
            if cases:
                task['estimated_seconds'] = sum(cost for node, cost in cases.items()
                                                if any(covers(target, node) for target in task['targets']))
    return sorted(tasks, key=lambda task: (-task['estimated_seconds'], task['id']))


def command_output(command, cwd=None, env=None):
    return subprocess.check_output(command, cwd=cwd, env=env, text=True).strip()


def copy_tree(source, destination):
    destination.mkdir(parents=True, exist_ok=True)
    # GNU cp 在支持的文件系统上使用写时复制，否则复制文件；绝不使用硬链接。
    subprocess.run(['cp', '-a', '--reflink=auto', str(source) + '/.', str(destination)], check=True)


def remove_owned_tree(path):
    """删除本次运行拥有的树；仅在需要时恢复树内目录的所有者权限。"""
    root = Path(path).absolute()
    if not stat.S_ISDIR(root.lstat().st_mode):
        shutil.rmtree(root)
        return
    root_real = root.resolve()

    class RetryRemoval(Exception):
        pass

    def repair(function, failed_path, details):
        error = details[1]
        if not isinstance(error, PermissionError):
            raise error
        if function in (os.open, os.scandir):
            directory = Path(failed_path)
        elif function in (os.unlink, os.rmdir, os.lstat):
            directory = Path(failed_path).parent
        else:
            raise error
        resolved = directory.resolve()
        if resolved != root_real and not resolved.is_relative_to(root_real):
            raise error
        mode = directory.lstat().st_mode
        if not stat.S_ISDIR(mode) or mode & stat.S_IRWXU == stat.S_IRWXU:
            raise error
        os.chmod(directory, mode | stat.S_IRWXU, follow_symlinks=False)
        raise RetryRemoval from error

    while True:
        try:
            shutil.rmtree(root, onerror=repair)
            return
        except RetryRemoval:
            continue


def snapshot(source, destination, timing_directory=None):
    head = command_output(['git', 'rev-parse', 'HEAD'], source)
    files = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=source)
    paths = sorted(set(os.fsdecode(path) for path in files.split(b'\0') if path))
    try:
        output_relative = destination.parent.relative_to(source).as_posix() + '/'
    except ValueError:
        output_relative = None
    subprocess.run(['git', 'clone', '--shared', '--no-checkout', '--quiet', str(source), str(destination)], check=True)
    subprocess.run(['git', 'read-tree', 'HEAD'], cwd=destination, check=True)
    timing_paths = set()
    if timing_directory is not None:
        for name in TIMING_FILES:
            try:
                timing_paths.add((timing_directory / name).relative_to(source).as_posix())
            except ValueError:
                pass
    inventory = []
    for relative in paths:
        if (relative.startswith(VERIFICATION_DATA) or relative in timing_paths or
                (output_relative is not None and relative.startswith(output_relative))):
            continue
        original = source / relative
        if not original.exists() and not original.is_symlink():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if original.is_symlink():
            raise ValueError(f'源码快照不支持符号链接: {relative}')
        elif original.is_file():
            shutil.copy2(original, target)
            content = target.read_bytes()
        else:
            raise ValueError(f'源码快照不支持此对象: {relative}')
        inventory.append({'path': relative, 'sha256': hashlib.sha256(content).hexdigest()})
    return {'root': str(source), 'head': head,
            'status': command_output(['git', 'status', '--short'], source),
            'snapshot_scope': {'excluded_directory': VERIFICATION_DATA,
                               'excluded_output_directory': output_relative,
                               'git_index': 'HEAD; excluded tracked files appear deleted'},
            'snapshot_sha256': hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest(),
            'files': inventory}


def clone_snapshot(seed, destination):
    subprocess.run(['git', 'clone', '--shared', '--no-checkout', '--quiet', str(seed), str(destination)], check=True)
    subprocess.run(['git', 'read-tree', 'HEAD'], cwd=destination, check=True)
    for path in seed.iterdir():
        if path.name == '.git':
            continue
        target = destination / path.name
        if path.is_symlink():
            target.symlink_to(os.readlink(path))
        elif path.is_dir():
            shutil.copytree(path, target, symlinks=True)
        else:
            shutil.copy2(path, target)


def read_progress(directory):
    path = directory / 'progress.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def test_environment(python, project, control, private, temporary):
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'PWD',
                                  'PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX')
                   and not key.startswith(('UV_', 'CAMCTL_', 'PYTEST_'))}
    environment.update(PYTHONPATH=os.pathsep.join([str(control), str(project / 'src')]),
                       PYTHONNOUSERSITE='1', PYTHONUNBUFFERED='1',
                       PYTHONPYCACHEPREFIX=str(private / 'python-cache'), UV_PYTHON=str(python),
                       UV_PROJECT_ENVIRONMENT=str(private / 'project-env'),
                       UV_CACHE_DIR=str(private / 'uv-cache'), UV_OFFLINE='1',
                       TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
    return environment


def memory_pytest_arguments(args):
    arguments = ['--camctl-memory-limit', args.camctl_memory_limit]
    if args.camctl_memory_cgroup_root is not None:
        arguments.extend(['--camctl-memory-cgroup-root', str(args.camctl_memory_cgroup_root)])
    return arguments


def managed_wait_sync(process):
    try:
        return process.wait()
    except KeyboardInterrupt:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        raise


def collect_and_shard(args, tasks, output, seed, python, versions, identity, costs):
    """当前收集清单决定覆盖；历史记录用于自动选择及均衡权重。"""
    result = []
    for task in tasks:
        if (task['scope'] != 'root' or args.root_shards == 1 or
                (not args.force_root_shards and task['estimated_seconds'] < ROOT_SHARD_SECONDS
                 and not task['historical_failure_observed'])):
            result.append(task)
            continue
        directory = output / 'collection' / task['id'].replace('/', '-')
        artifacts, work = directory / 'artifacts', directory / 'source'
        artifacts.mkdir(parents=True)
        clone_snapshot(seed, work)
        temporary = directory / 'tmp'
        temporary.mkdir()
        project = work / 'apps/camctl'
        environment = test_environment(python, project, output / 'control', directory, temporary)
        environment['CAMCTL_INTEGRATION_PROGRESS'] = str(artifacts)
        resource_script = project / 'scripts/sync_resources.py'
        if resource_script.exists():
            command = [str(python), str(resource_script)]
            preparation = {'command': command, 'cwd': str(work), 'started_utc': utc()}
            with (artifacts / 'prepare.log').open('wb') as log:
                with subprocess.Popen(command, cwd=work, env=environment, stdout=log, stderr=log,
                                      start_new_session=True) as process:
                    preparation['pid'] = process.pid
                    write_json(artifacts / 'prepare-command.json', preparation)
                    try:
                        code = managed_wait_sync(process)
                    finally:
                        preparation.update(exit_code=process.returncode, finished_utc=utc())
                        write_json(artifacts / 'prepare-command.json', preparation)
            if code:
                raise subprocess.CalledProcessError(code, command)
        command = [str(python), '-m', 'pytest', f'--rootdir={work}', *task['targets'], '--collect-only', '-q', '-p', 'integration_progress',
                   '-p', 'camctl_memory_plugin', '-p', 'test_kind_plugin', '--test-kind', args.test_kind,
                   *memory_pytest_arguments(args)]
        record = {'command': command, 'cwd': str(work), 'started_utc': utc(),
                  'source': identity['snapshot_sha256'], 'versions': versions}
        started = time.monotonic()
        print(f"收集 {task['id']} cwd={work}", flush=True)
        with (artifacts / 'output.log').open('wb') as log:
            with subprocess.Popen(command, cwd=work, env=environment, stdout=log, stderr=log, start_new_session=True) as process:
                record['pid'] = process.pid
                write_json(artifacts / 'command.json', record)
                try:
                    record['exit_code'] = managed_wait_sync(process)
                finally:
                    record.update(exit_code=process.returncode, finished_utc=utc(), wall_seconds=time.monotonic() - started)
                    write_json(artifacts / 'command.json', record)
        inventory = artifacts / 'inventory.json'
        if record['exit_code'] and not (record['exit_code'] == 5 and inventory.exists()
                                        and json.loads(inventory.read_text()) == []):
            raise ValueError(f"{task['id']}收集失败，见 {artifacts / 'output.log'}")
        nodes = json.loads(inventory.read_text())
        if len(nodes) != len(set(nodes)):
            raise ValueError(f"{task['id']}收集到重复节点，见 {artifacts / 'inventory.json'}")
        def release_collection():
            remove_owned_tree(work)
            remove_owned_tree(temporary)
            if (directory / 'python-cache').exists():
                remove_owned_tree(directory / 'python-cache')

        count = min(args.root_shards, len(nodes))
        if not nodes:
            result.append({**task, 'status': 'not_selected', 'exit_code': 5,
                           'collection': str(artifacts), 'collected_nodes': 0})
            release_collection()
            continue
        if count <= 1:
            result.append(task)
            release_collection()
            continue
        groups = [[] for _ in range(count)]
        totals = [0.0] * count
        for node in sorted(nodes, key=lambda node: (-costs.get(node, 1.0), node)):
            index = min(range(count), key=lambda index: (totals[index], index))
            groups[index].append(node)
            totals[index] += max(costs.get(node, 1.0), .001)
        assigned = [node for group in groups for node in group]
        if len(assigned) != len(nodes) or set(assigned) != set(nodes):
            raise ValueError(f"{task['id']}分片未完整且唯一地覆盖当前清单")
        for index, group in enumerate(groups):
            result.append({**task, 'id': task['id'] + f'/shard-{index + 1}',
                           'parent_id': task['id'], 'shard': index + 1, 'shards': count,
                           'targets': sorted(group, key=nodes.index), 'estimated_seconds': totals[index],
                           'collection': str(artifacts), 'collected_nodes': len(nodes)})
        release_collection()
    return sorted(result, key=lambda task: (-task['estimated_seconds'], task['id']))


async def managed_wait(process):
    try:
        return await process.wait()
    except asyncio.CancelledError:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 10)
            except asyncio.TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        # pytest 可能先于忽略 SIGINT 的构建后代退出。清理共享构建前
        # 还要终止本任务进程组中仍存活的直接受管后代。
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        while process_group_is_running(process.pid):
            await asyncio.sleep(.05)
        raise


def process_group_is_running(group):
    """检查本次任务组内是否仍有可以执行的进程；僵尸已经退出。"""
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(') ', 1)[1].split()
        except FileNotFoundError:
            continue
        if int(fields[2]) == group and fields[0] not in ('Z', 'X'):
            return True
    return False


async def run_blocking(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def execute(args, tasks, output, seed, python, cache, node_modules, versions, identity, started, started_utc):
    summary = {'started_utc': started_utc, 'source': {key: value for key, value in identity.items() if key != "files"}, 'versions': versions,
               'runner_pid': os.getpid(), 'jobs': args.jobs, 'scope': args.scope,
               'selection': args.target, 'test_kind': args.test_kind, 'status': 'running', 'tasks': tasks}
    queue = asyncio.Queue()
    for number, task in enumerate(tasks):
        task['number'] = number + 1
        if task.get('status') != 'not_selected':
            task['status'] = 'pending'
            queue.put_nowait(task)
    worker_count = min(args.jobs, queue.qsize())
    (output / 'workers').mkdir(exist_ok=True)
    for worker in range(worker_count):
        (output / 'workers' / str(worker)).mkdir(parents=True)
    control = output / 'control'
    control.mkdir(exist_ok=True)
    shutil.copy2(Path(__file__).with_name('integration_progress.py'), control)

    def save():
        summary.update(read_build_summaries(output))
        for task in tasks:
            if task.get('artifacts'):
                progress = read_progress(Path(task['artifacts']))
                if progress:
                    task['progress'] = progress
        summary['updated_utc'] = utc()
        summary['wall_seconds'] = time.monotonic() - started
        write_json(output / 'summary.json', summary)

    async def worker(number):
        private = output / 'workers' / str(number)
        task = queue.get_nowait()
        while True:
            directory = output / 'tasks' / f"{task['number']:03}-{task['id'].replace('/', '-')}"
            artifacts = directory / 'artifacts'
            artifacts.mkdir(parents=True)
            work = directory / 'source'
            task.update(status='preparing', worker=number, worker_cache=str(private),
                        artifacts=str(artifacts), cwd=str(work), started_utc=utc())
            task_started = time.monotonic()
            save()
            try:
                if task['scope'] != 'node' and not (private / 'uv-cache').exists():
                    await run_blocking(copy_tree, cache, private / 'uv-cache')
                if task['scope'] != 'camctl' and not (private / 'node_modules').exists():
                    if node_modules is None or not node_modules.is_dir():
                        raise ValueError('根与 Node 集成测试需要 --node-modules 指向已按锁文件准备的客户端依赖')
                    await run_blocking(copy_tree, node_modules, private / 'node_modules')
                await run_blocking(clone_snapshot, seed, work)
                project = work / 'apps/camctl'
                project.mkdir(parents=True, exist_ok=True)
                temporary = directory / 'tmp'
                temporary.mkdir()
                environment = test_environment(python, project, control, private, temporary)
                environment['CAMCTL_TEST_BUILD_ROOT'] = str(output)
                environment['CAMCTL_TEST_BUILD_SOURCE'] = str(seed)
                if task['scope'] == 'root':
                    environment['CAMCTL_HOST_DEMO_BUILD_DIR'] = str(output / 'host-demo-build')
                    environment['CAMCTL_HOST_DEMO_SOURCE'] = str(seed / 'apps/host-demo')
                    environment['CAMCTL_HOST_DEMO_RECEIPT'] = str(output / 'host-demo-build.json')
                if task['scope'] != 'node':
                    environment_root = project / '.venv311'
                    await run_blocking(subprocess.run,
                        [str(python), '-m', 'venv', '--without-pip', str(environment_root)], env=environment, check=True)
                    packages = environment_root / 'lib/python3.11/site-packages'
                    (packages / 'integration-dependencies.pth').write_text(
                        versions['site_packages'] + '\n', encoding='utf-8')
                if task['scope'] != 'camctl':
                    (work / 'apps/client').mkdir(parents=True, exist_ok=True)
                    (work / 'apps/client/node_modules').symlink_to(private / 'node_modules', target_is_directory=True)
                if task['scope'] == 'node':
                    command = ['node', '--disable-warning=ExperimentalWarning', '--test', '--test-reporter=tap', *task['targets']]
                    write_json(artifacts / 'inventory.json', task['targets'])
                else:
                    environment['CAMCTL_INTEGRATION_PROGRESS'] = str(artifacts)
                    root_arguments = [f'--rootdir={work}'] if task['scope'] == 'root' else []
                    command = [str(project / '.venv311/bin/python'), '-m', 'pytest', *root_arguments, *task['targets'],
                               '-q', '--durations=0', '-p', 'integration_progress',
                               '--basetemp', str(temporary / 'pytest'), '-p', 'camctl_memory_plugin',
                               '-p', 'test_kind_plugin', '--test-kind', args.test_kind,
                               *memory_pytest_arguments(args)]
                task['command'] = command
                task['environment'] = {key: environment[key] for key in (
                    'PYTHONPATH', 'PYTHONPYCACHEPREFIX', 'UV_PYTHON', 'UV_PROJECT_ENVIRONMENT',
                    'UV_CACHE_DIR', 'UV_OFFLINE', 'TMPDIR')}
                # 根测试也需要实际同步资源；仅写此任务的源码副本。
                async def prepare(label, command, cwd):
                    preparation = {'label': label, 'command': command, 'cwd': str(cwd), 'started_utc': utc()}
                    preparation_started = time.monotonic()
                    task.setdefault('preparations', []).append(preparation)
                    with (artifacts / 'prepare.log').open('ab') as log:
                        process = await asyncio.create_subprocess_exec(*command, cwd=cwd, env=environment,
                                                                      stdout=log, stderr=log, start_new_session=True)
                        preparation['pid'] = process.pid
                        task.update(preparation=label, preparation_pid=process.pid)
                        save()
                        try:
                            preparation['exit_code'] = await managed_wait(process)
                        finally:
                            preparation.update(exit_code=process.returncode, finished_utc=utc(),
                                               wall_seconds=time.monotonic() - preparation_started)
                            write_json(artifacts / 'preparations.json', task['preparations'])
                    if preparation['exit_code']:
                        raise RuntimeError(f'{label}准备失败，见 prepare.log')

                resource_script = project / 'scripts/sync_resources.py'
                if task['scope'] != 'node' and resource_script.exists():
                    await prepare('package-resources', [str(python), str(resource_script)], work)
                if task['path'] == HB_MODULE:
                    candidate = directory / 'client-candidate'
                    await run_blocking(clone_snapshot, seed, candidate)
                    (candidate / 'apps/client/node_modules').symlink_to(private / 'node_modules', target_is_directory=True)
                    environment['CAMCTL_HB_TEST_CLIENT_ROOT'] = str(candidate)
                    environment['npm_config_cache'] = str(private / 'npm-cache')
                    task['client_candidate'] = str(candidate)
                    task['environment']['CAMCTL_HB_TEST_CLIENT_ROOT'] = environment['CAMCTL_HB_TEST_CLIENT_ROOT']
                    await prepare('hasselblad-client-build', ['npm', 'run', 'build'], candidate / 'apps/client')
                with (artifacts / 'output.log').open('wb') as log:
                    process = await asyncio.create_subprocess_exec(*command, cwd=work, env=environment,
                                                                  stdout=log, stderr=log, start_new_session=True)
                    task.update(status='running', pid=process.pid, execution_started_utc=utc())
                    task['progress'] = {'status': 'starting', 'nodeid': task['path']}
                    write_json(artifacts / 'command.json', {**task, 'source': identity['snapshot_sha256'], 'versions': versions})
                    save()
                    print(f"启动 {task['id']} pid={process.pid} cwd={work}", flush=True)
                    task['exit_code'] = await managed_wait(process)
                if task['scope'] == 'node':
                    content = (artifacts / 'output.log').read_text(errors='replace')
                    counts = {name: int(value) for name, value in re.findall(r'^# (tests|pass|fail|skipped) (\d+)$', content, re.M)}
                    task['progress'] = {'status': 'finished', 'exit_code': task['exit_code'], 'outcomes': counts}
                    write_json(artifacts / 'progress.json', task['progress'])
                inventory = artifacts / 'inventory.json'
                if task['exit_code'] == 5 and inventory.exists() and json.loads(inventory.read_text()) == []:
                    task['status'] = 'not_selected'
                else:
                    task['status'] = 'passed' if task['exit_code'] == 0 else 'failed'
            except asyncio.CancelledError:
                task.update(status='interrupted', exit_code=130)
                raise
            except Exception as error:
                task.update(status='failed', exit_code=2, error=f'{type(error).__name__}: {error}')
            finally:
                task.update(finished_utc=utc(), wall_seconds=time.monotonic() - task_started)
                temporary = directory / 'tmp'
                if temporary.exists():
                    for host_build in temporary.glob('camctl-host-build-*'):
                        remove_owned_tree(host_build)
                save()
                write_json(artifacts / 'result.json', task)
                if task['status'] in ('passed', 'not_selected'):
                    try:
                        for name in ('source', 'tmp', 'client-candidate'):
                            path = directory / name
                            if path.exists():
                                remove_owned_tree(path)
                    except OSError as error:
                        task.update(status='failed', exit_code=2,
                                    error=f'清理任务环境失败: {error}')
                        save()
                        write_json(artifacts / 'result.json', task)
                print(f"结束 {task['id']} status={task['status']} exit={task.get('exit_code')} seconds={task['wall_seconds']:.3f}", flush=True)
            if queue.empty():
                break
            task = queue.get_nowait()

    save()
    workers = [asyncio.create_task(worker(number)) for number in range(worker_count)]
    last_print = time.monotonic()
    try:
        while not all(item.done() for item in workers):
            await asyncio.wait(workers, timeout=.5)
            save()
            if time.monotonic() - last_print >= 15:
                current = [f"{task['id']} pid={task.get('pid', task.get('preparation_pid', '-'))} {task.get('progress', {}).get('nodeid', task.get('preparation', task['status']))}"
                           for task in tasks if task['status'] in ('running', 'preparing')]
                print('进度 ' + ' | '.join(current), flush=True)
                last_print = time.monotonic()
        await asyncio.gather(*workers)
        failures = any(task['status'] == 'failed' for task in tasks)
        selected = any(task['status'] == 'passed' for task in tasks)
        summary['exit_code'] = 1 if failures or not selected else 0
        summary['status'] = 'finished' if selected or failures else 'not_selected'
    except asyncio.CancelledError:
        summary.update(status='interrupted', exit_code=130)
        for task in tasks:
            if task['status'] == 'pending':
                task.update(status='interrupted', exit_code=130)
        raise
    finally:
        for item in workers:
            if not item.done():
                item.cancel()
        results = await asyncio.gather(*workers, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result
        for private in (output / 'workers').iterdir():
            remove_owned_tree(private)
        for name in BUILD_NAMES:
            shared_build = output / f'{name}-build'
            if shared_build.exists():
                remove_owned_tree(shared_build)
        summary['finished_utc'] = utc()
        save()
    return summary['exit_code']


def main():
    started, started_utc = time.monotonic(), utc()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=4, help='最多同时运行的隔离任务数（默认 4）')
    parser.add_argument('--root-shards', type=int, help='历史长模块或曾失败根pytest模块最多分为几片（默认随jobs，1禁用）')
    parser.add_argument('--force-root-shards', action='store_true',
                        help='对本次所选根pytest模块收集并分片，不受历史时长和失败记录限制')
    parser.add_argument('--timing-history', type=Path, help='可选的本地计时 CSV 目录')
    parser.add_argument('--scope', choices=('all', 'camctl', 'root', 'node'), default='all')
    parser.add_argument('--test-kind', choices=TEST_KINDS, default=DEFAULT_TEST_KIND,
                        help='选择功能测试、性能专项、按需验收或全部测试')
    parser.add_argument('--target', action='append', default=[], help='仓库相对目录、文件或 pytest nodeid，可重复')
    parser.add_argument('--python', type=Path, default=Path(sys.executable), help='已准备测试依赖的外部 Python 3.11')
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1], help='待验证的 Git 工作区')
    parser.add_argument('--output', type=Path, help='新建的证据与隔离环境目录（默认 /tmp 下的新目录）')
    parser.add_argument('--node-modules', type=Path, help='已准备的客户端 node_modules（默认源工作区）')
    parser.add_argument('--uv-cache', type=Path, help='已准备的 uv 离线缓存（默认 uv cache dir）')
    parser.add_argument('--camctl-memory-limit', default=DEFAULT_MEMORY_LIMIT,
                        help='每个被测 camctl 及其全部后代共享的内存预算')
    parser.add_argument('--camctl-memory-cgroup-root', type=lambda value: Path(value).absolute(),
                        help='可创建专属预算组的 Linux 委派目录')
    preview = parser.add_mutually_exclusive_group()
    preview.add_argument('--list', action='store_true', help='只列出发现并选择的模块任务，不执行测试')
    preview.add_argument('--plan-only', action='store_true', help='冻结源码并收集根pytest模块的实际分片计划，不执行测试')
    args = parser.parse_args()
    output = None
    try:
        parse_memory_limit(args.camctl_memory_limit)
        if args.jobs < 1:
            raise ValueError('--jobs 必须大于 0')
        args.root_shards = args.root_shards if args.root_shards is not None else args.jobs
        if args.root_shards < 1:
            raise ValueError('--root-shards 必须大于 0')
        source = args.source.resolve()
        timing_directory = args.timing_history.resolve() if args.timing_history is not None else None
        history = load_timing_history(timing_directory)
        tasks = select(discover(source), args.scope, args.target)
        if args.test_kind not in ('functional', 'all'):
            tasks = [task for task in tasks if task['scope'] != 'node']
        if not tasks:
            raise ValueError('所选范围没有集成测试')
        for target in args.target:
            if not (source / target.partition('::')[0]).exists():
                raise ValueError(f'目标不存在: {target}')
        if args.list:
            print(json.dumps(tasks, ensure_ascii=False, indent=2))
            return 0
        python = args.python.absolute()
        version_environment = {key: value for key, value in os.environ.items()
                               if key not in ('PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX')}
        versions = json.loads(command_output([str(python), '-B', '-c',
            'import sys,sysconfig,sqlite3,json,pytest; print(json.dumps({"python":sys.version,"version":list(sys.version_info[:2]),"sqlite":sqlite3.sqlite_version,"pytest":pytest.__version__,"executable":sys.executable,"site_packages":sysconfig.get_path("purelib")}))'], env=version_environment))
        if versions['version'] != [3, 11]:
            raise ValueError('--python 必须使用部署版 Python 3.11')
        versions.update({name: command_output([name, '--version']) for name in ('uv', 'node', 'git')})
        cache = (args.uv_cache or Path(command_output(['uv', 'cache', 'dir']))).resolve()
        if not cache.is_dir():
            raise ValueError(f'uv 缓存目录不存在: {cache}')
        node_modules = (args.node_modules or source / 'apps/client/node_modules').resolve()
        if args.output:
            output = args.output.resolve()
            output.mkdir(parents=True, exist_ok=False)
        else:
            import tempfile
            output = Path(tempfile.mkdtemp(prefix='camctl-integration-'))
        print(f'证据目录 {output}', flush=True)
        write_json(output / 'summary.json', {'status': 'preparing', 'runner_pid': os.getpid(),
                   'started_utc': started_utc, 'test_kind': args.test_kind,
                   'tasks': [{**task, 'status': 'pending'} for task in tasks]})
        seed = output / 'snapshot'
        identity = snapshot(source, seed, timing_directory)
        write_json(output / 'source.json', identity)
        # 调度清单由冻结后的实际源码生成，确保与执行来源一致。
        tasks = select(discover(seed), args.scope, args.target)
        if args.test_kind not in ('functional', 'all'):
            tasks = [task for task in tasks if task['scope'] != 'node']
        tasks = order_by_measurement(tasks, history)
        (output / 'control').mkdir()
        shutil.copy2(Path(__file__).with_name('integration_progress.py'), output / 'control')
        shutil.copy2(Path(__file__).with_name('camctl_memory_plugin.py'),
                     output / 'control' / 'camctl_memory_plugin.py')
        shutil.copy2(Path(camctl_memory.__file__), output / 'control' / 'camctl_memory.py')
        shutil.copy2(Path(__file__).with_name('test_kind_plugin.py'), output / 'control')
        shutil.copy2(Path(__file__).with_name('round_test_build.py'), output / 'control')
        shutil.copy2(Path(__file__).with_name('host_demo_build.py'), output / 'control')
        tasks = collect_and_shard(args, tasks, output, seed, python, versions, identity, history[1])
        write_json(output / 'plan.json', tasks)
        if args.plan_only:
            write_json(output / 'summary.json', {'status': 'planned', 'exit_code': 0,
                'started_utc': started_utc, 'finished_utc': utc(), 'wall_seconds': time.monotonic() - started,
                **read_build_summaries(output),
                'source': {key: value for key, value in identity.items() if key != 'files'},
                'versions': versions, 'test_kind': args.test_kind, 'tasks': tasks})
            remove_owned_tree(seed)
            print(f'计划 {output / "plan.json"}；未执行测试。', flush=True)
            return 0
        result = asyncio.run(execute(args, tasks, output, seed, python, cache, node_modules, versions, identity, started, started_utc))
        if result == 0:
            remove_owned_tree(seed)
        print(f'汇总 {output / "summary.json"} exit={result}', flush=True)
        return result
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        if output is not None and (output / 'summary.json').exists():
            summary = json.loads((output / 'summary.json').read_text())
            summary.update(status='failed', exit_code=2, error=str(error), finished_utc=utc(),
                           wall_seconds=time.monotonic() - started)
            write_json(output / 'summary.json', summary)
        parser.error(str(error))
    except KeyboardInterrupt:
        if output is not None and (output / 'summary.json').exists():
            summary = json.loads((output / 'summary.json').read_text())
            summary.update(status='interrupted', exit_code=130, finished_utc=utc(), wall_seconds=time.monotonic() - started)
            write_json(output / 'summary.json', summary)
            print(f'运行已中断；已保存 {output / "summary.json"}。', file=sys.stderr)
        else:
            print('入口准备已中断，尚未生成任务汇总。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
