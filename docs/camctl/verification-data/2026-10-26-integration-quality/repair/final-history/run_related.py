"""只选择直接消费本轮历史异步准备的节点，并沿仓库隔离运行器执行。"""
import ast
import json
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
evidence = Path(__file__).resolve().parent
history = Path("apps/camctl/tests/integration/history")
targets = [str(history / "test_report_scope.py") + "::" + name for name in (
    "TestSelectReportScope", "TestRestoreEntity")]
for filename in ("test_report_selection.py", "test_restore_paths.py"):
    relative = history / filename
    for node in ast.parse((repo / relative).read_text()).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test_"):
            continue
        if filename == "test_report_selection.py":
            selected = node.name != "test_large_window_releases_overflow_once_and_keyset_pages_do_not_rescan_window"
        else:
            selected = bool({argument.arg for argument in node.args.args} & {"file_world", "two_item_world"})
        if selected:
            targets.append(str(relative) + "::" + node.name)
command = [sys.executable, str(repo / "scripts/run-integration.py"), "--scope", "camctl",
           "--jobs", "1", "--python", sys.executable, "--camctl-memory-limit", "2GiB",
           "--output", str(evidence / "related-run")]
for target in targets:
    command.extend(("--target", target))
(evidence / "related-targets.json").write_text(json.dumps({"targets": targets, "command": command}, indent=2) + "\n")
with (evidence / "related-run.log").open("w") as output:
    result = subprocess.run(command, cwd=repo, stdout=output, stderr=subprocess.STDOUT)
sys.exit(result.returncode)
