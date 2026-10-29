#!/usr/bin/env python3
"""只读重算归档计时；--output 必填，不执行测试或写入任何输入文件。

放在 docs/camctl/verification-data/<日期专题>/recompute.py 时自动定位仓库。
其他位置使用 --repo-root。示例：
python recompute.py --repo-root /path/to/repo --output /path/to/summary.json
"""
import argparse
import collections
import csv
import datetime
import hashlib
import json
from pathlib import Path

BASE = Path('docs/camctl/verification-data')
FULL = BASE / '2026-10-25-integration-optimization'
ROUND2 = BASE / '2026-10-25-integration-optimization-round2'
REPAIRS = BASE / '2026-10-25-integration-failure-repairs'
HISTORY = BASE / '2026-10-25-integration-timing'
PHASES = ('setup', 'call', 'teardown')
CAPACITY = {
    'test_large_growth_crosses_page_and_disk_cache_capacity',
    'test_fixed_batch_write_and_verification_have_bounded_residency',
    'test_large_window_releases_overflow_once_and_keyset_pages_do_not_rescan_window',
    'test_growth_has_bounded_python_peak_and_linear_projection_reads',
}


class Archive:
    def __init__(self, root):
        self.root, self.inputs = root, {}

    def read(self, relative, csv_rows=False):
        path = self.root / relative
        self.inputs[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
        with path.open(encoding='utf-8-sig', newline='') as stream:
            return list(csv.DictReader(stream)) if csv_rows else json.load(stream)


def identify_root(explicit):
    candidates = [explicit.resolve()] if explicit else Path(__file__).resolve().parents
    for root in candidates:
        if (root / FULL / 'final-run-manifest.json').is_file():
            return root
    raise ValueError('无法定位仓库；请指定 --repo-root。')


def elapsed(start, end):
    return (datetime.datetime.fromisoformat(end) - datetime.datetime.fromisoformat(start)).total_seconds()


def group(rows, key):
    groups = {}
    for row in rows:
        name = key(row)
        item = groups.setdefault(name, {'identity': name, 'cases': 0, 'seconds': 0,
                                         'outcomes': collections.Counter()})
        item['cases'] += 1
        item['seconds'] += row['seconds']
        item['outcomes'][row['outcome']] += 1
    return sorted(groups.values(), key=lambda item: (-item['seconds'], item['identity']))


def concentration(rows):
    costs = sorted((row['seconds'] for row in rows), reverse=True)
    total = sum(costs)
    return {'cases': len(costs), 'seconds': total,
            'top_share_percent': {str(n): 100 * sum(costs[:n]) / total
                                  for n in (1, 4, 10, 20, 50, 100) if total and len(costs) >= n}}


def phase_rows(cases, task=None):
    rows = []
    for case in cases:
        node = case['nodeid']
        if task and task['scope'] == 'camctl' and node.startswith('tests/'):
            node = 'apps/camctl/' + node
        rows.append({'nodeid': node, 'task': task['id'] if task else case['task'],
                     'outcome': case['outcome'], 'seconds': case['duration_seconds'],
                     **{phase + '_seconds': case.get('phases', {}).get(phase, {}).get('duration_seconds', 0)
                        for phase in PHASES}})
    return rows


def case_summary(rows, top):
    files = group(rows, lambda row: row['nodeid'].split('::')[0])
    functions = group(rows, lambda row: row['nodeid'].split('[')[0])
    return {'distribution': concentration(rows),
            'outcomes': collections.Counter(row['outcome'] for row in rows),
            'phases_seconds': {phase: sum(row[phase + '_seconds'] for row in rows) for phase in PHASES},
            'file_concentration': concentration(files), 'function_concentration': concentration(functions),
            'top_files': files[:top], 'top_functions': functions[:top],
            'top_cases': sorted(rows, key=lambda row: (-row['seconds'], row['nodeid']))[:top]}


def metadata(summary):
    return {'source': {key: value for key, value in summary['source'].items() if key != 'root'},
            **{key: summary[key] for key in ('started_utc', 'finished_utc', 'jobs', 'exit_code')},
            'versions': {key: summary.get('versions', {}).get(key) for key in ('python', 'sqlite', 'pytest', 'node')}}


def schedule(summary, tasks, phase_seconds, top):
    wall = summary['wall_seconds']
    total = sum(task['wall_seconds'] for task in tasks)
    py_tasks = [task for task in tasks if task['scope'] != 'node']
    py_wall = sum(task['wall_seconds'] for task in py_tasks)
    progress = sum(task.get('progress', {}).get('elapsed_seconds', 0) for task in py_tasks)
    preparations = collections.Counter()
    workers = []
    for task in tasks:
        for preparation in task.get('preparations', []):
            preparations[preparation['label']] += preparation['wall_seconds']
    for number in sorted({task['worker'] for task in tasks}):
        owned = sorted((task for task in tasks if task['worker'] == number), key=lambda task: task['started_utc'])
        work = sum(task['wall_seconds'] for task in owned)
        workers.append({'worker': number, 'tasks': len(owned), 'task_wall_seconds': work,
                        'runner_minus_task_sum_seconds': wall - work,
                        'utc_start_offset_seconds': elapsed(summary['started_utc'], owned[0]['started_utc']),
                        'utc_tail_seconds': elapsed(owned[-1]['finished_utc'], summary['finished_utc']),
                        'last_task': owned[-1]['id']})
    def task_row(task):
        return {key: task[key] for key in ('id', 'worker', 'wall_seconds', 'estimated_seconds', 'status')}
    slots = [0.0] * summary['jobs']
    for task in sorted(tasks, key=lambda task: -task['wall_seconds']):
        slots[min(range(len(slots)), key=lambda index: slots[index])] += task['wall_seconds']
    return {'tasks': len(tasks), 'task_outcomes': collections.Counter(task['status'] for task in tasks),
            'runner_wall_seconds': wall, 'utc_span_seconds': elapsed(summary['started_utc'], summary['finished_utc']),
            'task_wall_sum_seconds': total, 'pytest_task_wall_sum_seconds': py_wall,
            'node_task_wall_sum_seconds': total - py_wall,
            'pytest_phase_sum_seconds': phase_seconds,
            'all_task_minus_pytest_phase_seconds': total - phase_seconds,
            'pytest_task_minus_pytest_phase_seconds': py_wall - phase_seconds,
            'pytest_task_minus_progress_seconds': py_wall - progress,
            'pytest_progress_minus_phases_seconds': progress - phase_seconds,
            'explicit_preparations_seconds': preparations,
            'slot_occupancy_percent': 100 * total / (summary['jobs'] * wall),
            'fixed_task_average_lower_bound_seconds': total / summary['jobs'],
            'offline_actual_duration_descending_makespan_seconds': max(slots),
            'workers': workers,
            'top_tasks': [task_row(task) for task in sorted(tasks, key=lambda task: -task['wall_seconds'])[:top]],
            'last_finished_tasks': [{**task_row(task), 'finished_utc': task['finished_utc']}
                                    for task in sorted(tasks, key=lambda task: task['finished_utc'])[-4:]]}


def full_run(archive, top):
    manifest = archive.read(FULL / 'final-run-manifest.json')
    terminal = manifest['runner_terminal']
    summary = {**terminal, 'source': manifest['source'], 'versions': manifest['versions']}
    tasks = [record['result'] for record in manifest['task_execution_records']]
    raw = archive.read(FULL / 'final-run-cases.csv', csv_rows=True)
    rows = [{'nodeid': row['nodeid'], 'task': row['task'], 'scope': row['scope'],
             'outcome': row['reported_outcome'], 'seconds': float(row['duration_seconds'] or 0),
             **{phase + '_seconds': float(row[phase + '_seconds'] or 0) for phase in PHASES}}
            for row in raw if row['duration_basis'] == 'pytest_phase_sum']
    capacity = [row for row in rows if row['nodeid'].split('::')[-1].split('[')[0] in CAPACITY]
    result = case_summary(rows, top)
    phase_total = result['distribution']['seconds']
    result.update(metadata=metadata(summary),
                  primary_count=manifest['actual_primary_count'], primary_outcomes=manifest['primary_outcomes'],
                  primary_by_scope=manifest['primary_by_scope'], subchecks=manifest['subcheck_count'],
                  scope_distribution=group(rows, lambda row: row['scope']),
                  outcome_distribution=group(rows, lambda row: row['outcome']),
                  node_tap_sum_seconds=manifest['timing']['node_primary_duration_seconds'],
                  schedule=schedule(summary, tasks, phase_total, top), capacity_cases=capacity,
                  capacity_seconds=sum(row['seconds'] for row in capacity),
                  capacity_share_percent=100 * sum(row['seconds'] for row in capacity) / phase_total)
    return result


def selected_run(archive, base, top, round2=False):
    summary = archive.read(base / 'summary.json')
    if round2:
        cases = phase_rows(archive.read(base / 'cases.json'))
        manifest = archive.read(base / 'manifest.json')
    else:
        cases = []
        for task in summary['tasks']:
            directory = f"{task['number']:03}-{task['id'].replace('/', '-')}"
            cases.extend(phase_rows(archive.read(base / 'tasks' / directory / 'cases.json'), task))
    result = case_summary(cases, top)
    result.update(metadata=metadata(summary),
                  schedule=schedule(summary, summary['tasks'], result['distribution']['seconds'], top))
    if round2:
        result['original_manifest_timing'] = manifest['timing']
    return result


def rounded(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [rounded(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--top', type=int, default=20)
    args = parser.parse_args()
    if args.top < 1:
        parser.error('--top 必须大于零。')
    archive = Archive(identify_root(args.repo_root))
    result = {
        'schema_version': 1,
        'method': {
            'input_policy': '只读版本化归档，标准库执行，不启动测试；只写显式 --output。',
            'top_limit': args.top,
            'case_seconds': 'pytest setup/call/teardown之和；函数按参数展开前nodeid聚合；嵌套子检查不重复加时。',
            'task_seconds': '原任务单调墙钟，包含源码/依赖副本、环境、显式准备、pytest启动/收集/阶段及收场。',
            'runner_seconds': '原入口单调墙钟；任务和包含并发重叠，不能等同入口或CPU时间。',
            'difference': '任务减阶段不是安装成本；比较pytest任务与pytest阶段，另列Node任务和TAP。',
            'preparation': '显式准备仅同步资源与HB构建；测试函数体内建库、拍摄、浏览器和媒体准备仍可能归call。',
            'utc': 'UTC端点及尾部只用于定位；与单调计时存在未解释差值，不混算。',
            'simulation': '按实测任务时长降序分配最空槽，仅为固定耗时离线模拟，不含入口准备或负载变化。',
            'failure': '失败只计至实际失败处；不同源码/范围/完成量的批次独立保存，不合成为当前全量。',
        },
        'full_run': full_run(archive, args.top),
        'round2_runs': {name: selected_run(archive, ROUND2 / name, args.top, True)
                        for name in ('hb-after-4', 'cleanup-after-4')},
        'repair_runs': {name: selected_run(archive, REPAIRS / name, args.top)
                        for name in ('combined-mainline', 'browser-final')},
    }
    history = archive.read(HISTORY / 'cross-component-cases.csv', csv_rows=True)
    result['historical_weight_examples'] = [
        {key: row[key] for key in ('nodeid', 'status', 'total_s')}
        for row in history if 'test_installed_hasselblad_parameter_validation_agrees_across_consumers' in row['nodeid']
        or 'test_cleanup_query_present_or_unknown' in row['nodeid']
        or 'test_cleanup_saved_result_and_terminal_companions' in row['nodeid']]
    result['input_sha256'] = archive.inputs
    if args.output.resolve() in {(archive.root / path).resolve() for path in archive.inputs}:
        parser.error('--output 不能覆盖本次读取的源归档。')
    args.output.write_text(json.dumps(rounded(result), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
