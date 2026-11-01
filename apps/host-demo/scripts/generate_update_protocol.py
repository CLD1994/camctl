"""从根协议生成 C 更新结果枚举和合法结果组合。"""
import argparse
import itertools
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = ROOT / 'protocol/schemas/software-update-result.schema.json'
HEADER = ROOT / 'apps/host-demo/include/camctl_update_protocol.h'
TABLE = ROOT / 'apps/host-demo/src/update_protocol_generated.h'
FIELDS = ('action', 'outcome', 'effect', 'eligibility', 'prior_recovery')


def _name(field, value):
    return 'CAMCTL_UPDATE_' + field.upper() + '_' + value.upper()


def _valid(schema, values):
    for rule in schema['allOf']:
        conditions = rule['if']['properties']
        if all(values[name] == condition['const'] for name, condition in conditions.items()):
            for name, constraint in rule['then']['properties'].items():
                if 'const' in constraint and values[name] != constraint['const']:
                    return False
                if 'enum' in constraint and values[name] not in constraint['enum']:
                    return False
    return True


def render(schema):
    enums = {field: schema['properties'][field]['enum'] for field in FIELDS}
    header = ['#ifndef CAMCTL_UPDATE_PROTOCOL_H', '#define CAMCTL_UPDATE_PROTOCOL_H', '', '#include <stdbool.h>', '']
    for field, values in enums.items():
        header += ['typedef enum {', '    CAMCTL_UPDATE_' + field.upper() + '_INVALID = 0,']
        header += ['    ' + _name(field, value) + ',' for value in values]
        header += ['} camctl_update_' + field + ';', '']
    header += ['#ifdef __cplusplus', 'extern "C" {', '#endif',
               'bool camctl_update_outcome_succeeded(camctl_update_outcome value);',
               'const char *camctl_update_effect_description(camctl_update_effect value);',
               'const char *camctl_update_eligibility_description(camctl_update_eligibility value);',
               '#ifdef __cplusplus', '}', '#endif', '', '#endif', '']
    table = ['/* 由 generate_update_protocol.py 从根协议生成。 */',
             f'#define UPDATE_PROTOCOL_VERSION {schema["properties"]["version"]["const"]}',
             f'#define UPDATE_PROTOCOL_FIELDS {len(schema["required"])}', '']
    for field, values in enums.items():
        table += [f'static const struct update_name update_{field}_names[] = {{']
        table += ['    {' + _name(field, value) + ', "' + value + '", ' +
                  json.dumps(schema.get('x-labels', {}).get(field, {}).get(value, value), ensure_ascii=False) + '},'
                  for value in values]
        table += ['};', '']
    table += ['static const struct update_combination update_combinations[] = {']
    for combination in itertools.product(*(enums[field] for field in FIELDS)):
        values = dict(zip(FIELDS, combination))
        if _valid(schema, values):
            table.append('    {' + ', '.join(_name(field, values[field]) for field in FIELDS) + '},')
    table += ['};', '']
    success = schema['x-exit-status']
    table += [f'#define UPDATE_SUCCESS_EXIT {success["success"]}',
              f'#define UPDATE_FAILURE_EXIT {success["failure"]}',
              'static const int update_success_outcomes[] = {']
    table += ['    ' + _name('outcome', value) + ',' for value in success['success_outcomes']]
    table += ['};', '']
    return '\n'.join(header), '\n'.join(table)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    for path, content in zip((HEADER, TABLE), render(json.loads(SCHEMA.read_text(encoding='utf-8')))):
        if args.check:
            if not path.is_file() or path.read_text(encoding='utf-8') != content:
                raise SystemExit('生成的更新协议已过期: ' + str(path))
        else:
            path.write_text(content, encoding='utf-8')


if __name__ == '__main__':
    main()
