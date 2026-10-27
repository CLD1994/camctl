"""拍摄组件测试的会话工厂和真实业务数据库装配。"""

from pathlib import Path

from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import DriverDeclaration
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing


async def assemble_capture(owned, *, driver, evidence, results, staging,
                           wall_us, monotonic_ns, recording=None,
                           recovery_boundary=None, recovery_max_event_id=None,
                           declaration=None):
    """使用独立连接和正式会话工厂装配；调用方负责关闭返回的数据库。"""
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    database = await BusinessDatabase.open(
        lambda: open_existing(path, DbOpenMode.EXISTING_RW, DbConfig()))
    if declaration is None:
        declaration = DriverDeclaration(True, False, False, False, False, False, False)
    entry = DriverEntry("camctl-adb", driver, declaration, evidence,
                        DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
    options = {}
    if recovery_boundary is not None:
        options["recovery_boundary"] = recovery_boundary
        options["recovery_max_event_id"] = recovery_max_event_id
    try:
        factory = session_capture_assembly(
            devices={"cam-1": {"driver": "camctl-adb", "recording": recording or {}}},
            drivers=DriverRegistry((entry,)), results=results,
            staging=staging, wait_config=lambda _: CaptureWaitConfig(600_000, 0),
            wall_us=wall_us, monotonic_ns=monotonic_ns, media_enabled=False,
            **options)
        runtime = await factory(database, "cam-1")
    except BaseException:
        await database.close()
        raise
    return runtime, database
