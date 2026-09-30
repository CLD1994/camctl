"""从统一历史对象登记生成 SQL 中的类型范围与字典序标识。"""
import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def generated_blocks(objects):
    ids, tables = set(), set()
    for name, rule in objects.items():
        assert re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name), f'非法对象标识：{name}'
        assert set(rule) == {'id', 'table', 'report_target', 'snapshot'}, f'对象登记字段错误：{name}'
        assert type(rule['id']) is int and 0 < rule['id'] <= 2**63 - 1 and rule['id'] not in ids, f'对象编号错误：{name}'
        assert re.fullmatch(r'[a-z_][a-z0-9_]*', rule['table']) and rule['table'] not in tables, f'对象主表错误：{name}'
        assert type(rule['report_target']) is bool and type(rule['snapshot']) is bool, f'对象资格错误：{name}'
        ids.add(rule['id'])
        tables.add(rule['table'])
    ordered = sorted(objects.items(), key=lambda pair: pair[1]['id'])
    def accepted(flag=None):
        values = [str(rule['id']) for _, rule in ordered if flag is None or rule[flag]]
        assert values, f'对象范围为空：{flag}'
        return ','.join(values)
    def constraint(flag=None):
        return f'    entity_type INTEGER NOT NULL CHECK (entity_type IN ({accepted(flag)})),'
    cases = '\n'.join(f"        WHEN {rule['id']} THEN '{name}'" for name, rule in ordered if rule['snapshot'])
    return {
        'history_types': constraint(),
        'report_types': constraint('report_target'),
        'snapshot_types': constraint('snapshot'),
        'progress_types': constraint('snapshot'),
        'snapshot_type_name': '    entity_type_name TEXT GENERATED ALWAYS AS (CASE entity_type\n' + cases + '\n    END) VIRTUAL,'
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true', help='更新生成区段；默认只检查')
    args = parser.parse_args()
    registry = json.loads((ROOT / 'docs/camctl/database/enum-registry.json').read_text())
    path = ROOT / 'docs/camctl/database/schema/history.sql'
    current = path.read_text()
    expected = current
    for name, body in generated_blocks(registry['history_objects']).items():
        start, end = f'-- 登记生成开始：{name}', f'-- 登记生成结束：{name}'
        pattern = re.escape(start) + r'\n.*?' + re.escape(end)
        expected, count = re.subn(pattern, lambda _: start + '\n' + body + '\n' + end, expected, flags=re.S)
        assert count == 1, f'生成区段缺失或重复：{name}'
    if args.write:
        path.write_text(expected)
    else:
        assert expected == current, '历史对象 SQL 与登记不一致；运行本脚本 --write 同步后复核差异'
    print('历史对象 SQL 已生成。' if args.write else '历史对象 SQL 与统一登记一致。')


if __name__ == '__main__':
    main()
