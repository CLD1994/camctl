"""从同一次结果扫描的可靠页区分预览缺席与未确认。"""

from pathlib import PurePosixPath

PREVIEW_VALIDATION_VERSION = 1
PREVIEW_VALIDATION_FIELD = "preview_validation_version"


def read_preview_validation_version(evidence):
    version = evidence.get(PREVIEW_VALIDATION_FIELD, 0)
    if type(version) is not int or version not in (0, PREVIEW_VALIDATION_VERSION):
        raise ValueError("预览依据版本无效")
    return version


def unconfirmed_previews(originals, pages):
    """返回缺少可靠预览依据的原片 ID；不读取文件或改变原片结果。

    originals 只包含已经确认存在、尚无正式预览的原片路径。
    pages 必须来自同一结果尝试并由调用方核实连续性。扫描未完整
    结束时不能证明缺席；已观察 LRF 却没有正式预览也不能报告缺席。
    """
    families = {}
    for identity, path in originals.items():
        file = PurePosixPath(path)
        families.setdefault((file.parent, file.stem), set()).add(identity)
    observed = set()
    complete = False
    reliable = True
    for page in pages:
        reliable = reliable and page.outcome.error is None
        complete = page.scan_complete and page.set_finalized and page.outcome.error is None
        for entry in page.entries:
            if entry.format_id == "lrf":
                file = PurePosixPath(entry.identity)
                observed.update(families.get((file.parent, file.stem), ()))
        evidence = page.outcome.settlement.evidence
        for item in evidence.data.get("optional_preview_observations", ()):
            path = item.get("preview_identity")
            if isinstance(path, str):
                file = PurePosixPath(path)
                identities = families.get((file.parent, file.stem), ())
                if (item.get("state") == "preview_missing" and item.get("scan_complete") is True
                        and item.get("observed_preview_identities") == []):
                    observed.difference_update(identities)
                else:
                    observed.update(identities)
    return frozenset(observed if complete and reliable else originals)
