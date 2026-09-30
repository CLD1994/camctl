"""用真实 SQLite 检查数据库结构规格；不实现业务事件或设备流程。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from contextlib import closing, contextmanager

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / 'docs/camctl/database/schema'
checks = 0


def verify(condition: bool, label: str) -> None:
    global checks
    if not condition:
        raise AssertionError(label)
    checks += 1


def insert(db: sqlite3.Connection, table: str, **row: object) -> None:
    # 标识符仅来自本文件的结构用例；业务值始终绑定参数。
    db.execute(f'INSERT INTO {table} ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))


@contextmanager
def case(db: sqlite3.Connection):
    db.execute('SAVEPOINT spec_case')
    try:
        yield
    finally:
        db.execute('ROLLBACK TO spec_case')
        db.execute('RELEASE spec_case')


def rejects(db: sqlite3.Connection, sql: str, params: tuple = ()) -> None:
    with case(db):
        try:
            db.execute(sql, params)
        except sqlite3.IntegrityError:
            verify(True, '拒绝非法结构')
        else:
            raise AssertionError(f'未拒绝非法结构：{sql}')


def read_unique_json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AssertionError(f'JSON 键重复：{path}/{key}')
            result[key] = value
        return result
    return json.loads(path.read_text(), object_pairs_hook=unique)


def version_components(value, size: int) -> bool:
    return (isinstance(value, (tuple, list)) and len(value) == size
            and all(type(part) is int and part >= 0 for part in value) and value[0] > 0)


def read_runtime_requirements() -> dict:
    rules = read_unique_json(ROOT / 'docs/camctl/sqlite-runtime.json')
    verify(set(rules) == {'format_version', 'python_minimum', 'sqlite_mainline_minimum',
                          'sqlite_fixed_backports', 'source'}, '运行库版本条件成员完整')
    verify(type(rules['format_version']) is int and rules['format_version'] == 1, '运行库版本条件格式为 1')
    verify(version_components(rules['python_minimum'], 2), 'Python 下限是主次版本整数数组')
    verify(version_components(rules['sqlite_mainline_minimum'], 3), 'SQLite 主线下限是三个版本整数')
    backports = rules['sqlite_fixed_backports']
    verify(isinstance(backports, list) and all(version_components(v, 3) for v in backports), '回移修复版本逐项列明')
    verify(len({tuple(v) for v in backports}) == len(backports), '回移修复版本不重复')
    verify(all(tuple(v) < tuple(rules['sqlite_mainline_minimum']) for v in backports), '回移例外低于主线下限')
    verify(isinstance(rules['source'], str) and rules['source'].startswith('https://'), '版本条件提供核验来源')
    return rules


def sqlite_version_allowed(version, rules: dict) -> bool:
    if not version_components(version, 3):
        return False
    return (tuple(version) >= tuple(rules['sqlite_mainline_minimum'])
            or tuple(version) in {tuple(v) for v in rules['sqlite_fixed_backports']})


def check_runtime_doc(rules: dict, write_doc: bool) -> None:
    def number(parts):
        return '.'.join(map(str, parts))
    start = '<!-- SQLite 版本条件生成开始 -->'
    end = '<!-- SQLite 版本条件生成结束 -->'
    doc = ROOT / 'docs/camctl/sqlite-runtime.md'
    content = doc.read_text()
    verify(content.count(start) == content.count(end) == 1, '版本表有唯一生成边界')
    before, body = content.split(start)
    _, after = body.split(end)
    rows = [start, '', '| 条件 | 允许范围 |', '| --- | --- |',
            f'| Python 接口 | {number(rules["python_minimum"])} 及后续版本 |',
            f'| SQLite 主线 | {number(rules["sqlite_mainline_minimum"])} 及后续版本 |',
            f'| SQLite 旧分支例外 | {"、".join(number(v) for v in rules["sqlite_fixed_backports"]) or "无"}；仅精确匹配这些版本 |',
            '| 其他 SQLite 版本 | 不允许 |', '', end]
    generated = before + '\n'.join(rows) + after
    if write_doc:
        doc.write_text(generated)
    else:
        verify(content == generated, '版本表须与统一条件一致；使用 --write-runtime-doc 更新后复核')


def check_runtime(rules: dict) -> None:
    verify(sys.version_info[:2] >= tuple(rules['python_minimum']), '当前 Python 满足接口版本要求')
    verify(sqlite_version_allowed(sqlite3.sqlite_version_info, rules),
           f'Python 实际链接的 SQLite {sqlite3.sqlite_version} 满足统一版本条件')
    verify(sqlite3.threadsafety in (1, 3), 'SQLite 支持不同线程独立使用各自连接')
    verify(callable(getattr(sqlite3.Connection, 'blobopen', None)), 'Python 提供增量 BLOB 接口')
    # 临时文件库只验证运行库基本能力，不打开业务库，也不验证目标主机的断电持久性。
    with tempfile.TemporaryDirectory(prefix='camctl-sqlite-check-') as directory:
        path = Path(directory) / 'probe.db'
        with closing(sqlite3.connect(path)) as db:
            verify(db.execute('SELECT sqlite_version()').fetchone()[0] == sqlite3.sqlite_version,
                   'SQL 与 Python 报告同一实际运行库版本')
            source_id = db.execute('SELECT sqlite_source_id()').fetchone()[0]
            verify(isinstance(source_id, str) and bool(source_id), '运行库提供构建身份')
            verify(db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal', '文件检查库实际启用 WAL')
            db.execute('PRAGMA synchronous=FULL')
            verify(db.execute('PRAGMA synchronous').fetchone()[0] == 2, '检查库写连接实际采用 FULL 同步')
            db.execute('PRAGMA foreign_keys=ON')
            verify(db.execute('PRAGMA foreign_keys').fetchone()[0] == 1, '检查库支持外键约束')
            db.execute('CREATE TABLE runtime_probe(id INTEGER PRIMARY KEY, value INTEGER, content BLOB) STRICT')
            rejects(db, 'INSERT INTO runtime_probe(id,value) VALUES(1,?)', ('invalid-integer',))
            doc = '{"value":7}'
            verify(db.execute("SELECT json_valid(?),json_type(?,'$.value'),json_extract(?,'$.value')",
                              (doc, doc, doc)).fetchone() == (1, 'integer', 7), '实际 JSON 函数返回正确类型和值')
            db.execute('INSERT INTO runtime_probe(id,value,content) VALUES(1,7,zeroblob(6))')
            with db.blobopen('runtime_probe', 'content', 1) as blob:
                blob.write(b'cam')
                blob.write(b'ctl')
            db.commit()
            with closing(sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)) as reader:
                with reader.blobopen('runtime_probe', 'content', 1, readonly=True) as blob:
                    verify(blob.read() == b'camctl', '独立只读连接读回已提交的分段 BLOB 字节')
    print(f'运行库检查通过：Python {sys.version.split()[0]}（{sys.executable}），SQLite {sqlite3.sqlite_version}。')
    print(f'SQLite 构建身份：{source_id}')
    print('已检查 STRICT、JSON、增量 BLOB、临时文件库 WAL/FULL；目标部署及完整并发验收须另外执行。')


def check_integer_definitions(db: sqlite3.Connection, write_doc: bool) -> None:
    directory = SCHEMA.parent
    definitions = read_unique_json(directory / 'enum-registry.json')
    verify(set(definitions) == {'format_version', 'description', 'enums', 'json_enums',
        'boolean_columns', 'external_columns', 'fixed_columns', 'history_objects', 'sources'}, '整数定义的根成员完整')
    verify(type(definitions['format_version']) is int and definitions['format_version'] == 1, '整数定义格式为 1')
    verify(isinstance(definitions['description'], str) and bool(definitions['description']), '整数定义有说明')
    verify(set(definitions['sources']) == {'public_errors', 'events'}, '编号来源文件完整')
    errors = read_unique_json(directory / definitions['sources']['public_errors'])['codes']
    events = read_unique_json(directory / definitions['sources']['events'])
    table_sql = dict(db.execute("SELECT name,sql FROM sqlite_schema WHERE type='table'"))
    columns = {f'{table}.{row[1]}': row for table in table_sql for row in db.execute(f'PRAGMA table_xinfo({table})')}
    groups = {}

    def members_valid(name, members):
        verify(isinstance(members, dict) and bool(members), f'成员集合非空：{name}')
        verify(all(re.fullmatch(r'[A-Z][A-Z0-9_]*', member) for member in members), f'成员名合法：{name}')
        values = list(members.values())
        verify(all(type(value) is int and 0 < value <= 2**63 - 1 for value in values), f'编号为正整数：{name}')
        verify(len(values) == len(set(values)), f'组内编号唯一：{name}')

    def contract_valid(rule, name):
        verify(isinstance(rule['contract'], str) and (directory / rule['contract'].split('#')[0]).is_file(), f'字段有正式契约：{name}')

    def add(name, members):
        verify(name in columns and columns[name][2] == 'INTEGER', f'编号定位实际整数列：{name}')
        verify(name not in groups, f'普通列只属于一种编号分类：{name}')
        groups[name] = members

    for section in ('enums', 'json_enums'):
        for name, rule in definitions[section].items():
            verify(set(rule) == {'members', 'contract'}, f'枚举定义成员完整：{name}')
            members_valid(name, rule['members'])
            contract_valid(rule, name)
            parts = name.split('.')
            if section == 'enums':
                verify(len(parts) == 2, f'普通列路径合法：{name}')
                add(name, rule['members'])
            else:
                verify(len(parts) == 3 and re.fullmatch(r'[a-z_][a-z0-9_]*', parts[2]), f'JSON 成员路径合法：{name}')
                column = '.'.join(parts[:2])
                verify(column in columns and columns[column][2] == 'TEXT' and parts[1].endswith('_json'), f'JSON 分类定位实际列：{name}')
    for name in definitions['boolean_columns']:
        add(name, {'FALSE': 0, 'TRUE': 1})
    for name, rule in definitions['fixed_columns'].items():
        verify(set(rule) == {'value', 'contract'}, f'固定值定义完整：{name}')
        members_valid(name, {'FIXED': rule['value']})
        contract_valid(rule, name)
        add(name, {'FIXED': rule['value']})

    # 公共错误按所属列分别核对，组间相同编号合法；本文件不再保存另一份完整编号表。
    error_groups = {'actions.error_code': {}}
    for code, rule in errors.items():
        verify(re.fullmatch(r'[a-z][a-z0-9_]*', code) is not None, f'公共错误码使用规范标识：{code}')
        if 'action_error_id' in rule:
            error_groups['actions.error_code'][code.upper()] = rule['action_error_id']
        for table, value in rule.get('item_error_ids', {}).items():
            error_groups.setdefault(f'{table}.error_code', {})[code.upper()] = value
    for name, members in error_groups.items():
        members_valid(name, members)
    external_errors = set()
    for name, rule in definitions['external_columns'].items():
        source = rule['source']
        if source == 'public_errors':
            verify(set(rule) == {'source', 'key'}, f'错误来源引用完整：{name}')
            expected = 'action_error_id' if name == 'actions.error_code' else f'item_error_ids.{name.split(".")[0]}'
            verify(name.endswith('.error_code') and rule['key'] == expected and name in error_groups, f'错误来源与所属表一致：{name}')
            members = error_groups[name]
            external_errors.add(name)
        elif source in ('events', 'event_versions'):
            verify(set(rule) == {'source'}, f'事件来源引用完整：{name}')
            if source == 'events':
                members = {key: event['id'] for key, event in events['events'].items()}
            else:
                members = {f'VERSION_{event["version"]}': event['version'] for event in events['events'].values()}
        else:
            verify(source == 'history_objects' and set(rule) <= {'source', 'qualification'}, f'对象来源引用合法：{name}')
            qualification = rule.get('qualification')
            verify(qualification in (None, 'snapshot', 'report_target'), f'对象资格合法：{name}')
            members = {key.upper(): obj['id'] for key, obj in definitions['history_objects'].items()
                       if qualification is None or obj[qualification]}
        members_valid(name, members)
        add(name, members)
    verify(external_errors == set(error_groups), '全部公共错误编号均有实际列引用')

    # 只识别本仓库列声明使用的有限值约束。既有组改成其他写法会因缺少对应列而失败，
    # 不能跳过。状态组合约束继续由下方实际业务表样本验证。
    domains = {}
    for table, sql in table_sql.items():
        for match in re.finditer(r'^\s+([a-z_]+) INTEGER\b([^\n]*)$', sql, re.M):
            col, declaration = match.groups()
            constraint = re.search(r'CHECK \((.*)\),?$', declaration)
            if not constraint:
                continue
            expression = constraint[1]
            finite = re.fullmatch(rf'(?:{col} IS NULL OR )?{col} (?:IN \(([\d, ]+)\)|BETWEEN (\d+) AND (\d+)|= (\d+))', expression)
            if finite:
                domains[f'{table}.{col}'] = (col, declaration, expression, finite.groups())
    verify(set(domains) == set(groups),
           f'有限整数列分类完整：缺少 {sorted(set(domains) - set(groups))}；多出 {sorted(set(groups) - set(domains))}')
    for name, members in groups.items():
        col, declaration, expression, (listed, lower, upper, fixed) = domains[name]
        expected = set(members.values())
        if listed is not None:
            actual = [int(value) for value in listed.split(',')]
        elif lower is not None:
            verify(0 <= int(upper) - int(lower) < len(expected), f'有限范围没有额外成员：{name}')
            actual = list(range(int(lower), int(upper) + 1))
        else:
            actual = [int(fixed)]
        verify(len(actual) == len(set(actual)) and set(actual) == expected, f'SQL 编号全集等于权威定义：{name}')
        # 此处只验证声明的列值约束；不模拟 INTEGER PRIMARY KEY 的自动分配。
        # 调用方显式提供主键的责任仍按身份契约验证。
        required = 'NOT NULL' in declaration
        db.execute(f'CREATE TEMP TABLE enum_probe ({col} INTEGER {"NOT NULL" if required else ""} CHECK ({expression})) STRICT')
        # 完整合法集合、每个编号相邻空缺、负数、零、整数边界、非整数和空值。
        candidates = expected | {v + delta for v in expected for delta in (-1, 1)} | {-2**63, -1, 0, 2**63 - 1}
        for value in [*sorted(candidates), None, 1.5, 'invalid']:
            accepted = True
            try:
                db.execute(f'INSERT INTO enum_probe ({col}) VALUES (?)', (value,))
            except sqlite3.IntegrityError:
                accepted = False
            verify(accepted == (value in expected or (value is None and not required)), f'SQL 列值约束：{name}/{value!r}')
            db.execute('DELETE FROM enum_probe')
        db.execute('DROP TABLE enum_probe')

    # 数字条件引用同一字段的定义；业务状态图本身仍由事件检查器验证。
    for name, model in events['state_models'].items():
        verify(name in groups, f'状态模型的字段有编号定义：{name}')
        for edge in model['edges']:
            verify(all(type(edge[side]) is int and edge[side] in groups[name].values() for side in ('from', 'to')), f'状态边使用合法编号：{name}')
    for event in events['events'].values():
        for branch in event['branches'].values():
            for row in branch['rows']:
                for side in (row.get('before', {}), row['after']):
                    for col, values in side.items():
                        name = f'{row["table"]}.{col}'
                        if name in groups:
                            verify(all(value is None or (type(value) is int and value in groups[name].values()) for value in values), f'事件条件使用合法编号：{name}')

    lines = ['# 数据库整数编号一览', '', '[定义与检查规则](common.md#状态与类型的整数枚举) · [数据库目录](../database-schema.md)', '',
             '本页由 `scripts/check-database-spec.py --write-enum-doc` 从权威定义生成，不单独修改编号。成员的业务含义、空值和转换条件以各表的正式章节为准。', '']
    for title, section in [('普通列', 'enums'), ('JSON 中的整数分类', 'json_enums')]:
        lines += [f'## {title}', '']
        for name, rule in definitions[section].items():
            lines += [f'### `{name}`', '', f'[行为说明]({rule["contract"]})', '', '| 编号 | 成员 |', '| --- | --- |']
            lines += [f'| {value} | `{member}` |' for member, value in sorted(rule['members'].items(), key=lambda item: item[1])]
            lines += ['']
    lines += ['## 动作及逐项错误', '', '编号、公共错误码、阶段与详情共用[公共错误定义](../../../protocol/errors/workflow-codes.json)。详情由对应条目的 `details_schema` 定义。', '']
    for name, members in sorted(error_groups.items()):
        lines += [f'### `{name}`', '', '| 编号 | 公共错误码 | 阶段 |', '| --- | --- | --- |']
        lines += [f'| {value} | `{member.lower()}` | `{errors[member.lower()]["stage"]}` |'
                  for member, value in sorted(members.items(), key=lambda item: item[1])]
        lines += ['']
    lines += ['## 其他编号来源', '', '事件类型、事件版本及分支编号见[事件种类与分支](history-formats.md#事件正文版本-1)。历史对象类型及资格见[对象目录与归属](history-formats.md#对象目录与归属)。布尔列和固定列的分类见[整数编号定义](enum-registry.json)；它们不作为业务状态枚举。', '']
    doc = directory / 'enum-values.md'
    content = '\n'.join(lines)
    if write_doc:
        doc.write_text(content)
    else:
        verify(doc.is_file() and doc.read_text() == content, '编号一览须与权威定义一致；使用 --write-enum-doc 更新后复核')
    print(f'整数编号检查通过：{len(definitions["enums"])} 组普通列、{len(definitions["json_enums"])} 组 JSON 分类、{len(error_groups)} 组公共错误；共核对 {len(groups)} 个有限值整数列。')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-enum-doc', action='store_true', help='从整数编号定义更新编号一览；仍执行全部结构检查')
    parser.add_argument('--write-runtime-doc', action='store_true', help='从统一版本条件更新版本表')
    parser.add_argument('--runtime-only', action='store_true', help='只检查运行库版本、文档一致性及临时库基本能力')
    args = parser.parse_args()
    if args.runtime_only and args.write_enum_doc:
        parser.error('--runtime-only 不能与 --write-enum-doc 同时使用')
    rules = read_runtime_requirements()
    check_runtime_doc(rules, args.write_runtime_doc)
    check_runtime(rules)
    if args.runtime_only:
        return
    db = sqlite3.connect(':memory:')
    db.execute('PRAGMA foreign_keys = ON')
    files = sorted(SCHEMA.glob('*.sql'))
    db.executescript('BEGIN IMMEDIATE;\n' + '\n'.join(p.read_text() for p in files) + '\nCOMMIT;')
    tables = {r[1]: r for r in db.execute('PRAGMA table_list') if r[2] == 'table' and not r[1].startswith('sqlite_')}
    # 与人类导航核对，避免另维护一份完整表名单。
    navigation = (ROOT / 'docs/camctl/database-schema.md').read_text()
    documented = set(re.findall(r'^\| (?:`|\[)([a-z_]+)(?:`|\])', navigation, re.M))
    verify(set(tables) == documented, 'SQL 表集合必须与入口目录完全一致')
    for table, row in tables.items():
        verify(row[-1] == 1, f'{table} 使用 STRICT')
        cols = list(db.execute(f'PRAGMA table_xinfo({table})'))
        verify([(r[1], r[2]) for r in cols if r[5]] == [('id', 'INTEGER')], f'{table} 的唯一主键为整数 id')
        for fk in db.execute(f'PRAGMA foreign_key_list({table})'):
            verify(fk[2] in tables, f'{table} 外键目标存在')
            target = {r[1] for r in db.execute(f'PRAGMA table_xinfo({fk[2]})')}
            verify(fk[4] in target, f'{table} 外键列存在')
            verify(fk[6] == 'NO ACTION', f'{table} 不级联删除历史投影')
    verify(list(db.execute('PRAGMA foreign_key_check')) == [], '空结构外键有效')
    verify(db.execute("SELECT count(*) FROM sqlite_schema WHERE type='trigger'").fetchone()[0] == 0, '无隐式业务触发器')
    check_integer_definitions(db, args.write_enum_doc)

    # 以下是跨表结构样本，只验证外键、唯一性和 CHECK；不是可回放的业务历史样本。
    insert(db, 'database_metadata', id=1, application_id='camctl', instance_id='a' * 32, format_version=1,
           staging_path='/srv/camctl/staging', ready_path='/srv/camctl/ready', processing_path='/srv/camctl/processing')
    insert(db, 'runtime_state', id=1, acknowledged_wm=0)
    insert(db, 'history_transactions', id=1, operation_key='b' * 32, first_event_id=1, last_event_id=2)
    for event in (1, 2):
        insert(db, 'history_events', id=event, transaction_id=1, event_type=event, event_version=1,
               occurred_at=0, clock_status=2, change_seq=event, body_json='{}')
    meta = dict(created_event_id=1, last_event_id=2, change_count=2)
    insert(db, 'plans', id=1, request_id=1, name='结构检查', created_at=0, status=2, **meta)
    for action_id, action_type in enumerate((1, 4, 5, 6, 7), 1):
        row = dict(id=action_id, plan_id=1, input_index=action_id - 1, name=f'a{action_id}',
                   type=action_type, input_fields_json='{}', execution_spec_json='{}', status=2,
                   execution_started=1, cancel_requested=0, **meta)
        if action_type == 1:
            row.update(device_id='camera', scheduled_at=0, effective_params_json='{}', driver_id='driver', max_delay_ms=0)
        if action_type in (4, 5):
            row.update(scheduled_at=0, source_resolution_state=2, resolved_source_plan_id=1)
        insert(db, 'actions', **row)
    insert(db, 'auto_preview_links', id=1, obtain_action_id=2, source_action_id=1,
           parameter_type='photo', preview_support=1, is_valid=1)
    insert(db, 'action_dependencies', id=1, action_id=2, depends_on_action_id=1)
    insert(db, 'obtain_source_selections', id=1, dependency_id=1, status=2)
    insert(db, 'device_activities', id=1, action_id=1, task_key='c' * 32, state_query_supported=0,
           stop_supported=0, safe_repeat_stop=0, start_return_meaning=1, completion_mode=2,
           ownership_mode=1, output_scope_json='{}', baseline_state=1, dispatch_state=3,
           activity_state=1, occupancy_state=1, completion_basis=1, result_set_state=1)
    insert(db, 'device_files', id=1, observer_action_id=1, source_action_id=1, identity_key='["camera","driver","f1"]',
           locator_json='{}', ownership_evidence_json='{}', role=2, presence_state=2, completion_state=3,
           completion_evidence_json='{}', size_bytes=100, checksum_support=3, **meta)
    insert(db, 'outputs', id=1, source_action_id=1, kind=1, device_file_id=1, availability=1, cleanup_status=1, media_json='{}', **meta)
    insert(db, 'device_files', id=2, observer_action_id=1, source_action_id=1, identity_key='["camera","driver","f2"]',
           locator_json='{}', ownership_evidence_json='{}', role=2, presence_state=2, completion_state=3,
           completion_evidence_json='{}', size_bytes=50, checksum_support=3, **meta)
    insert(db, 'outputs', id=2, source_action_id=1, kind=3, device_file_id=2, availability=1, cleanup_status=1, media_json='{}', **meta)
    insert(db, 'output_origins', id=1, output_id=2, original_output_id=1)
    insert(db, 'deliveries', id=1, action_id=2, output_id=1, file_name='1.bin', display_name='副本', status=2, withdrawal_state=1, **meta)
    insert(db, 'obtain_items', id=1, selection_id=1, output_id=1, basis=1, status=3,
           source_dependency=1, delivery_id=1)
    insert(db, 'intermediate_files', id=1, owner_delivery_id=1, purpose=1,
           relative_path='deliveries/1.bin', retention_state=1, cleanup_state=1, **meta)
    insert(db, 'operation_runs', id=1, action_id=2, delivery_id=1, kind=3, responsibility_key='read/1', copy_id=1,
           status=2, attempts_used=1, max_attempts_used=3, timeout_s_json='10', retry_interval_s_json='3', retry_wait_required=0)
    insert(db, 'file_copies', id=1, delivery_id=1, source_device_file_id=1, target_file_id=1,
           round=1, recopies_used=0, max_recopies_used=2, source_size=100, committed_bytes=0,
           reset_state=1, slot_device_id='camera', verification_state=1)
    insert(db, 'operation_attempts', id=1, run_id=1, attempt_no=1, copy_round=1, status=1,
           intent_event_id=1, max_attempts_used=3, timeout_s_json='10', retry_interval_s_json='3', effect_state=1)
    insert(db, 'cleanup_items', id=1, action_id=3, requested_output_id=1, output_id=1, status=2, restriction_state=2)
    insert(db, 'cancel_items', id=1, action_id=4, target_action_id=2, selection_basis=1, status=2, cancellation_effect=2)
    insert(db, 'cancel_delivery_items', id=1, cancel_item_id=1, delivery_id=1, status=1)
    insert(db, 'recording_processing', id=1, action_id=1, source_device_file_id=1, check_state=1,
           check_decision=1, media_json='{}', repair_state=1, discard_state=1)
    insert(db, 'plan_file_diagnostics', id=1, input_path='input.json', errors_json='[{}]', created_event_id=1)
    insert(db, 'reports', id=1, frozen_event_id=0, from_wm=0, to_wm=0, format_version=1,
           status=1, publication_count=0, created_event_id=1, last_event_id=1)
    insert(db, 'state_syncs', id=1, action_id=5, mode=1, from_wm=0, started_boundary_event_id=2, status=1)
    insert(db, 'entity_event_links', id=1, entity_type=1, entity_id=1, event_id=1, change_count=1)
    insert(db, 'report_entity_changes', id=1, entity_type=1, entity_id=1, event_id=1, change_seq=1)
    insert(db, 'entity_snapshots', id=1, entity_type=1, entity_id=1, boundary_event_id=2, change_count=64, format_version=1, content=b'{}\n')
    insert(db, 'entity_snapshot_progress', id=1, entity_type=1, entity_id=1, current_change_count=70,
           snapshot_change_count=64, latest_snapshot_id=1)
    db.commit()
    verify(list(db.execute('PRAGMA foreign_key_check')) == [], '全部表样本及共同建档引用在提交时有效')
    verify(all(db.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in tables), '全部表都有结构样本')

    # 查询责任的用途、固定目标与规范键共同决定身份；这些样本只验证 SQL 约束。
    verify('query_purpose' in {r[1] for r in db.execute('PRAGMA table_info(operation_runs)')},
           '操作流程必须保存查询用途，执行前检查不能强行引用活动')
    purposes = json.loads((ROOT / 'docs/camctl/database/enum-registry.json').read_text())['enums']['operation_runs.query_purpose']['members']
    with case(db):
        for ident in (6, 7):
            insert(db, 'actions', id=ident, plan_id=1, input_index=ident - 1, name=f'query-{ident}', type=1,
                   device_id='camera', scheduled_at=0, effective_params_json='{}', driver_id='driver', max_delay_ms=0,
                   input_fields_json='{}', execution_spec_json='{}', status=2, execution_started=1,
                   cancel_requested=0, **meta)
        examples = [
            ('BEFORE_EXECUTION', 6, None, 'query/preflight/6'),
            ('START_CONFIRMATION', 1, 1, 'query/start/1/1'),
            ('ACTIVITY_OBSERVATION', 1, 1, 'query/activity/1/1'),
            ('STOP_CONFIRMATION', 1, 1, 'query/stop/1/1'),
            ('RESIDUAL_STOP_CONFIRMATION', 6, 1, 'query/residual/6/1'),
        ]
        for ident, (purpose, action, activity, key) in enumerate(examples, 10):
            row = dict(id=ident, action_id=action, kind=6, query_purpose=purposes[purpose],
                       responsibility_key=key, activity_id=activity, status=1, attempts_used=0,
                       max_attempts_used=3, timeout_s_json='10', retry_interval_s_json='3', retry_wait_required=0)
            insert(db, 'operation_runs', **row)
            verify(True, f'{purpose} 可以保存规定目标及配置')
            for column, value in [
                ('query_purpose', None), ('query_purpose', 0), ('query_purpose', max(purposes.values()) + 1),
                ('activity_id', 1 if activity is None else None), ('copy_id', 1), ('cleanup_item_id', 1),
                ('session_key', 'e' * 32), ('delivery_id', 1), ('responsibility_key', 'query/1'),
                ('responsibility_key', key + '/1'), ('action_id', 7),
                ('timeout_s_json', None), ('retry_interval_s_json', None),
            ]:
                rejects(db, f'UPDATE operation_runs SET {column}=? WHERE id=?', (value, ident))
            columns = list(row)
            duplicate = {**row, 'id': ident + 100}
            rejects(db, f'INSERT INTO operation_runs ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
                    tuple(duplicate.values()))
        # 不同触发者各自检查同一设备或历史活动，不共用查询额度。
        for ident, purpose, activity, key in [
            (20, purposes['BEFORE_EXECUTION'], None, 'query/preflight/7'),
            (21, purposes['RESIDUAL_STOP_CONFIRMATION'], 1, 'query/residual/7/1'),
        ]:
            insert(db, 'operation_runs', id=ident, action_id=7, kind=6, query_purpose=purpose,
                   responsibility_key=key, activity_id=activity, status=1, attempts_used=0,
                   max_attempts_used=3, timeout_s_json='0.5', retry_interval_s_json='0', retry_wait_required=0)
        verify(True, '不同触发动作的执行前检查和残留核实分别保存')
        db.execute('UPDATE operation_runs SET attempts_used=3,max_attempts_used=1 WHERE id=10')
        verify(db.execute('SELECT attempts_used,max_attempts_used FROM operation_runs WHERE id=10').fetchone() == (3, 1),
               '降低查询上限保留已有次数')
        db.execute('UPDATE operation_runs SET status=3 WHERE id=10')
        rejects(db, "INSERT INTO operation_runs (action_id,kind,query_purpose,responsibility_key,status,attempts_used,max_attempts_used,timeout_s_json,retry_interval_s_json,retry_wait_required) VALUES (6,6,?,'query/preflight/6',1,0,9,'10','3',0)",
                (purposes['BEFORE_EXECUTION'],))
        insert(db, 'operation_runs', id=30, action_id=1, kind=7, responsibility_key='results/1', activity_id=1,
               status=1, attempts_used=0, max_attempts_used=3, timeout_s_json='10', retry_interval_s_json='3', retry_wait_required=0)
        for sql in [
            "UPDATE operation_runs SET responsibility_key='results/01' WHERE id=30",
            'UPDATE operation_runs SET query_purpose=1 WHERE id=30',
            'UPDATE operation_runs SET activity_id=NULL WHERE id=30',
            'UPDATE operation_runs SET timeout_s_json=NULL WHERE id=30',
            'UPDATE operation_runs SET retry_interval_s_json=NULL WHERE id=30',
            "INSERT INTO operation_runs (action_id,kind,responsibility_key,activity_id,status,attempts_used,max_attempts_used,timeout_s_json,retry_interval_s_json,retry_wait_required) VALUES (1,7,'results/1',1,1,0,3,'10','3',0)",
            'UPDATE operation_runs SET query_purpose=1 WHERE id=1',
        ]:
            rejects(db, sql)
        verify(list(db.execute('PRAGMA foreign_key_check')) == [], '查询与结果核实样本具有实际目标引用')

    # 应急补记的结果、次数与空值组合；证据真实性及父流程种类仍由业务事务校验。
    emergency = dict(id=90, action_id=1, kind=9, responsibility_key='emergency/' + 'e' * 32 + '/1',
                     activity_id=1, session_key='e' * 32, status=3, attempts_used=1, max_attempts_used=3,
                     timeout_s_json='0.5', retry_interval_s_json='0', retry_wait_required=0)
    # 零次可以是已确认停止或未能尝试；有次数且限额合法时可以成功或停止未确认。
    for used, limit, allowed_statuses in [
        (0, None, (3, 4)), (0, 3, (3, 4)), (1, None, ()),
        (1, 3, (3, 6)), (3, 3, (3, 6)), (4, 3, ()),
    ]:
        for status in range(1, 8):
            row = {**emergency, 'status': status, 'attempts_used': used, 'max_attempts_used': limit,
                   'error_json': '{}' if status in (4, 6) else None}
            if used == 0:
                row.update(timeout_s_json=None, retry_interval_s_json=None)
            with case(db):
                if status in allowed_statuses:
                    insert(db, 'operation_runs', **row)
                    verify(True, f'应急最终状态 {status} 允许次数 {used} 和上限 {limit}')
                else:
                    columns = list(row)
                    rejects(db, f'INSERT INTO operation_runs ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
                            tuple(row.values()))
    with case(db):
        insert(db, 'operation_runs', **emergency)
        for column, value in [
            ('max_attempts_used', None), ('max_attempts_used', 0), ('max_attempts_used', -1),
            ('timeout_s_json', None), ('retry_interval_s_json', None), ('retry_wait_required', 1),
            ('error_json', '{}'), ('session_key', None), ('session_key', 'e' * 31),
            ('session_key', 'E' * 32), ('session_key', 'f' * 32), ('activity_id', None),
            ('responsibility_key', 'emergency/' + 'e' * 32 + '/01'),
            ('responsibility_key', 'emergency/' + 'e' * 32 + '/1/extra'),
            ('copy_id', 1), ('delivery_id', 1), ('cleanup_item_id', 1), ('query_purpose', 1),
        ]:
            rejects(db, f'UPDATE operation_runs SET {column}=? WHERE id=90', (value,))
        duplicate = {**emergency, 'id': 91}
        columns = list(duplicate)
        rejects(db, f'INSERT INTO operation_runs ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
                tuple(duplicate.values()))
        insert(db, 'operation_runs', **{**emergency, 'id': 92, 'session_key': 'f' * 32,
               'responsibility_key': 'emergency/' + 'f' * 32 + '/1'})
        verify(True, '后续会话具有独立应急责任，同一会话的已结束责任仍不可重复')
        for status in (2, 3, 4):
            with case(db):
                insert(db, 'operation_attempts', id=90, run_id=90, attempt_no=1, status=status,
                       intent_event_id=None, result_event_id=2, max_attempts_used=3,
                       timeout_s_json='0.5', retry_interval_s_json='0', effect_state=1,
                       result_json='{}', error_json=None if status == 2 else '{}')
                verify(True, '已结束的应急尝试可无意图引用，并保留实际结果')
                for column, value in [
                    ('status', 1), ('copy_round', 1), ('result_event_id', None),
                    ('max_attempts_used', None), ('timeout_s_json', None),
                    ('retry_interval_s_json', None), ('result_json', None),
                    ('error_json', '{}' if status == 2 else None),
                ]:
                    rejects(db, f'UPDATE operation_attempts SET {column}=? WHERE id=90', (value,))
        rejects(db, 'UPDATE operation_runs SET max_attempts_used=NULL WHERE id=1')
        rejects(db, 'UPDATE operation_attempts SET intent_event_id=NULL WHERE id=1')
        verify(list(db.execute('PRAGMA foreign_key_check')) == [], '应急结构样本的关联引用有效')

    for sql in [
        'UPDATE runtime_state SET id=2',
        'UPDATE database_metadata SET format_version=2',
        'UPDATE actions SET id=0 WHERE id=1',
        'UPDATE actions SET type=99 WHERE id=1',
        "UPDATE actions SET input_fields_json='[]' WHERE id=1",
        'UPDATE actions SET status=4 WHERE id=1',
        'UPDATE actions SET execution_spec_json=NULL WHERE id=1',
        'UPDATE actions SET source_resolution_state=NULL WHERE id=2',
        'UPDATE actions SET source_resolution_state=2,resolved_source_plan_id=NULL WHERE id=2',
        'UPDATE auto_preview_links SET preview_support=NULL WHERE id=1',
        'UPDATE obtain_source_selections SET status=1,error_code=1,error_details_json=\'{}\' WHERE id=1',
        'UPDATE obtain_items SET status=2,delivery_id=NULL WHERE id=1',
        'UPDATE obtain_items SET delivery_id=NULL WHERE id=1',
        'UPDATE cleanup_items SET output_id=NULL WHERE id=1',
        'UPDATE cancel_items SET target_action_id=action_id WHERE id=1',
        'UPDATE cancel_items SET status=3 WHERE id=1',
        'UPDATE cancel_delivery_items SET status=4 WHERE id=1',
        'UPDATE device_files SET size_bytes=NULL WHERE id=1',
        'UPDATE intermediate_files SET retention_state=2 WHERE id=1',
        'UPDATE outputs SET intermediate_file_id=1 WHERE id=1',
        'UPDATE deliveries SET status=5 WHERE id=1',
        'UPDATE file_copies SET committed_bytes=101 WHERE id=1',
        'UPDATE file_copies SET round=2 WHERE id=1',
        'UPDATE file_copies SET verification_state=3 WHERE id=1',
        'UPDATE operation_runs SET status=7 WHERE id=1',
        'UPDATE operation_attempts SET status=2 WHERE id=1',
        'UPDATE device_activities SET occupancy_state=2 WHERE id=1',
        'UPDATE recording_processing SET repair_state=5 WHERE id=1',
        'UPDATE plan_file_diagnostics SET errors_json=\'[]\' WHERE id=1',
        'UPDATE reports SET status=4 WHERE id=1',
        'UPDATE state_syncs SET status=2 WHERE id=1',
        'UPDATE runtime_state SET trusted_time_lower_bound=1',
        'UPDATE entity_snapshot_progress SET snapshot_change_count=71 WHERE id=1',
        'UPDATE entity_snapshot_progress SET latest_snapshot_id=NULL WHERE id=1',
        "UPDATE entity_snapshots SET content=X'' WHERE id=1",
        'UPDATE history_transactions SET last_event_id=0 WHERE id=1',
    ]:
        rejects(db, sql)
    # NULL、缺少记录、有效空集合是不同情况。
    with case(db):
        db.execute('UPDATE actions SET source_resolution_state=2,resolved_source_plan_id=1 WHERE id=3')
        verify(db.execute('SELECT count(*) FROM action_dependencies WHERE action_id=3').fetchone()[0] == 0, '允许已固定空来源')
        db.execute('UPDATE auto_preview_links SET is_valid=0,preview_support=NULL')
        insert(db, 'auto_preview_links', id=2, obtain_action_id=3, source_action_id=1, is_valid=0)
        verify(db.execute('SELECT count(*) FROM auto_preview_links WHERE source_action_id=1').fetchone()[0] == 2, '允许保留多项失败关联')
    rejects(db, 'INSERT INTO auto_preview_links VALUES (2,3,1,1,\'photo\',1)')
    with case(db):
        insert(db, 'obtain_items', id=2, selection_id=1, requested_output_id=9999, basis=5,
               status=4, source_dependency=0, error_code=1, error_details_json='{}')
        verify(list(db.execute('PRAGMA foreign_key_check')) == [], '不存在的原请求 ID 不构成虚假外键')
        insert(db, 'obtain_items', id=3, selection_id=1, basis=3, original_output_id=1,
               status=4, source_dependency=0, error_code=8, error_details_json='{}')
        insert(db, 'obtain_items', id=4, selection_id=1, basis=3, original_output_id=2,
               status=4, source_dependency=0, error_code=8, error_details_json='{}')
        verify(db.execute('SELECT count(*) FROM obtain_items WHERE error_code=8').fetchone()[0] == 2, '不同原文件可以分别缺少预览')
    # 每个发送/活动组合分别列出允许保留占用、允许释放的采集判定。
    # 这里只检查行内组合；动作类型、真实观察与文件限制由写入事务检查。
    occupancy_cases = {
        (1, 1): ((None, 1, 4), (None, 1, 4)),
        (4, 1): ((None, 1, 4), (None, 1, 4)),
        (2, 1): ((None, 1, 4), ()),
        (2, 2): ((None, 1, 4), ()),
        (2, 3): ((None, 1, 2, 4), (None, 1, 2, 4)),
        (3, 1): ((None, 1, 3, 4), (3,)),
        (3, 2): ((None, 1, 4), ()),
        (3, 3): ((None, 1, 2, 3, 4), (None, 1, 2, 3, 4)),
    }
    timing = dict(sent_at=0, result_wait_margin_ms=0, extra_wait_ms_used=0,
                  expected_check_at=1, wait_completed_event_id=2,
                  result_set_state=3, result_check_json='{}')
    for dispatch in (1, 2, 3, 4):
        for activity in (1, 2, 3):
            for basis in (None, 1, 2, 3, 4):
                for occupancy in (1, 2):
                    values = dict(dispatch_state=dispatch, activity_state=activity, completion_basis=basis,
                                  completion_evidence_json='{}' if basis in (2, 3, 4) else None,
                                  occupancy_state=occupancy)
                    if basis == 3:
                        values.update(timing)
                    sql = 'UPDATE device_activities SET ' + ','.join(f'{c}=?' for c in values) + ' WHERE id=1'
                    allowed = occupancy_cases.get((dispatch, activity), ((), ()))[occupancy - 1]
                    with case(db):
                        if basis in allowed:
                            db.execute(sql, tuple(values.values()))
                            verify(True, f'允许活动组合 {dispatch, activity, basis, occupancy}')
                        else:
                            rejects(db, sql, tuple(values.values()))
    with case(db):
        values = dict(completion_basis=3, completion_evidence_json='{}', occupancy_state=2, **timing)
        db.execute('UPDATE device_activities SET ' + ','.join(f'{c}=?' for c in values) + ' WHERE id=1', tuple(values.values()))
        verify(db.execute('SELECT activity_state FROM device_activities WHERE id=1').fetchone()[0] == 1,
               '时间与产物完成不补造设备结束观察')
        for column, value in [
            ('completion_mode', 1), ('dispatch_state', 2), ('activity_state', 2),
            ('sent_at', None), ('expected_check_at', None), ('wait_completed_event_id', None),
            ('result_wait_margin_ms', None), ('extra_wait_ms_used', None),
            ('result_set_state', 1), ('result_set_state', 2), ('result_set_state', 4),
            ('result_check_json', None), ('completion_evidence_json', None),
        ]:
            rejects(db, f'UPDATE device_activities SET {column}=? WHERE id=1', (value,))
    for basis, evidence in [(None, '{}'), (1, '{}'), (2, None), (3, None), (4, None)]:
        rejects(db, 'UPDATE device_activities SET activity_state=3,completion_basis=?,completion_evidence_json=? WHERE id=1',
                (basis, evidence))
    rejects(db, "UPDATE device_activities SET completion_basis=NULL,capture_json='{}' WHERE id=1")
    with case(db):
        db.execute('UPDATE actions SET type=2 WHERE id=1')
        db.execute('UPDATE device_activities SET completion_basis=NULL,activity_state=3 WHERE id=1')
        verify(db.execute('SELECT occupancy_state FROM device_activities WHERE id=1').fetchone()[0] == 1,
               '录像已结束仍可因文件归属限制保持占用')
        db.execute('UPDATE device_activities SET occupancy_state=2 WHERE id=1')
        verify(db.execute('SELECT completion_basis FROM device_activities WHERE id=1').fetchone()[0] is None,
               '录像释放占用不需要虚构采集判定')
    with case(db):
        db.execute('UPDATE operation_runs SET max_attempts_used=1,attempts_used=2,timeout_s_json=NULL,retry_interval_s_json=NULL')
        verify(True, '降低上限不否定累计次数；本地操作不强加超时')
    with case(db):
        db.execute('UPDATE history_transactions SET last_event_id=9999')
        verify(bool(list(db.execute('PRAGMA foreign_key_check'))), '延迟外键可以在事务内发现悬空引用')
    db.execute('BEGIN') if not db.in_transaction else None
    db.execute('UPDATE entity_snapshots SET boundary_event_id=1')
    try:
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        verify(True, '快照不得引用事务内部事件作为完整边界')
    else:
        raise AssertionError('快照接受了非完整边界')
    verify(db.execute('SELECT pending_changes FROM entity_snapshot_progress').fetchone()[0] == 6, '70 次变化的对象保存 64 次快照后仍待处理 6 次')
    with case(db):
        for ident, kind in ((2, 4), (3, 2), (4, 3)):
            insert(db, 'entity_snapshot_progress', id=ident, entity_type=kind, entity_id=1,
                   current_change_count=64, snapshot_change_count=0)
        result = db.execute('SELECT entity_type FROM entity_snapshot_progress WHERE pending_changes>=64 ORDER BY pending_changes DESC,entity_type_name COLLATE BINARY,entity_id').fetchall()
        verify(result == [(2,), (3,), (4,)], '快照阈值及并列排序')
    with case(db):
        db.execute('UPDATE plans SET request_id=?', (2**63 - 1,))
        verify(db.execute('SELECT request_id FROM plans').fetchone()[0] == 2**63 - 1, '64 位整数上限无损保存')
        try:
            db.execute('UPDATE plans SET request_id=?', (2**63,))
        except OverflowError:
            verify(True, '超范围 Python 整数不得隐式截断')
        else:
            raise AssertionError('超范围整数未拒绝')
    registry = json.loads((ROOT / 'protocol/errors/workflow-codes.json').read_text())['codes']
    ids = [rule['action_error_id'] for rule in registry.values() if 'action_error_id' in rule]
    verify(len(ids) == len(set(ids)) and all(type(i) is int and i > 0 for i in ids), '动作错误编号为唯一正整数')
    for rule in registry.values():
        if 'action_error_id' not in rule:
            continue
        with case(db):
            admission = rule['stage'] == 'admission'
            db.execute('UPDATE actions SET status=4,error_code=?,error_details_json=\'{}\',execution_started=?,execution_spec_json=?,effective_params_json=?,driver_id=? WHERE id=1',
                       (rule['action_error_id'], 0 if admission else 1, None if admission else '{}', None if admission else '{}', None if admission else 'driver'))
            verify(True, '登记错误码可保存')
    core_sql = db.execute("SELECT sql FROM sqlite_schema WHERE name='actions'").fetchone()[0]
    allowed = re.search(r'error_code IS NULL OR error_code IN \(([^)]+)\)', core_sql)
    verify(allowed is not None and {int(n) for n in allowed[1].split(',')} == set(ids), 'SQL 动作错误全集与公共登记一致')
    queries = [
        ('SELECT id FROM actions WHERE status IN (1,2) ORDER BY scheduled_at,id LIMIT 10', 'actions_schedule'),
        ('SELECT id FROM outputs WHERE source_action_id=1 AND id>0 ORDER BY id LIMIT 10', 'outputs_source'),
        ('SELECT id FROM obtain_items WHERE output_id=1 AND source_dependency=1', 'output_readers'),
        ('SELECT id FROM cleanup_items WHERE output_id=1 AND restriction_state IN (2,4)', 'output_delete_restrictions'),
        ('SELECT id FROM intermediate_files WHERE retention_state=2 AND cleanup_state<>4 AND id>0 ORDER BY id LIMIT 10', 'intermediate_cleanup'),
        ('SELECT id FROM entity_snapshot_progress WHERE pending_changes>=64 ORDER BY pending_changes DESC,entity_type_name COLLATE BINARY,entity_id LIMIT 10', 'snapshot_candidates'),
        ('SELECT change_seq,entity_id FROM report_entity_changes WHERE entity_type=1 AND change_seq>0 AND change_seq<=2 ORDER BY change_seq,entity_id LIMIT 4097', 'report_changes_by_sequence'),
        ('SELECT DISTINCT entity_id FROM report_entity_changes WHERE entity_type=1 AND entity_id>0 AND +change_seq>0 AND +change_seq<=2 ORDER BY entity_id LIMIT 10', 'report_changes_by_entity'),
    ]
    for query, index in queries:
        plan = ' '.join(str(r[3]) for r in db.execute('EXPLAIN QUERY PLAN ' + query))
        verify(index in plan, f'查询应使用 {index}，实际：{plan}')
    verify(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', '结构与样本完整性检查')
    db.close()
    print(f'数据库结构规格检查通过：{len(files)} 份 SQL，{len(tables)} 张表，{checks} 项断言（SQLite {sqlite3.sqlite_version}）。')
    print('边界：未实现事件应用、业务事务、历史回放或设备流程；结构样本不作为端到端业务历史。')


if __name__ == '__main__':
    main()
