"""拍摄与 DEVICE 读取共用的设备检查装配。"""

from decimal import Decimal

from camctl.bootstrap.capture_assembly import _hasselblad_identity, query_attempt_config
from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.files import CameraFileIdentity
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
            await self.coordinator._recover_attempt(context.database, run, check_id, request,
                entry.evidence, DeviceBinding(check["device_id"], check["driver_id"]))
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

    async def read(self, runtime, candidate):
        if candidate.driver_id != DRIVER_ID:
            return AvailabilityProgress.READY
        if candidate.held:
            if candidate.attempts_used == 0 or candidate.retry_wait_required != 0:
                return AvailabilityProgress.READY
            from camctl.persistence.repositories.retry_wait_reads import held_read_ready_for_availability
            eligible = await runtime.database.read(lambda owned: held_read_ready_for_availability(
                owned.connection, candidate.copy_id, candidate.device_id, runtime.occurred_at()))
            if not eligible:
                return AvailabilityProgress.READY
        source = await runtime.database.read(lambda owned: row_facts(owned.connection, "device_files", candidate.source_file_id))
        fixed = CameraFileIdentity.from_source(source["identity_key"], source["locator_json"])
        declaration = self.devices.get(candidate.device_id)
        if declaration is None or _hasselblad_identity(declaration) != CameraIdentity(fixed.serial, fixed.product_id):
            return AvailabilityProgress.READY
        now = runtime.occurred_at()
        request = CreateAvailabilityCheck(candidate.action_id, CameraIdentity(fixed.serial, fixed.product_id),
            now, self.preparation_leads.get(candidate.device_id, Decimal(0)), now,
            trigger_copy_id=candidate.copy_id)
        entry = self.drivers.entry(DRIVER_ID)
        return await self.coordinator.request(runtime.database, request, query_attempt_config(declaration),
            entry.driver, entry.evidence, fixed.binding)
