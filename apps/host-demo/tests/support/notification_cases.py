"""在构建时把公共夹具转成静态C值，单元测试不访问文件系统。"""
import json
import pathlib
import sys

cases = json.loads(pathlib.Path(sys.argv[1]).read_text())
lines = ['typedef struct {const char *json; int valid, position;} shared_case;',
         'static const shared_case shared_cases[]={']
for case in cases:
    if case['kind'] != 'notification':
        continue
    raw = (case['json'] + '\n').encode('utf-8', errors='surrogatepass')
    literal = ''.join(f'\\{byte:03o}' for byte in raw)
    lines.append(f'{{"{literal}",{int(case["valid"])},{case.get("position", "0")}}},')
lines.append('};')
pathlib.Path(sys.argv[2]).write_text('\n'.join(lines) + '\n')
