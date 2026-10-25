#!/usr/bin/env python3
"""核对归档计划的目标身份与 Git 源码派生范围，不执行或收集测试。

final-plan-audit.json 保存运行入口实际生成的任务目标及分片观察。
本脚本默认只依赖同目录证据、原计时 CSV 和记录的 Git 提交。
--run 可另外核对保留的原运行目录；运行命令和核对边界见归档 JSON。
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import itertools
import json
from pathlib import Path
import subprocess

from derive_scope import CSV_NAMES, TIMING, derive

TARGET_FIELDS = ('id', 'scope', 'path', 'targets', 'parent_id', 'shard', 'shards', 'collected_nodes')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def projection(tasks):
    return [{key: task[key] for key in TARGET_FIELDS if key in task} for task in tasks]


def covers(target, node):
    return target == node or any(node.startswith(target + boundary) for boundary in ('/', '::', '['))


def source_modules(repo, ref):
    files = subprocess.check_output([
        'git', 'ls-tree', '-r', '--name-only', ref, '--',
        'apps/camctl/tests/integration', 'tests/integration'], cwd=repo, text=True).splitlines()
    modules = set()
    for file in files:
        path = Path(file)
        if file.startswith('apps/camctl/tests/integration/') and path.name.startswith('test_') and path.suffix == '.py':
            modules.add(('camctl', '/'.join(path.parts[:5])))
        elif path.parent.as_posix() == 'tests/integration':
            if path.name.startswith('test_') and path.suffix == '.py':
                modules.add(('root', file))
            elif path.name.endswith('.test.mjs'):
                modules.add(('node', file))
    return modules


def validate(repo, evidence_path, expected_path, run=None):
    evidence, archived_expected = read_json(evidence_path), read_json(expected_path)
    source = evidence['source']
    assert archived_expected['source_ref'] == source['head']
    expected_record, expected, baseline = derive(repo, source['head'])
    for key in ('baseline_count', 'baseline_outcomes', 'expected_total', 'expected_scopes',
                'expected_subchecks', 'groups', 'additions'):
        assert expected_record[key] == archived_expected[key], ('范围派生不一致', key)
    normalized_skips = [{key: row[key] for key in ('suite', 'nodeid', 'reason')}
                        for row in expected_record['skip_baseline']]
    assert normalized_skips == archived_expected['skip_baseline']
    windows_skips = sum('Windows' in row['reason'] for row in normalized_skips)
    assert len(normalized_skips) == evidence['known_skip_boundary']['expected']
    assert windows_skips == evidence['known_skip_boundary']['windows_only']
    assert len(normalized_skips) - windows_skips == evidence['known_skip_boundary']['nonapplicable_parameter_partitions']
    csv_records = archived_expected['input_sources']['csv_files']
    assert {record['path'] for record in csv_records} == {str(Path(TIMING) / name) for name in CSV_NAMES}
    assert len(csv_records) == len(CSV_NAMES)
    for record in csv_records:
        assert hashlib.sha256((repo / record['path']).read_bytes()).hexdigest() == record['sha256'], record['path']
    assert source['status'] == ''
    assert evidence['expected_scopes'] == archived_expected['expected_scopes']
    assert evidence['baseline_total'] == archived_expected['baseline_count']
    assert evidence['integration_node_delta'] == len(expected) - len(baseline)
    assert evidence['missing'] == evidence['unexpected'] == []
    tasks = evidence['task_targets']
    assert len(tasks) == len({task['id'] for task in tasks}) == evidence['plan_tasks']
    modules = {(task['scope'], task['path']) for task in tasks}
    assert modules == source_modules(repo, source['head'])
    assert dict(Counter(task['scope'] for task in tasks)) == evidence['plan_task_scopes']
    assert dict(Counter(scope for scope, _ in modules)) == evidence['unique_module_scopes']
    assigned = Counter()
    for task in tasks:
        assert all(covers(task['path'], target) for target in task['targets']), ('目标不属于任务路径', task['id'])
        selected = [node for target in task['targets'] for node in expected if covers(target, node)]
        assert selected, ('空目标', task['id'])
        assert len(selected) == len(set(selected)), ('任务内部目标重叠', task['id'])
        assert all(any(covers(target, node) for node in expected) for target in task['targets'])
        assigned.update(selected)
    assert set(assigned) == expected
    assert all(count == 1 for count in assigned.values())
    assert len(assigned) == evidence['planned_primary_union_count'] == evidence['expected_total']
    assert evidence['planned_primary_duplicate_count'] == 0

    parents = defaultdict(list)
    for task in tasks:
        if task.get('parent_id'):
            parents[task['parent_id']].append(task)
    witnesses = {row['parent']: row for row in evidence['partitions']}
    assert set(parents) == set(witnesses)
    for parent, shards in parents.items():
        witness = witnesses[parent]
        assert {task['shard'] for task in shards} == set(range(1, len(shards) + 1))
        assert all(task['shards'] == len(shards) for task in shards)
        targets = [node for task in shards for node in task['targets']]
        selected = {node for node in expected if node.partition('::')[0] == shards[0]['path']}
        assert len(targets) == len(set(targets)) == len(selected) == witness['collected_nodes']
        assert set(targets) == selected
        assert all(task['collected_nodes'] == len(targets) for task in shards)
        assert [len(task['targets']) for task in sorted(shards, key=lambda row: row['shard'])] == witness['per_shard']

    hb_path = 'tests/integration/test_hasselblad_photo_roundtrip.py'
    hb = {node for task in tasks if task['path'] == hb_path for node in task['targets']}
    prefix = hb_path + '::test_installed_hasselblad_photo_roundtrip['
    normal = {node for node in hb if node.startswith(prefix)}
    assert normal == {node for node in baseline if node.startswith(prefix)}
    assert len(normal) == evidence['hasselblad']['normal_matrix_collected'] == 12
    combinations = {tuple(node[len(prefix):-1].split('-')) for node in normal}
    dimensions = [{row[index] for row in combinations} for index in range(3)]
    assert combinations == set(itertools.product(*dimensions))
    assert list(map(len, dimensions)) == [2, 2, 3]
    assert len(hb) == evidence['hasselblad']['collected_total']
    assert sorted(dimensions[0]) == evidence['hasselblad']['normal_formats']
    assert sorted(dimensions[1]) == evidence['hasselblad']['normal_sources']
    assert sorted(dimensions[2]) == evidence['hasselblad']['normal_shutters']
    node_files = {path for scope, path in modules if scope == 'node'}
    assert node_files == {row['file'] for row in baseline.values() if row['suite'] == 'root-node'}
    assert sorted(node_files) == evidence['node']['planned_files']
    assert sum(row['suite'] == 'root-node' for row in baseline.values()) == evidence['node']['expected_leaf_cases'] == 118
    assert archived_expected['expected_subchecks'] == evidence['subchecks']['expected'] == 342
    for record in evidence['already_collected_component_inventories']:
        task = next(task for task in tasks if task['id'] == record['task'])
        size = sum(node.startswith(task['path'] + '/') for node in expected)
        assert size == record['actual_collected'] == record['expected']

    if run is not None:
        identity = read_json(run / 'source.json')
        assert all(identity[key] == source[key] for key in ('head', 'status', 'snapshot_sha256'))
        assert projection(read_json(run / 'plan.json')) == tasks
        for witness in evidence['partitions']:
            path = run / 'collection' / witness['parent'].replace('/', '-') / 'artifacts/inventory.json'
            assert hashlib.sha256(path.read_bytes()).hexdigest() == witness['inventory_sha256']
            targets = [node for task in parents[witness['parent']] for node in task['targets']]
            assert Counter(read_json(path)) == Counter(targets)
        for record in evidence['already_collected_component_inventories']:
            task = next(task for task in tasks if task['id'] == record['task'])
            index = next(index for index, item in enumerate(tasks, 1) if item['id'] == task['id'])
            path = run / 'tasks' / f"{index:03}-{task['id'].replace('/', '-')}" / 'artifacts/inventory.json'
            assert hashlib.sha256(path.read_bytes()).hexdigest() == record['inventory_sha256']
            actual = [('apps/camctl/' + node if node.startswith('tests/integration/') else node) for node in read_json(path)]
            assert len(actual) == len(set(actual)) == record['actual_collected']
            assert set(actual) == {node for node in expected if node.startswith(task['path'] + '/')}
    return {'source': source['head'], 'planned_primary_union': len(assigned), 'duplicate_count': 0,
            'tasks': len(tasks), 'hasselblad_normal_collected': len(normal),
            'node_expected': 118, 'subchecks_expected': 342,
            'raw_run_inputs_rechecked': run is not None,
            'boundary': '范围与身份核对通过；没有执行测试，也没有判断 Node、子检查或业务测试的实际结果。'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--expected-scope', type=Path, required=True)
    parser.add_argument('--run', type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.repo, args.evidence, args.expected_scope, args.run), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
