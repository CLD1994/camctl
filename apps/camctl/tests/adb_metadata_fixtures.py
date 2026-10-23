"""供结果读取器测试使用的批量文件信息替身。"""

from camctl.operations.process import LocalExit, RawToolOutcome


async def collect_fake_metadata_many(filesystem, identities, *, timeout_s, stop):
    from camctl.devices.drivers.adb_cameras.filesystem import FileMetadataBatch, FileMetadataItem

    items = []
    fields = [b"CAMCTL-METADATA/1"]
    for identity in identities:
        result = await filesystem.metadata(identity, timeout_s=timeout_s, stop=stop)
        items.append(FileMetadataItem(identity, result.value, result.error))
        fields.extend((identity.path.encode(), b"0" if result.error is None else b"1",
            f"{result.value}\n".encode() if result.error is None else b"",
            b"" if result.error is None else b"metadata failed"))
    raw = RawToolOutcome(LocalExit(exit_code=0), b"\0".join((*fields, b"END", b"")),
        None, None, stderr=b"")
    return FileMetadataBatch(raw, tuple(items), next((item.error for item in items if item.error), None))
