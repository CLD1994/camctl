"""通过正式拍摄流程准备 Hasselblad DEVICE 照片来源。"""

import json
from pathlib import Path
from types import SimpleNamespace

from camctl.bootstrap.flows import capture_flow
from camctl.devices.catalog import DriverDefinitions, build_catalog
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID, driver_definition
from camctl.devices.drivers.registry import DriverRegistry

from ..capture.test_host_photo_flow import _NOW, _entry, _world
from .capture import capture_database


async def prepare_device_photo(tmp_path, capture_resources):
    """返回配置、观察连接、驱动和原图身份；调用方负责关闭观察连接。

    真实受理与拍摄流程保存原活动、机身和 DEVICE 原图与预览。
    来源不含内容大小或摘要；后续读取由调用方独立提供工具响应。
    业务数据库与文件任务由 capture_resources 负责收场。
    """
    cfg, owned, driver, _, _ = await _world(tmp_path, storage="device", start_first=False)
    try:
        database = await capture_database(owned)
        factory = await capture_resources.factory(owned, driver=driver, evidence=_entry(driver).evidence,
            devices=cfg.devices, drivers=DriverRegistry((_entry(driver),)),
            staging=Path(cfg.paths.staging), wait_config=lambda _: None,
            wall_us=lambda: _NOW, monotonic_ns=lambda: 9_000_000_000)
        flow = capture_flow(factory)
        await flow(SimpleNamespace(database=database, clock=SimpleNamespace(utc_micros=lambda: _NOW)))
        await flow.settle()
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        output_id, file_id, size, digest, locator = owned.connection.execute(
            "SELECT o.id,f.id,f.size_bytes,f.sha256,f.locator_json FROM outputs o"
            " JOIN device_files f ON f.id=o.device_file_id"
            " WHERE o.source_action_id=1 AND o.kind=1").fetchone()
        locator = json.loads(locator)
        assert size is None and digest is None and locator['variant'] == 'original'
        catalog = build_catalog(cfg, DriverDefinitions({DRIVER_ID: driver_definition()}))
        return cfg, owned, driver, catalog, output_id, file_id
    except BaseException:
        owned.connection.close()
        raise
