#!/workspaces/camctl/apps/camctl/.venv311/bin/python
"""启动已审查的补跑清单；创建本次独立预算与隔离证据。"""
import argparse
import json
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--jobs', type=int, choices=(3, 4), default=3)
args = parser.parse_args()
record = json.loads(Path('/tmp/camctl-full-272f-replay-command.json').read_text())
selection = json.loads(Path(record['selection']).read_text())
source = Path(record['source'])
for name in ('output', 'budget_evidence'):
    if Path(record[name]).exists():
        parser.error(f"{name} 已存在，保留原证据：{record[name]}")
subprocess.run(['git', 'merge-base', '--is-ancestor', record['rootdir_fix_commit_required'], 'HEAD'], cwd=source, check=True)
actual_tree = subprocess.check_output(['git', 'rev-parse', 'HEAD:apps/camctl/src'], cwd=source, text=True).strip()
if actual_tree != selection['production_source_tree']:
    parser.error('当前生产源码树与已审查补跑基线不同')
subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--', 'apps/camctl/src'], cwd=source, check=True)
command = list(record['command'])
command[command.index('--jobs') + 1] = str(args.jobs)
raise SystemExit(subprocess.run(command, cwd=source).returncode)
