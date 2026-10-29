"""仅使用归档内容，核对全部文件及原文可还原性。"""
import gzip
import hashlib
import io
import json
import pathlib
import sys
import tarfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def verify(root):
    root = pathlib.Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    expected_paths = set()
    restored_bytes = stored_bytes = 0
    for record in manifest["files"]:
        relative = pathlib.PurePosixPath(record["path"])
        assert not relative.is_absolute() and ".." not in relative.parts
        assert record["path"] not in expected_paths
        expected_paths.add(record["path"])
        data = (root / relative).read_bytes()
        assert len(data) == record["archived_bytes"], record["path"]
        assert digest(data) == record["archived_sha256"], record["path"]
        original = gzip.decompress(data) if record["encoding"] == "gzip" else data
        assert len(original) == record["original_bytes"], record["path"]
        assert digest(original) == record["original_sha256"], record["path"]
        restored_bytes += len(original)
        stored_bytes += len(data)
    actual_paths = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
    allowed_metadata = {"manifest.json", "verification-receipt.json"}
    assert actual_paths - allowed_metadata == expected_paths

    merge = root / "merge"
    inner = json.loads((merge / "manifest.json").read_text())
    inner_expected = {entry["path"]: entry for entry in inner["files"]}
    with tarfile.open(merge / "merge-272f-evidence.tar.gz", "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers() if member.isfile()}
        assert set(members) == set(inner_expected) | {"manifest.json"}
        assert archive.extractfile(members["manifest.json"]).read() == (merge / "manifest.json").read_bytes()
        for name, record in inner_expected.items():
            data = archive.extractfile(members[name]).read()
            assert len(data) == record["bytes"] and digest(data) == record["sha256"], name

    source = json.loads((root / "main/source.json").read_text())
    resource_counts = {}
    records = {record["path"]: record for record in manifest["files"]}
    for index_path in sorted((root / "resource-receipts").glob("*/index.json")):
        run = index_path.parent.name
        index = json.loads(index_path.read_text())
        audit = json.loads((root / "audits/resources" / run / "resource-audit.json").read_text())
        expected_sources = {path for receipt in audit["receipts"] for path in receipt["paths"]}
        assert len(index["files"]) == index["raw_json_count"] == audit["raw_receipt_paths"]
        assert {row["source_path"] for row in index["files"]} == expected_sources
        assert index["unfinished_receipts_as_audited"] == audit["unfinished_receipts"]
        for row in index["files"]:
            record = records[row["archive_path"]]
            assert record["role"] == "per-process-budget-raw-receipt"
            assert record["source_path"] == row["source_path"]
            assert record["encoding"] == "identity" and row["archive_path"].endswith(".json")
        resource_counts[run] = {"raw_json_files": len(index["files"]),
                                "unique_groups": audit["unique_groups"],
                                "unfinished_groups_preserved": len(audit["unfinished_receipts"])}
    return {"ok": True, "manifest_file_count": len(expected_paths),
            "archived_bytes_excluding_manifest_and_receipt": stored_bytes,
            "restored_bytes": restored_bytes,
            "source_per_file_record_count": len(source["files"]),
            "nested_merge_payload_files_verified": len(inner_expected),
            "resource_receipts_verified_by_run": resource_counts,
            "all_archived_hashes_match": True, "all_original_hashes_restore": True,
            "archive_has_no_unlisted_payload_files": True,
            "verification_requires_original_sources": False}


if __name__ == "__main__":
    print(json.dumps(verify(pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).parent),
                     ensure_ascii=False, indent=2))
