"""拍摄测试通过会话工厂装配，统一拥有数据库线程和文件任务的收场。"""

from contextlib import asynccontextmanager
from contextvars import ContextVar
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import DriverDeclaration
from camctl.host_files.tasks import FileTaskExecutor
from camctl.persistence.business import BusinessDatabase
from camctl.session.supervision import Supervisor

from .database import business_database, database_path


_current_session = ContextVar("capture_test_session", default=None)


def capture_assembly(*, driver, evidence, staging, results=None, stopper=None,
                     query=None, directory=None, devices=None, device_id="cam-1",
                     driver_id="camctl-adb", drivers=None, wait_config=None, wall_us=None,
                     monotonic_ns=None, start_config=None, stop_config=None,
                     check_config=None, query_config=None, residual_config=None, **options):
    """按真实驱动登记和设备配置返回生产工厂，不组装内部运行对象。

    对需要真实读取、摘要或准备能力的驱动，调用方直接传入完整的
    drivers 登记和 devices 配置到 session_capture_assembly。
    本入口只集中控制、停止、查询和目录端口的契约替身组合。
    独立 ResultFilesPort 从生产工厂的 results 参数注入，不声明为驱动能力。
    """
    ports = SimpleNamespace(control=driver.control)
    if stopper is not None:
        ports.stop = stopper.stop
    if query is not None:
        ports.query_state = query.query_state
    if directory is not None:
        ports.read_directory = directory.read_directory
    declaration = DriverDeclaration(
        control_supported=True, stop_supported=stopper is not None,
        query_supported=query is not None, result_supported=False,
        read_supported=False, digest_supported=False, delete_supported=False,
        directory_supported=directory is not None,
    )
    overrides = (start_config, stop_config, check_config, query_config, residual_config)
    if devices is None:
        configured_devices = {device_id: {"driver": driver_id}}
    elif not any(value is not None for value in overrides):
        # 完整部署输入由生产工厂读取；不另建一份配置权威来源。
        configured_devices = devices
    else:
        configured_devices = {identity: {
            key: dict(value) if isinstance(value, Mapping) else value
            for key, value in declaration.items()}
            for identity, declaration in devices.items()}
    for config, section, maximum, timeout, retry in (
        (start_config, "recording", "max_start_attempts", "start_timeout_s", "start_retry_interval_s"),
        (stop_config, "recording", "max_stop_attempts", "stop_timeout_s", "stop_retry_interval_s"),
        (check_config, "result_check", "max_attempts", "call_timeout_s", "retry_interval_s"),
        (query_config, "query", "max_attempts", "timeout_s", "retry_interval_s"),
        (residual_config, "residual_stop", "max_attempts", "timeout_s", "retry_interval_s"),
    ):
        if config is not None:
            configured_devices[device_id].setdefault(section, {}).update({
                maximum: config.max_attempts, timeout: config.timeout_s,
                retry: config.retry_interval_s})
    registry = drivers if drivers is not None else DriverRegistry((
        DriverEntry(driver_id, ports, declaration, evidence,
            DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
    return session_capture_assembly(
        devices=configured_devices,
        drivers=registry, results=results, staging=Path(staging),
        wait_config=wait_config if wait_config is not None else execution_wait_config,
        wall_us=wall_us, monotonic_ns=monotonic_ns, **options,
    )


class CaptureTestSession:
    """单个测试的资源拥有者；先等待实际文件责任，再关闭业务数据库。"""

    def __init__(self):
        self._databases = {}
        self._supervisors = {}

    @staticmethod
    def _path(owned_or_path):
        return (Path(owned_or_path) if isinstance(owned_or_path, (str, Path))
                else database_path(owned_or_path))

    async def database(self, owned_or_path):
        if isinstance(owned_or_path, BusinessDatabase):
            return owned_or_path
        path = self._path(owned_or_path)
        if path not in self._databases:
            context = business_database(path)
            self._databases[path] = (await context.__aenter__(), context)
        return self._databases[path][0]

    async def restart_database(self, owned_or_path):
        """先结束本作用域该库的实际责任和 worker，再建立新连接。

        关闭观察连接不会关闭业务 worker。需要跨连接恢复时显式调用
        此入口，或退出整个 capture_test_session 后进入新作用域。
        """
        path = self._path(owned_or_path)
        await self._close_database(path)
        return await self.database(path)

    async def factory(self, owned_or_database, *, driver, evidence, staging=None,
                      device_id="cam-1", **options):
        """保留生产工厂的会话责任；每次调用仍由工厂创建运行时。

        原保存申请由该工厂持有。跨连接时先显式 restart_database，
        再调用同一工厂；需要进程恢复则以明确的原输入新建工厂。
        """
        database = await self.database(owned_or_database)
        supervisor = Supervisor()
        factory = capture_assembly(
            driver=driver, evidence=evidence,
            staging=Path(database.metadata.staging_path) if staging is None else staging,
            device_id=device_id, file_executor=FileTaskExecutor(supervisor), **options,
        )
        async def build(owned_or_database, selected_device_id=device_id):
            current = await self.database(owned_or_database)
            owners = self._supervisors.setdefault(current, [])
            if supervisor not in owners:
                owners.append(supervisor)
            runtime = await factory(current, selected_device_id)
            assert runtime is not None, "拍摄测试须声明控制能力"
            return runtime
        return build

    async def runtime(self, owned_or_database, **options):
        factory = await self.factory(owned_or_database, **options)
        return await factory(owned_or_database)

    async def _close_database(self, path):
        resource = self._databases.pop(path, None)
        if resource is None:
            return
        database, context = resource
        errors = []
        for supervisor in self._supervisors.pop(database, []):
            try:
                result = await supervisor.drain_required()
                errors.extend(failure.exception or RuntimeError(failure.error)
                              for failure in result.failed)
            except BaseException as error:
                errors.append(error)
        try:
            await context.__aexit__(None, None, None)
        except BaseException as error:
            errors.append(error)
        if errors:
            raise BaseExceptionGroup("拍摄测试实际资源收场失败", errors)

    async def close(self):
        errors = []
        for path in tuple(self._databases):
            try:
                await self._close_database(path)
            except BaseException as error:
                errors.append(error)
        # 调用方传入的数据库仍由调用方关闭，本作用域拥有的文件责任照常收场。
        for database, supervisors in tuple(self._supervisors.items()):
            del self._supervisors[database]
            for supervisor in supervisors:
                try:
                    result = await supervisor.drain_required()
                    errors.extend(failure.exception or RuntimeError(failure.error)
                                  for failure in result.failed)
                except BaseException as error:
                    errors.append(error)
        if errors:
            raise BaseExceptionGroup("拍摄测试资源关闭失败", errors)


@asynccontextmanager
async def capture_test_session():
    """显式资源作用域；同一测试内可装配多个正式运行时。"""
    session = CaptureTestSession()
    token = _current_session.set(session)
    try:
        try:
            yield session
        except BaseException as original:
            try:
                await session.close()
            except BaseException as closing:
                raise BaseExceptionGroup("拍摄操作和资源收场均失败", [original, closing])
            raise
        else:
            await session.close()
    finally:
        _current_session.reset(token)


async def capture_runtime(owned_or_database, **options):
    """供具有 capture_test_session 夹具的用例取得正式装配运行时。"""
    session = _current_session.get()
    if session is None:
        raise RuntimeError("拍摄测试须使用 capture_test_session 资源作用域")
    return await session.runtime(owned_or_database, **options)


async def capture_factory(owned_or_database, **options):
    """由当前测试作用域管理正式工厂及其实际文件责任。"""
    session = _current_session.get()
    if session is None:
        raise RuntimeError("拍摄测试须使用 capture_test_session 资源作用域")
    return await session.factory(owned_or_database, **options)


async def capture_database(owned_or_path):
    """取得当前测试资源作用域拥有的真实业务数据库。"""
    session = _current_session.get()
    if session is None:
        raise RuntimeError("拍摄测试须使用 capture_test_session 资源作用域")
    return await session.database(owned_or_path)


async def restart_capture_database(owned_or_path):
    """显式结束当前测试该库的文件责任与旧 worker，再打开新业务连接。"""
    session = _current_session.get()
    if session is None:
        raise RuntimeError("拍摄测试须使用 capture_test_session 资源作用域")
    return await session.restart_database(owned_or_path)
