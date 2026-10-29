"""拍摄与 DEVICE 读取共用的设备检查装配。"""

from decimal import Decimal

from camctl.bootstrap.capture_assembly import _device_attempts, _hasselblad_identity, query_attempt_config
from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.files import camera_file_identity_from_source
from camctl.devices.drivers.hasselblad.identity import CameraIdentity
from camctl.operations.availability import AvailabilityProgress
from camctl.persistence.repositories.availability_create import CreateAvailabilityCheck
from camctl.persistence.transaction import row_facts


class AvailabilityAssembly:
    def __init__(self, coordinator, devices, drivers, preparation_leads):
        self.coordinator = coordinator
        self.devices = devices
        self.drivers = drivers
        self.preparation_leads = preparation_leads
        self._recovery_after = 0
        self._finish_after = 0

    def binding_diagnostic(self, action_id, copy_id=None, *, has_read_copy=True):
        key = (action_id, copy_id) if has_read_copy else (action_id, ("item", copy_id))
        return self.coordinator.binding_diagnostics_by_request.get(key)

    def capture_selection(self, device_id, now):
        """取得本会话对该机身实际采用的拍摄选择输入。"""
        declaration = self.devices.get(device_id)
        if declaration is None or declaration.get("driver") != DRIVER_ID:
            return None
        from camctl.persistence.repositories.capture_selection import CaptureSelectionInput
        return CaptureSelectionInput(device_id, DRIVER_ID, _hasselblad_identity(declaration),
                                     self.preparation_leads[device_id], now)

    async def resume(self, context):
        """发现旧检查的未交付结果，包括触发动作已经终态的责任。"""
        self.coordinator.wake = context.wake
        def read(owned):
            return tuple(owned.connection.execute(
                "SELECT r.id,c.id FROM operation_runs r"
                " JOIN device_availability_checks c ON c.id=r.availability_check_id"
                " JOIN operation_attempts t ON t.run_id=r.id"
                " WHERE (t.status=1 OR t.result_event_id IS NULL) AND r.id>? ORDER BY r.id LIMIT 128",
                (self._recovery_after,)).fetchall())
        rows = await context.database.read(read)
        self._recovery_after = rows[-1][0] if len(rows) == 128 else 0
        if self._recovery_after and context.wake is not None:
            from camctl.scheduling.notifications import WakeReason
            context.wake.mark_changed(WakeReason.ACTIVITY_FINISHED)
        for run_id, check_id in rows:
            if run_id in self.coordinator.calls:
                continue
            def facts(owned):
                from camctl.operations.availability import _run_facts
                return _run_facts(owned, run_id), row_facts(owned.connection, "device_availability_checks", check_id)
            run, check = await context.database.read(facts)
            identity = CameraIdentity(**check["expected_identity_json"])
            entry = self.drivers.entry(check["driver_id"])
            if entry is None:
                continue
            now = context.clock.utc_micros()
            request = CreateAvailabilityCheck(check["action_id"], identity, now, Decimal(0), now,
                                              trigger_copy_id=check["trigger_copy_id"])
            declaration = self.devices.get(check["device_id"])
            config = None if declaration is None else query_attempt_config(declaration)
            await self.coordinator._recover_attempt(context.database, run, check_id, request,
                entry.evidence, DeviceBinding(check["device_id"], check["driver_id"]), config)
        def waiting(owned):
            return tuple(owned.connection.execute(
                "SELECT r.id,c.id,c.action_id,c.trigger_copy_id,c.device_id,r.max_attempts_used,a.cancel_requested,a.status"
                " FROM operation_runs r JOIN device_availability_checks c ON c.id=r.availability_check_id"
                " JOIN actions a ON a.id=c.action_id WHERE r.status IN (1,2) AND r.id>?"
                " AND (a.cancel_requested=1 OR a.status IN (5,6)) ORDER BY r.id LIMIT 128",
                (self._finish_after,)).fetchall())
        rows = await context.database.read(waiting)
        self._finish_after = rows[-1][0] if len(rows) == 128 else 0
        if self._finish_after and context.wake is not None:
            from camctl.scheduling.notifications import WakeReason
            context.wake.mark_changed(WakeReason.ACTIVITY_FINISHED)
        from camctl.persistence.repositories.availability_wait_finish import (
            WaitingFinishDisposition, finish_waiting_availability,
        )
        for run_id, check_id, action_id, copy_id, device_id, saved_max, canceled, status in rows:
            if self.coordinator.pending(action_id):
                if canceled or status == 6:
                    self.coordinator.cancel_action(action_id)
                continue
            declaration = self.devices.get(device_id)
            maximum = query_attempt_config(declaration).max_attempts if declaration is not None else saved_max or 1
            now = context.clock.utc_micros()
            result = await self.coordinator._write(context.database, (action_id, copy_id, "finish-wait"),
                (check_id, now, now, maximum),
                lambda args, key, owned: finish_waiting_availability(*args, key, owned))
            if result is WaitingFinishDisposition.RESOLVED:
                from camctl.persistence.repositories.availability_release import finish_if_resolved
                await self.coordinator._write(context.database, (action_id, copy_id, "resolve"),
                    (check_id, now), lambda args, key, owned: finish_if_resolved(*args, key, owned))
            if result in (WaitingFinishDisposition.FINISHED, WaitingFinishDisposition.RESOLVED):
                self.coordinator._deadlines.pop(check_id, None)
                self.coordinator.retry_gate.cleared(f"availability/{check_id}")
                self.coordinator._notify()

    async def capture(self, runtime, action):
        if action["driver_id"] != DRIVER_ID or action["type"] != 1 or action["status"] != 1:
            return AvailabilityProgress.READY
        activity = await runtime.database.read(lambda owned: owned.connection.execute(
            "SELECT 1 FROM device_activities WHERE action_id=?", (action["id"],)).fetchone())
        if activity is not None:
            return AvailabilityProgress.READY
        from camctl.capture.preparation import effective_preparation_lead_s
        identity = runtime.fixed_camera_identity
        now = runtime.wall_us()
        request = CreateAvailabilityCheck(action["id"], identity, now,
            effective_preparation_lead_s(runtime, action), now)
        entry = self.drivers.entry(DRIVER_ID)
        return await self.coordinator.request(runtime.database, request, runtime.query_config,
            entry.driver, entry.evidence, DeviceBinding(action["device_id"], DRIVER_ID))

    async def finish_capture_binding(self, runtime, action, details):
        """沿原检查身份完成已开始的结果及绑定失败收场。"""
        from camctl.capture.preparation import effective_preparation_lead_s
        now = runtime.wall_us()
        identity = runtime.fixed_camera_identity
        if identity is None:
            from types import SimpleNamespace
            from camctl.operations.availability import _binding_check_pages
            async for check_id, _, _ in _binding_check_pages(
                    runtime.database, SimpleNamespace(action_id=action["id"], trigger_copy_id=None)):
                found = await runtime.database.read(lambda owned: row_facts(
                    owned.connection, "device_availability_checks", check_id))
                if found is None:
                    from camctl.contracts.values import ConsistencyError
                    raise ConsistencyError("开放检查缺少固定事实")
                identity = CameraIdentity(**found["expected_identity_json"])
                break
            else:
                return True
        request = CreateAvailabilityCheck(action["id"], identity, now,
            effective_preparation_lead_s(runtime, action), now)
        entry = self.drivers.entry(DRIVER_ID)
        current = None if details["reason"] not in ("physical_identity_mismatch", "physical_identity_conflict") else {
            "serial": identity.serial, "product_id": identity.product_id}
        return await self.coordinator.finish_binding(runtime.database, request, runtime.query_config,
            entry.evidence, DeviceBinding(action["device_id"], DRIVER_ID), details, current)

    async def read(self, runtime, candidate):
        if candidate.driver_id != DRIVER_ID:
            return AvailabilityProgress.READY
        declaration = self.devices.get(candidate.device_id)
        if declaration is None:
            return AvailabilityProgress.READY
        max_attempts = _device_attempts(declaration, "copy", 3, "max_read_attempts")
        if candidate.held:
            if candidate.retry_wait_required != 0:
                return AvailabilityProgress.READY
            from camctl.persistence.repositories.retry_wait_reads import held_read_ready_for_availability
            eligible = await runtime.database.read(lambda owned: held_read_ready_for_availability(
                owned.connection, candidate.copy_id, candidate.device_id, runtime.occurred_at(), max_attempts))
            if not eligible:
                return AvailabilityProgress.READY
        source = await runtime.database.read(lambda owned: row_facts(owned.connection, "device_files", candidate.source_file_id))
        fixed = camera_file_identity_from_source(source["identity_key"], source["locator_json"])
        if _hasselblad_identity(declaration) != CameraIdentity(fixed.serial, fixed.product_id):
            return AvailabilityProgress.READY
        now = runtime.occurred_at()
        request = CreateAvailabilityCheck(candidate.action_id, CameraIdentity(fixed.serial, fixed.product_id),
            now, self.preparation_leads.get(candidate.device_id, Decimal(0)), now,
            trigger_copy_id=candidate.copy_id, max_read_attempts=max_attempts)
        entry = self.drivers.entry(DRIVER_ID)
        return await self.coordinator.request(runtime.database, request, query_attempt_config(declaration),
            entry.driver, entry.evidence, fixed.binding)

    async def finish_read_binding(self, runtime, candidate, result):
        """读取项绑定失败前先收场其原可用性检查。"""
        from camctl.devices.bindings import binding_failure_details
        source = await runtime.database.read(lambda owned: row_facts(
            owned.connection, "device_files", candidate.source_file_id))
        if source is None:
            from camctl.contracts.values import ConsistencyError
            raise ConsistencyError("读取绑定失败缺少原设备来源")
        fixed = camera_file_identity_from_source(source["identity_key"], source["locator_json"])
        identity = CameraIdentity(fixed.serial, fixed.product_id)
        now = runtime.occurred_at()
        request = CreateAvailabilityCheck(candidate.action_id, identity, now,
            self.preparation_leads.get(candidate.device_id, Decimal(0)), now, trigger_copy_id=candidate.copy_id)
        declaration = self.devices.get(candidate.device_id)
        config = (query_attempt_config(declaration)
                  if declaration is not None and declaration.get("driver") == DRIVER_ID else None)
        entry = self.drivers.entry(DRIVER_ID)
        details = binding_failure_details(result)
        current = result.current_identity
        if details["reason"] == "physical_identity_conflict":
            from camctl.persistence.repositories.availability_wait_finish import saved_binding_check_failure
            saved = await runtime.database.read(lambda owned: saved_binding_check_failure(
                owned.connection, candidate.action_id, resource_key=identity.resource_key, expected_identity=identity))
            if saved is not None and saved[0] == details:
                current = saved[1]
            else:
                configured = _hasselblad_identity(declaration)
                current = {"serial": configured.serial, "product_id": configured.product_id}
        return await self.coordinator.finish_binding(runtime.database, request, config, entry.evidence,
            fixed.binding, details, current, has_read_copy=getattr(candidate, "has_read_copy", True))
