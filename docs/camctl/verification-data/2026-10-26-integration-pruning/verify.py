"""脱离原临时目录核对归档完整性、节点集合差和组合结果。"""

import gzip
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def read_json(path):
    path = ROOT / path
    data = path.read_bytes()
    return json.loads(gzip.decompress(data) if path.suffix == ".gz" else data)


def collected(phase):
    summary = read_json(f"collection/{phase}/summary.json")
    result = set()
    for group in summary["groups"]:
        label = "root" if group["target"] == "tests/integration" else Path(group["target"]).name
        data = gzip.decompress((ROOT / f"collection/{phase}/{label}/nodes.json.gz").read_bytes())
        assert hashlib.sha256(data).hexdigest() == group["nodes_sha256"], label
        nodes = json.loads(data)["nodeids"]
        assert len(nodes) == group["node_count"], label
        assert not result.intersection(nodes), label
        result.update(nodes)
    return result


for name, entry in read_json("manifest.json").items():
    data = (ROOT / name).read_bytes()
    assert len(data) == entry["bytes"], name
    assert hashlib.sha256(data).hexdigest() == entry["sha256"], name
    if name.endswith(".gz"):
        gzip.decompress(data)

audit = read_json("audit.json")
change = read_json("change-audit.json")
assert audit["summary"] == change
files = [item for group in audit["groups"] for item in group["files"]]
assert len(files) == len({item["path"] for item in files}) == change["reviewed_files"]
before, after = collected("before"), collected("after")
assert len(before) == change["before_collected_nodes"]
assert len(after) == change["after_collected_nodes"]
assert sorted(before - after) == change["removed_collected_nodes"]
assert sorted(after - before) == change["added_collected_nodes"] == []

combination = read_json("combination/summary.json")
unit = combination["groups"][0]
assert unit["name"] == "unit-cover" and unit["counts"]["passed"] == 138
integration = combination["groups"][1:]
assert sum(item["counts"]["passed"] for item in integration) == 151
assert sum(item["counts"]["failed"] for item in integration) == 7
assert combination["exit_code"] == 1
print(f"归档校验通过：{len(files)} 个测试文件已审查，收集节点 {len(before)} → {len(after)}。")
