"""冻结已结束首轮及已完成诊断；只向本暂存目录写入。"""
import collections
import datetime
import gzip
import hashlib
import json
import os
import pathlib
import sys

from verify import verify

STAGE = pathlib.Path(__file__).resolve().parent
MAIN = pathlib.Path("/tmp/camctl-integration-all-final-20261026-u19lkvoe")
DIAGNOSIS = pathlib.Path("/tmp/camctl-full-272f-diagnosis")
MERGE = pathlib.Path("/tmp/camctl-full-272f-archive-stage")
TEXT_LOG_SUFFIXES = {".log", ".jsonl", ".xml", ".txt", ".trace"}
DIAGNOSIS_SUFFIXES = TEXT_LOG_SUFFIXES | {".json", ".md", ".tsv", ".py", ".patch"}
EXCLUDED_DIRS = {"source", "snapshot", "tmp", "pytest-tmp", "__pycache__", ".pytest_cache",
                 ".venv", ".venv311", "node_modules", "cache", "dist", "wheel", "wheels",
                 "uv-cache", "staging", "processing", "ready", "worlds"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build():
    main_summary = json.loads((MAIN / "summary.json").read_text())
    assert main_summary["status"] == "finished"
    budget = pathlib.Path(str(MAIN) + "-budget")
    assert json.loads((budget / "launcher.json").read_text())["status"] == "finished"
    selected = []

    def select(source, relative, role, preserve_raw=False):
        assert source.is_file() and not source.is_symlink(), source
        selected.append((source, pathlib.Path(relative), role, preserve_raw))

    for name in ("source.json", "plan.json", "summary.json"):
        select(MAIN / name, "main/" + name, "main-metadata", True)
    artifact_groups = {}
    for section in ("collection", "tasks"):
        count = 0
        for group in sorted((MAIN / section).iterdir()):
            artifact = group / "artifacts"
            if not artifact.is_dir():
                continue
            count += 1
            if section == "tasks":
                assert (artifact / "result.json").is_file(), group
            for path in sorted(artifact.iterdir()):
                if path.is_file():
                    select(path, pathlib.Path("main") / section / group.name / "artifacts" / path.name,
                           "main-" + section + "-artifact")
        artifact_groups[section] = count
    for path in sorted((MAIN / "control").glob("*.py")):
        select(path, pathlib.Path("main/control") / path.name, "main-instrumentation", True)
    for root, dirs, files in os.walk(budget):
        dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRS]
        for name in sorted(files):
            path = pathlib.Path(root) / name
            select(path, pathlib.Path("budget/main") / path.relative_to(budget), "main-budget-original", True)
    completed_diagnosis = {"capture", "root-demos", "bootstrap", "outputs", "history", "session-observer-control"}
    for group in sorted(completed_diagnosis):
        group_root = DIAGNOSIS / group
        assert group_root.is_dir(), group
        for root, dirs, files in os.walk(group_root):
            dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRS]
            for name in sorted(files):
                path = pathlib.Path(root) / name
                if path.suffix in DIAGNOSIS_SUFFIXES:
                    select(path, pathlib.Path("diagnosis") / path.relative_to(DIAGNOSIS), "completed-diagnosis")
    for name in ("merge-272f-evidence.tar.gz", "manifest.json", "merge-summary.json", "bundle-receipt.json"):
        select(MERGE / name, pathlib.Path("merge") / name, "existing-merge-bundle", True)

    entries = []
    seen = set()
    for source, relative, role, preserve_raw in selected:
        before = source.stat()
        original = source.read_bytes()
        after = source.stat()
        assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns), source
        encode = not preserve_raw and source.suffix in TEXT_LOG_SUFFIXES
        data = gzip.compress(original, compresslevel=9, mtime=0) if encode else original
        if encode:
            relative = pathlib.Path(str(relative) + ".gz")
        assert str(relative) not in seen
        seen.add(str(relative))
        target = STAGE / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        assert source.read_bytes() == original, source
        entries.append({"path": str(relative), "source_path": str(source), "role": role,
                        "encoding": "gzip" if encode else "identity",
                        "original_bytes": len(original), "original_sha256": digest(original),
                        "archived_bytes": len(data), "archived_sha256": digest(data)})

    payload_count = len(entries)
    payload_original_bytes = sum(entry["original_bytes"] for entry in entries)
    payload_archived_bytes = sum(entry["archived_bytes"] for entry in entries)
    readme = """# 2026-10-26 完整集成证据\n\n本目录可整体复制到正式验证数据目录。main 保留已结束首轮的原始 source.json、plan/summary、collection 和逐任务 artifacts；budget 保留本轮预算原文。diagnosis 仅包含本轮已完成诊断和必要复现脚本，merge 复用已核对的合并证据包。正在运行的 replay 未收录。\n\nmanifest.json 为每个文件记录原文和归档文件的字节数、SHA-256、编码及原绝对来源。绝对来源只用于追溯；核验不依赖原临时目录。对 encoding=gzip 的文件解压一次即可还原原文；其余文件原样保存。main/source.json 的逐文件摘要完整保留。\n\n运行 `python -B verify.py` 可独立核对所有归档文件、压缩前原文摘要及内层合并包。manifest 不包含自身和 verification-receipt.json，以免自引用；收据另记录 manifest 与 summary 摘要。原始结果、诊断记录和修复后的窄复验分别保存，不互相覆盖。\n"""
    (STAGE / "README.md").write_text(readme)
    summary = {"schema_version": 1, "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "scope": "已结束首轮、预算原文、已完成本轮诊断与既有合并证据；不含运行中replay",
               "main_source": main_summary["source"], "main_status": main_summary["status"],
               "main_exit_code": main_summary["exit_code"], "artifact_groups": artifact_groups,
               "payload_file_count": payload_count, "payload_original_bytes": payload_original_bytes,
               "payload_archived_bytes": payload_archived_bytes,
               "payload_roles": dict(collections.Counter(entry["role"] for entry in entries)),
               "replay_included": False,
               "excluded": ["冻结源码副本", "tmp数据库和测试世界", "node_modules", "cache", "dist", "wheel", "uv依赖", "存量quality-repair历史"]}
    (STAGE / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    for name in ("archive.py", "verify.py", "README.md", "summary.json"):
        data = (STAGE / name).read_bytes()
        entries.append({"path": name, "source_path": str(STAGE / name), "role": "archive-control",
                        "encoding": "identity", "original_bytes": len(data), "original_sha256": digest(data),
                        "archived_bytes": len(data), "archived_sha256": digest(data)})
    manifest = {"schema_version": 1, "created_utc": summary["created_utc"], "hash_algorithm": "SHA-256",
                "scope": "全部归档文件，排除manifest自身和verification-receipt.json；二者由收据单列",
                "file_count": len(entries), "files": sorted(entries, key=lambda entry: entry["path"])}
    (STAGE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    verification = verify(STAGE)
    receipt = {"created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "command": [sys.executable, "-B", str(STAGE / "archive.py")], "exit_code": 0,
               "validation": verification,
               "manifest": {"path": "manifest.json", "bytes": (STAGE / "manifest.json").stat().st_size,
                            "sha256": digest((STAGE / "manifest.json").read_bytes())},
               "summary": {"path": "summary.json", "bytes": (STAGE / "summary.json").stat().st_size,
                           "sha256": digest((STAGE / "summary.json").read_bytes())},
               "all_payload_sources_unchanged_during_copy": True, "replay_included": False}
    (STAGE / "verification-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"path": str(STAGE), "payload_files": payload_count,
                      "total_files": len(entries) + 2,
                      "payload_original_bytes": payload_original_bytes,
                      "payload_archived_bytes": payload_archived_bytes,
                      "all_verified": verification["ok"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    build()
