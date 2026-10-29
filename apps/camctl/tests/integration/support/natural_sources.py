"""来源转换测试从明确端口及持久化事实建立新的正式会话。"""

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.devices.bindings import DeviceBinding

from .capture import capture_factory


async def cold_source_runtime(owned, resources, state, *, evidence=None, results=None, **changes):
    """先完成旧文件责任并关闭 worker，再以原部署输入建立新工厂。"""
    options = {**state['options'], **changes}
    if evidence is not None:
        options['evidence'] = evidence
    if results is not None:
        options['results'] = results
    await resources.restart_database(owned)
    state['factory'] = await capture_factory(owned, **options)
    return await state['factory'](owned)


async def legacy_result_runtime(owned, resources, state, driver):
    from ..capture.result_consumer_fixtures import RESULT_EVIDENCE
    return await cold_source_runtime(owned, resources, state, evidence=RESULT_EVIDENCE,
        results=DriverResultListing(driver, DeviceBinding('cam-1', 'camctl-adb'), RESULT_EVIDENCE))


async def result_consumer_world(tmp_path, consumer, resources, *, catalog=None):
    """普通照片和录像复用正式结果装配，端口脚本由调用方提供。"""
    from decimal import Decimal
    from unittest.mock import create_autospec
    from camctl.devices.ports import ResultDriver
    from camctl.operations.attempts import AttemptConfig
    from .result_cases import ResultCase

    case = ResultCase(resources)
    owned, runtime, action_id, handler = await case.world(tmp_path, consumer, catalog=catalog,
        assembly_options={'check_config': AttemptConfig(2, Decimal('1'), Decimal(0))})
    driver = create_autospec(ResultDriver, instance=True)
    case.results.delegate = DriverResultListing(
        driver, DeviceBinding('cam-1', 'camctl-adb'), case.evidence)
    return case, owned, runtime, action_id, handler, driver


async def unanchored_source_world(tmp_path, resources, startup, stopper):
    """从真实受理和正式装配建立尚未派发 START 的历史恢复场景。"""
    from decimal import Decimal
    from types import SimpleNamespace
    from unittest.mock import create_autospec
    from camctl.capture.handlers import capture_handler
    from camctl.capture.timelapse import CaptureWaitConfig
    from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
    from camctl.devices.ports import DriverDeclaration, ResultDriver
    from camctl.operations.attempts import AttemptConfig
    from ..capture.result_consumer_fixtures import RESULT_EVIDENCE, consumer_world
    from ..capture.test_capture_contract import _NOW
    from ..capture.test_natural_wait_time_eligibility import SmallNativeCatalog
    from ..capture.test_record_file_completion_wait import _REGISTRY

    wall, mono = [_NOW], [5_000_000_000]
    results = create_autospec(ResultDriver, instance=True)
    evidence = _REGISTRY.with_contract(RESULT_EVIDENCE.contract('timelapse_sent', 1))
    state = {'options': dict(evidence=evidence,
        results=DriverResultListing(results, DeviceBinding('cam-1', 'camctl-adb'), evidence),
        stopper=stopper, wall_us=lambda: wall[0], monotonic_ns=lambda: mono[0],
        check_config=AttemptConfig(1, Decimal('1'), Decimal(0)),
        wait_config=lambda action: CaptureWaitConfig(1000, 200, 0, startup))}

    async def assemble(owned, *, driver):
        options = state['options']
        options['driver'] = driver
        options['drivers'] = DriverRegistry((DriverEntry('camctl-adb',
            SimpleNamespace(control=driver.control, stop=stopper.stop),
            DriverDeclaration(control_supported=True, stop_supported=True,
                query_supported=False, result_supported=False, read_supported=False,
                digest_supported=False, delete_supported=False,
                adb_foreground_recovery_operations=frozenset({'control'})),
            evidence, DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
        state['factory'] = await capture_factory(owned, **options)
        return await state['factory'](owned)

    owned, runtime, aid, handler = await consumer_world(tmp_path, 'timelapse',
        catalog=SmallNativeCatalog(startup), start=False, runtime_factory=assemble)
    return owned, runtime, aid, capture_handler(handler), wall, results, state


async def recovered_source_runtime(owned, resources, state, *, evidence=None):
    """通过原驱动声明与可靠历史上界恢复已保存的 RESULTS 页。"""
    from types import SimpleNamespace
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
    from camctl.devices.evidence import EvidenceContract
    from camctl.devices.ports import DriverDeclaration
    options = state['options']
    evidence = (options['evidence'] if evidence is None else evidence).with_contract(
        EvidenceContract('adb_foreground_recovery', 1, 'result', frozenset()))
    drivers = DriverRegistry((DriverEntry('camctl-adb',
        SimpleNamespace(control=options['driver'].control, stop=options['stopper'].stop),
        DriverDeclaration(control_supported=True, stop_supported=True,
            query_supported=False, result_supported=False, read_supported=False,
            digest_supported=False, delete_supported=False,
            adb_foreground_recovery_operations=frozenset({'result'})),
        evidence, DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
    horizon = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
    return await cold_source_runtime(owned, resources, state, evidence=evidence,
        drivers=drivers, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
        recovery_max_event_id=lambda: horizon)


async def save_historical_source_page(owned, action_id, ticket, port, evidence, occurred_at):
    """按公开页存储格式保存旧调用实际返回，当前处理器随后负责恢复消费。"""
    from decimal import Decimal
    from camctl.capture.result_pages import ResultPageSave
    from camctl.contracts.json_values import parse_exact_json
    from camctl.contracts.values import new_operation_key
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    scope, = owned.connection.execute('SELECT output_scope_json FROM device_activities WHERE action_id=?',
        (action_id,)).fetchone()
    page = await port.list_page(ticket, cursor=None, timeout_s=Decimal('1'),
        output_scope=parse_exact_json(scope), completion_context=None)
    request = ResultPageSave(ticket, 1, None, validate_outcome(ticket, page.outcome, evidence), occurred_at)
    saved = CaptureRepository().save_result_page(request, new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    return CaptureRepository().read_result_page(saved.value, owned)


def save_historical_direct_files(owned, action_id, saved):
    """旧 direct 来源的文件依规定的公开归属和写完命令建立。"""
    from camctl.capture.files import FileCompletionSave, OwnershipSave
    from camctl.contracts.enums import enum_for
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    repository = CaptureRepository()
    identities = dict(saved.file_ids)
    for entry in saved.page.entries:
        file_id = identities[entry.identity]
        for method, request in (
            (repository.save_file_ownership, OwnershipSave(file_id, action_id,
                int(enum_for('device_files.ownership_evidence_json.method').TASK_SCOPE),
                int(enum_for('device_files.role').ORIGINAL), entry.evidence, saved.occurred_at)),
            (repository.save_file_completion, FileCompletionSave(file_id,
                int(enum_for('device_files.completion_state').COMPLETE), saved.occurred_at,
                basis=int(enum_for('device_files.completion_evidence_json.basis').DEVICE_GUARANTEE),
                observation=entry.evidence, size_bytes=entry.size_bytes)),
        ):
            receipt = method(request, new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
