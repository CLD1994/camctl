"""在 replay 结束后追加证据，保留首轮和已有诊断版本。"""
import argparse
import collections
import datetime
import gzip
import hashlib
import json
import os
import pathlib
import re
import sys

from verify import verify

ROOT = pathlib.Path(__file__).resolve().parent
MAIN = pathlib.Path("/tmp/camctl-integration-all-final-20261026-u19lkvoe")
DIAGNOSIS = pathlib.Path("/tmp/camctl-full-272f-diagnosis")
LOG_SUFFIXES = {".log", ".jsonl", ".xml", ".txt", ".trace"}
EVIDENCE_SUFFIXES = LOG_SUFFIXES | {".json", ".md", ".tsv", ".csv", ".py", ".sh", ".patch"}
EXCLUDED_DIRS = {"source", "snapshot", "tmp", "pytest-tmp", "__pycache__", ".pytest_cache",
                 ".venv", ".venv311", "node_modules", "cache", "dist", "wheel", "wheels",
                 "uv-cache", "staging", "processing", "ready", "worlds"}
TERMINAL = {"finished", "interrupted", "aborted", "failed", "cancelled", "canceled"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def named_path(value):
    name, separator, path = value.partition("=")
    if not separator or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise argparse.ArgumentTypeError("必须使用 name=/绝对路径，name 仅含字母、数字、下划线或连字符")
    result = pathlib.Path(path)
    if not result.is_absolute():
        raise argparse.ArgumentTypeError("证据来源必须为绝对路径")
    return name, result


def iter_evidence(directory):
    for root, dirs, files in os.walk(directory):
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDED_DIRS)
        for name in sorted(files):
            path = pathlib.Path(root) / name
            if path.suffix in EVIDENCE_SUFFIXES:
                yield path


def terminal_run(run):
    summary = json.loads((run / "summary.json").read_text())
    if summary.get("status") not in TERMINAL or "exit_code" not in summary:
        raise RuntimeError("replay 尚无带退出码的结束摘要；不复制运行中证据")
    budget = pathlib.Path(str(run) + "-budget")
    launcher = json.loads((budget / "launcher.json").read_text())
    if launcher.get("status") != "finished" or "exit_code" not in launcher:
        raise RuntimeError("预算 launcher 尚未完成；不复制运行中证据")
    for group in launcher.get("groups", []):
        if group.get("remaining_pids"):
            raise RuntimeError("预算收据仍有未结束进程；保留现状并等待最终收场")
    return summary, budget


def resource_selections(args):
    result = []
    for name, audit_path in args.resource_audit:
        run = MAIN if name == "main" else args.replay
        audit = json.loads(audit_path.read_text())
        if audit.get("runner_status") != "finished" or audit.get("launcher_status") != "finished":
            raise RuntimeError("资源审计尚未覆盖结束边界: " + str(audit_path))
        receipts = audit["receipts"]
        selected = [(row["group"], row["receipt"].get("status"), pathlib.Path(path))
                    for row in receipts for path in row["paths"]]
        paths = [str(path) for _, _, path in selected]
        if len(paths) != len(set(paths)) or len(paths) != audit["raw_receipt_paths"]:
            raise RuntimeError("原始预算收据数量与资源审计不一致: " + name)
        if len(receipts) != audit["unique_groups"]:
            raise RuntimeError("预算进程组数量与资源审计不一致: " + name)
        for _, _, path in selected:
            if (not path.is_absolute() or not path.is_relative_to(run)
                    or path.suffix != ".json" or not path.is_file()):
                raise RuntimeError("资源审计引用的原始JSON缺失或越出本轮目录: " + str(path))
        result.append((name, run, audit_path, audit, selected))
    return result


def append(args):
    # 在读取或复制 replay 动态文件之前确认原归档以及结束边界。
    initial_validation = verify(ROOT)
    replay_summary, replay_budget = terminal_run(args.replay)
    resources = resource_selections(args)
    label = args.label
    batch_root = pathlib.Path("additions") / label
    if (ROOT / batch_root).exists():
        raise RuntimeError("追加批次已存在，请使用新的 label，避免替换已有证据")
    previous_manifest_data = (ROOT / "manifest.json").read_bytes()
    previous_receipt_data = (ROOT / "verification-receipt.json").read_bytes()
    manifest = json.loads(previous_manifest_data)
    rows = {row["path"]: row for row in manifest["files"]}
    original_main = {path: row["archived_sha256"] for path, row in rows.items()
                     if path.startswith("main/")}
    added = []
    changed = []
    reused_versions = []
    versions_by_source = {(row["source_path"], row["original_sha256"]): row["path"]
                          for row in rows.values()}
    skipped_identical = 0
    selected_sources = []

    def add_bytes(original, relative, role, source_path, compress=False, version_changed=True):
        nonlocal skipped_identical
        relative = pathlib.Path(relative)
        encoded_path = pathlib.Path(str(relative) + ".gz") if compress else relative
        key = str(encoded_path)
        existing = rows.get(key)
        if existing and existing["original_sha256"] == digest(original):
            skipped_identical += 1
            return
        if existing:
            if not version_changed:
                raise RuntimeError("既有证据不可覆盖: " + key)
            archived_version = versions_by_source.get((str(source_path), digest(original)))
            if archived_version is not None:
                reused_versions.append({"logical_path": key, "archive_path": archived_version})
                skipped_identical += 1
                return
            changed.append({"original_archive_path": key, "previous_original_sha256": existing["original_sha256"],
                            "new_original_sha256": digest(original)})
            encoded_path = batch_root / "versions" / encoded_path
            key = str(encoded_path)
        if key in rows or (ROOT / encoded_path).exists():
            raise RuntimeError("追加目标已存在: " + key)
        data = gzip.compress(original, compresslevel=9, mtime=0) if compress else original
        target = ROOT / encoded_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        row = {"path": key, "source_path": str(source_path), "role": role,
               "encoding": "gzip" if compress else "identity", "original_bytes": len(original),
               "original_sha256": digest(original), "archived_bytes": len(data), "archived_sha256": digest(data),
               "append_label": label}
        rows[key] = row
        versions_by_source[(str(source_path), row["original_sha256"])] = key
        added.append(row)

    def add_file(source, relative, role, preserve_raw=False, version_changed=True):
        if not source.is_file() or source.is_symlink():
            raise RuntimeError("来源缺失或不是普通文件: " + str(source))
        before = source.stat()
        original = source.read_bytes()
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("复制期间来源变化: " + str(source))
        add_bytes(original, relative, role, source, not preserve_raw and source.suffix in LOG_SUFFIXES, version_changed)
        selected_sources.append((source, len(original), digest(original)))

    # 保存更新整体清单之前的完整索引，保留首轮归档形成时的状态。
    add_bytes(previous_manifest_data, batch_root / "previous-manifest.json", "archive-index-history", ROOT / "manifest.json")
    add_bytes(previous_receipt_data, batch_root / "previous-verification-receipt.json", "archive-index-history", ROOT / "verification-receipt.json")
    for source in sorted(args.replay.iterdir()):
        if source.is_file() and source.suffix in EVIDENCE_SUFFIXES:
            add_file(source, pathlib.Path("replay") / source.name, "replay-metadata",
                     preserve_raw=source.suffix == ".json")
    for required in ("source.json", "plan.json", "summary.json"):
        if "replay/" + required not in rows:
            raise RuntimeError("replay 必需元数据缺失: " + required)
    group_counts = {}
    absent = []
    for section in ("collection", "tasks"):
        count = 0
        for group in sorted((args.replay / section).iterdir()):
            artifact = group / "artifacts"
            if not artifact.is_dir():
                continue
            count += 1
            for source in sorted(artifact.iterdir()):
                if source.is_file():
                    add_file(source, pathlib.Path("replay") / section / group.name / "artifacts" / source.name,
                             "replay-" + section + "-artifact")
            if section == "tasks":
                for expected in ("command.json", "result.json", "preparations.json", "inventory.json", "cases.json", "progress.json", "events.jsonl", "output.log"):
                    if not (artifact / expected).is_file():
                        absent.append({"group": group.name, "missing_artifact": expected})
        group_counts[section] = count
    for source in sorted((args.replay / "control").glob("*.py")):
        add_file(source, pathlib.Path("replay/control") / source.name, "replay-instrumentation", True)
    for directory, dirs, files in os.walk(replay_budget):
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDED_DIRS)
        for name in sorted(files):
            source = pathlib.Path(directory) / name
            add_file(source, pathlib.Path("budget/replay") / source.relative_to(replay_budget), "replay-budget-original", True)
    for run, category in ((MAIN, "main"), (args.replay, "replay")):
        driver = pathlib.Path(str(run) + "-driver.log")
        add_file(driver, pathlib.Path(category) / "driver.log", category + "-driver-log", version_changed=False)
    resource_counts = {}
    for name, run, audit_path, audit, selected in resources:
        add_file(audit_path, pathlib.Path("audits/resources") / name / "resource-audit.json", "resource-audit-original", True)
        index = []
        for group, status, source in selected:
            relative = pathlib.Path("resource-receipts") / name / source.relative_to(run)
            add_file(source, relative, "per-process-budget-raw-receipt", True)
            index.append({"source_path": str(source), "archive_path": str(relative), "group": group,
                          "group_status_from_audit": status})
        resource_counts[name] = {"raw_json_count": len(selected), "unique_groups": audit["unique_groups"],
                                 "unfinished_groups": len(audit["unfinished_receipts"])}
        raw_index = {"source_audit": str(audit_path), **resource_counts[name],
                     "unfinished_receipts_as_audited": audit["unfinished_receipts"], "files": index}
        relative = pathlib.Path("resource-receipts") / name / "index.json"
        add_bytes((json.dumps(raw_index, ensure_ascii=False, indent=2) + "\n").encode(), relative,
                  "per-process-budget-receipt-index", ROOT / relative)
    # 实际选择、执行命令和 session 中断只收录本轮明确的原始记录。
    for name in ("replay-ready-selection.json", "session-replay-selection.json", "replay-selection.json",
                 "replay-command.json", "session-interrupt.json", "run-replay.py"):
        source = pathlib.Path("/tmp/camctl-full-272f-" + name)
        add_file(source, pathlib.Path("execution-records") / name, "actual-selection-command-interruption", True)
    combination = pathlib.Path("/tmp/camctl-full-272f-final-combination")
    for name in ("receipt.json", "results.xml", "output.log"):
        add_file(combination / name, pathlib.Path("final-combination") / name, "final-narrow-combination")
    add_file(pathlib.Path(str(combination) + ".py"), "final-combination/run.py", "final-narrow-combination-script", True)
    # 新增诊断放原相对目录；变化内容保存为本批次版本，原诊断与 main 均不覆盖。
    for source in iter_evidence(DIAGNOSIS):
        add_file(source, pathlib.Path("diagnosis") / source.relative_to(DIAGNOSIS), "completed-diagnosis-addition")
    for name, source_root in args.audit + args.evidence:
        sources = list(iter_evidence(source_root)) if source_root.is_dir() else [source_root]
        for source in sources:
            relative = source.relative_to(source_root) if source_root.is_dir() else pathlib.Path(source.name)
            add_file(source, batch_root / "supplemental" / name / relative, "final-audit-or-narrow-evidence")
    # 本批次所有新来源在整体写入后再比一次，失败时不发布新清单或成功收据。
    for source, size, expected_hash in selected_sources:
        data = source.read_bytes()
        if len(data) != size or digest(data) != expected_hash:
            raise RuntimeError("追加收场核对发现来源变化: " + str(source))
    batch_summary = {"label": label, "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                     "replay_source": str(args.replay), "replay_status": replay_summary["status"],
                     "replay_exit_code": replay_summary["exit_code"], "artifact_groups": group_counts,
                     "raw_resource_receipts": resource_counts,
                     "missing_task_artifacts_as_observed": absent,
                     "added_files_before_batch_summary": len(added), "unchanged_files_skipped": skipped_identical,
                     "versioned_changes": changed, "source_recheck_file_count": len(selected_sources),
                     "reused_prior_versions": reused_versions,
                     "supplemental_sources": [{"name": name, "source": str(source)} for name, source in args.audit + args.evidence],
                     "original_main_files_preserved": len(original_main), "initial_archive_validation": initial_validation}
    add_bytes((json.dumps(batch_summary, ensure_ascii=False, indent=2) + "\n").encode(),
              batch_root / "summary.json", "append-summary", ROOT / batch_root / "summary.json")
    for path, expected in original_main.items():
        assert rows[path]["archived_sha256"] == expected
        assert digest((ROOT / path).read_bytes()) == expected
    manifest["files"] = sorted(rows.values(), key=lambda row: row["path"])
    manifest["file_count"] = len(rows)
    manifest["updated_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    manifest.setdefault("append_batches", []).append(str(batch_root / "summary.json"))
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    validation = verify(ROOT)
    receipt = {"created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "command": [sys.executable, "-B", str(ROOT / "append_evidence.py"), *sys.argv[1:]],
               "exit_code": 0, "append_label": label, "validation": validation, "replay_included": True,
               "original_main_files_preserved": len(original_main),
               "manifest": {"path": "manifest.json", "bytes": (ROOT / "manifest.json").stat().st_size,
                            "sha256": digest((ROOT / "manifest.json").read_bytes())},
               "batch_summary": {"path": str(batch_root / "summary.json"),
                                 "sha256": digest((ROOT / batch_root / "summary.json").read_bytes())}}
    (ROOT / "verification-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"added_files": len(added), "manifest_files": len(rows), "all_verified": validation["ok"],
                      "original_main_files_preserved": len(original_main), "append_summary": str(batch_root / "summary.json")},
                     ensure_ascii=False, indent=2))


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=pathlib.Path, required=True, help="已结束 replay 根目录；预算取同名 -budget 目录")
    parser.add_argument("--label", required=True, help="本次追加批次的唯一名称")
    parser.add_argument("--audit", type=named_path, action="append", default=[], help="已完成独立审计，格式 name=/绝对路径")
    parser.add_argument("--resource-audit", type=named_path, action="append", required=True,
                        help="最终资源审计，必须分别传 main=/绝对路径 和 replay=/绝对路径；逐份复制其receipts[*].paths原始JSON")
    parser.add_argument("--evidence", type=named_path, action="append", default=[], help="选择、中断或窄验证补充证据，格式 name=/绝对路径")
    args = parser.parse_args()
    if not args.replay.is_absolute() or not re.fullmatch(r"[A-Za-z0-9_-]+", args.label):
        parser.error("replay 必须使用绝对路径，label 仅含字母、数字、下划线或连字符")
    if len(args.resource_audit) != 2 or {name for name, _ in args.resource_audit} != {"main", "replay"}:
        parser.error("必须恰好指定 main 和 replay 两份最终资源审计")
    return args


if __name__ == "__main__":
    append(arguments())
