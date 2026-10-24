#!/usr/bin/env python3
"""在独立源码副本与工具环境中并行运行完整或指定范围的集成测试。"""
from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

CAMCTL = 'apps/camctl/tests/integration'
ROOT = 'tests/integration'


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
    for target in dict.fromkeys(targets):
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


def order_by_measurement(source, tasks):
    """既有逐文件测量只用于排程；未测目标仍完整执行。"""
    timing = source / 'docs/camctl/verification-data/2026-10-25-integration-timing'
    costs = {}
    for name in ('camctl-files.csv', 'cross-component-files.csv'):
        path = timing / name
        if path.exists():
            with path.open(encoding='utf-8-sig', newline='') as stream:
                for row in csv.DictReader(stream):
                    costs[row['name']] = float(row['total_s'])
    for task in tasks:
        task['estimated_seconds'] = sum(cost for path, cost in costs.items()
                                        if path == task['path'] or path.startswith(task['path'] + '/'))
    return sorted(tasks, key=lambda task: (-task['estimated_seconds'], task['id']))


def command_output(command, cwd=None):
    return subprocess.check_output(command, cwd=cwd, text=True).strip()


def copy_tree(source, destination):
    destination.mkdir(parents=True, exist_ok=True)
    # GNU cp 在支持的文件系统上使用写时复制，否则复制文件；绝不使用硬链接。
    subprocess.run(['cp', '-a', '--reflink=auto', str(source) + '/.', str(destination)], check=True)


def snapshot(source, destination):
    head = command_output(['git', 'rev-parse', 'HEAD'], source)
    files = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=source)
    paths = sorted(set(os.fsdecode(path) for path in files.split(b'\0') if path))
    subprocess.run(['git', 'clone', '--shared', '--no-checkout', '--quiet', str(source), str(destination)], check=True)
    subprocess.run(['git', 'read-tree', 'HEAD'], cwd=destination, check=True)
    inventory = []
    for relative in paths:
        original = source / relative
        if not original.exists() and not original.is_symlink():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if original.is_symlink():
            target.symlink_to(os.readlink(original))
            content = os.fsencode(os.readlink(original))
        elif original.is_file():
            shutil.copy2(original, target)
            content = target.read_bytes()
        else:
            raise ValueError(f'源码快照不支持此对象: {relative}')
        inventory.append({'path': relative, 'sha256': hashlib.sha256(content).hexdigest()})
    return {'root': str(source), 'head': head,
            'status': command_output(['git', 'status', '--short'], source),
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


async def execute(args, tasks, output, seed, python, cache, node_modules, versions, identity, started, started_utc):
    summary = {'started_utc': started_utc, 'source': {key: value for key, value in identity.items() if key != "files"}, 'versions': versions,
               'runner_pid': os.getpid(), 'jobs': args.jobs, 'scope': args.scope,
               'selection': args.target, 'status': 'running', 'tasks': tasks}
    queue = asyncio.Queue()
    for number, task in enumerate(tasks):
        task.update(status='pending', number=number + 1)
        queue.put_nowait(task)
    for worker in range(min(args.jobs, len(tasks))):
        (output / 'workers' / str(worker)).mkdir(parents=True)
    control = output / 'control'
    control.mkdir()
    shutil.copy2(Path(__file__).with_name('integration_progress.py'), control)

    def save():
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
            task.update(status='preparing', worker=number, artifacts=str(artifacts), cwd=str(work), started_utc=utc())
            task_started = time.monotonic()
            save()
            try:
                if not (private / 'uv-cache').exists():
                    await asyncio.to_thread(copy_tree, cache, private / 'uv-cache')
                if task['scope'] != 'camctl' and not (private / 'node_modules').exists():
                    if node_modules is None or not node_modules.is_dir():
                        raise ValueError('根与 Node 集成测试需要 --node-modules 指向已按锁文件准备的客户端依赖')
                    await asyncio.to_thread(copy_tree, node_modules, private / 'node_modules')
                await asyncio.to_thread(clone_snapshot, seed, work)
                project = work / 'apps/camctl'
                project.mkdir(parents=True, exist_ok=True)
                environment_root = project / '.venv311'
                await asyncio.to_thread(subprocess.run,
                    [str(python), '-B', '-m', 'venv', '--without-pip', str(environment_root)], check=True)
                packages = environment_root / 'lib/python3.11/site-packages'
                (packages / 'integration-dependencies.pth').write_text(
                    versions['site_packages'] + '\n', encoding='utf-8')
                if task['scope'] != 'camctl':
                    (work / 'apps/client').mkdir(parents=True, exist_ok=True)
                    (work / 'apps/client/node_modules').symlink_to(private / 'node_modules', target_is_directory=True)
                temporary = directory / 'tmp'
                temporary.mkdir()
                environment = {key: value for key, value in os.environ.items()
                               if key not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'PWD')
                               and not key.startswith(('UV_', 'CAMCTL_', 'PYTEST_'))}
                environment.update(PYTHONPATH=os.pathsep.join([str(control), str(project / 'src')]),
                                   PYTHONNOUSERSITE='1', PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1', UV_PYTHON=str(python),
                                   UV_PROJECT_ENVIRONMENT=str(private / 'project-env'),
                                   UV_CACHE_DIR=str(private / 'uv-cache'), UV_OFFLINE='1',
                                   TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
                if task['scope'] == 'node':
                    command = ['node', '--disable-warning=ExperimentalWarning', '--test', '--test-reporter=tap', *task['targets']]
                    write_json(artifacts / 'inventory.json', task['targets'])
                else:
                    environment['CAMCTL_INTEGRATION_PROGRESS'] = str(artifacts)
                    command = [str(project / '.venv311/bin/python'), '-m', 'pytest', *task['targets'],
                               '-q', '--durations=0', '-p', 'integration_progress',
                               '--basetemp', str(temporary / 'pytest')]
                task['command'] = command
                task['environment'] = {key: environment[key] for key in (
                    'PYTHONPATH', 'UV_PYTHON', 'UV_PROJECT_ENVIRONMENT', 'UV_CACHE_DIR', 'UV_OFFLINE', 'TMPDIR')}
                # 根测试也需要实际同步资源；仅写此任务的源码副本。
                resource_script = project / 'scripts/sync_resources.py'
                with (artifacts / 'prepare.log').open('wb') as log:
                    if resource_script.exists():
                        prepared = await asyncio.create_subprocess_exec(str(python), str(resource_script), cwd=work,
                                                                        env=environment, stdout=log, stderr=log)
                        if await prepared.wait():
                            raise RuntimeError('包资源准备失败，见 prepare.log')
                with (artifacts / 'output.log').open('wb') as log:
                    process = await asyncio.create_subprocess_exec(*command, cwd=work, env=environment,
                                                                  stdout=log, stderr=log, start_new_session=True)
                    task.update(status='running', pid=process.pid, execution_started_utc=utc())
                    task['progress'] = {'status': 'starting', 'nodeid': task['path']}
                    write_json(artifacts / 'command.json', {**task, 'source': identity['snapshot_sha256'], 'versions': versions})
                    save()
                    print(f"启动 {task['id']} pid={process.pid} cwd={work}", flush=True)
                    try:
                        task['exit_code'] = await process.wait()
                    except asyncio.CancelledError:
                        os.killpg(process.pid, signal.SIGINT)
                        try:
                            await asyncio.wait_for(process.wait(), 10)
                        except asyncio.TimeoutError:
                            os.killpg(process.pid, signal.SIGKILL)
                            await process.wait()
                        raise
                if task['scope'] == 'node':
                    content = (artifacts / 'output.log').read_text(errors='replace')
                    counts = {name: int(value) for name, value in re.findall(r'^# (tests|pass|fail|skipped) (\d+)$', content, re.M)}
                    task['progress'] = {'status': 'finished', 'exit_code': task['exit_code'], 'outcomes': counts}
                    write_json(artifacts / 'progress.json', task['progress'])
                task['status'] = 'passed' if task['exit_code'] == 0 else 'failed'
            except asyncio.CancelledError:
                task.update(status='interrupted', exit_code=130)
                raise
            except Exception as error:
                task.update(status='failed', exit_code=2, error=f'{type(error).__name__}: {error}')
            finally:
                task.update(finished_utc=utc(), wall_seconds=time.monotonic() - task_started)
                save()
                write_json(artifacts / 'result.json', task)
                print(f"结束 {task['id']} status={task['status']} exit={task.get('exit_code')} seconds={task['wall_seconds']:.3f}", flush=True)
            if queue.empty():
                break
            task = queue.get_nowait()

    save()
    workers = [asyncio.create_task(worker(number)) for number in range(min(args.jobs, len(tasks)))]
    last_print = time.monotonic()
    try:
        while not all(item.done() for item in workers):
            await asyncio.wait(workers, timeout=.5)
            save()
            if time.monotonic() - last_print >= 15:
                current = [f"{task['id']} pid={task.get('pid', '-')} {task.get('progress', {}).get('nodeid', task['status'])}"
                           for task in tasks if task['status'] in ('running', 'preparing')]
                print('进度 ' + ' | '.join(current), flush=True)
                last_print = time.monotonic()
        await asyncio.gather(*workers)
        summary['exit_code'] = 1 if any(task['exit_code'] != 0 for task in tasks) else 0
        summary['status'] = 'finished'
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
        await asyncio.gather(*workers, return_exceptions=True)
        summary['finished_utc'] = utc()
        save()
    return summary['exit_code']


def main():
    started, started_utc = time.monotonic(), utc()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=4, help='最多同时运行的隔离任务数（默认 4）')
    parser.add_argument('--scope', choices=('all', 'camctl', 'root', 'node'), default='all')
    parser.add_argument('--target', action='append', default=[], help='仓库相对目录、文件或 pytest nodeid，可重复')
    parser.add_argument('--python', type=Path, default=Path(sys.executable), help='已准备测试依赖的外部 Python 3.11')
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1], help='待验证的 Git 工作区')
    parser.add_argument('--output', type=Path, help='新建的证据与隔离环境目录（默认 /tmp 下的新目录）')
    parser.add_argument('--node-modules', type=Path, help='已准备的客户端 node_modules（默认源工作区）')
    parser.add_argument('--uv-cache', type=Path, help='已准备的 uv 离线缓存（默认 uv cache dir）')
    parser.add_argument('--list', action='store_true', help='只列出发现并选择的任务，不执行测试')
    args = parser.parse_args()
    try:
        if args.jobs < 1:
            raise ValueError('--jobs 必须大于 0')
        source = args.source.resolve()
        tasks = select(discover(source), args.scope, args.target)
        if not tasks:
            raise ValueError('所选范围没有集成测试')
        for target in args.target:
            if not (source / target.partition('::')[0]).exists():
                raise ValueError(f'目标不存在: {target}')
        if args.list:
            print(json.dumps(tasks, ensure_ascii=False, indent=2))
            return 0
        python = args.python.absolute()
        versions = json.loads(command_output([str(python), '-B', '-c',
            'import sys,sysconfig,sqlite3,json,pytest; print(json.dumps({"python":sys.version,"version":list(sys.version_info[:2]),"sqlite":sqlite3.sqlite_version,"pytest":pytest.__version__,"executable":sys.executable,"site_packages":sysconfig.get_path("purelib")}))']))
        if versions['version'] != [3, 11]:
            raise ValueError('--python 必须使用部署版 Python 3.11')
        versions.update({name: command_output([name, '--version']) for name in ('uv', 'node', 'git')})
        cache = (args.uv_cache or Path(command_output(['uv', 'cache', 'dir']))).resolve()
        if not cache.is_dir():
            raise ValueError(f'uv 缓存目录不存在: {cache}')
        node_modules = (args.node_modules or source / 'apps/client/node_modules').resolve()
        if args.output:
            output = args.output.absolute()
            output.mkdir(parents=True, exist_ok=False)
        else:
            import tempfile
            output = Path(tempfile.mkdtemp(prefix='camctl-integration-'))
        print(f'证据目录 {output}', flush=True)
        seed = output / 'snapshot'
        identity = snapshot(source, seed)
        write_json(output / 'source.json', identity)
        # 调度清单由冻结后的实际源码生成，确保与执行来源一致。
        tasks = order_by_measurement(seed, select(discover(seed), args.scope, args.target))
        write_json(output / 'plan.json', tasks)
        result = asyncio.run(execute(args, tasks, output, seed, python, cache, node_modules, versions, identity, started, started_utc))
        print(f'汇总 {output / "summary.json"} exit={result}', flush=True)
        return result
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        print('运行已中断；已保存当前 summary.json。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
