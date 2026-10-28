"""拍摄启动效果与本地终止共同使用的可靠事实。

动作进入 RUNNING 与设备启动分别判断。所有原启动尝试都可靠无
效果且没有待核实启动责任时，才能按未启动处理；调用错误本身不
提供这个依据。这里不执行设备操作，也不保存推测的设备观察。
"""

from contextlib import closing
from dataclasses import dataclass, replace
from typing import Any, Mapping

from camctl.contracts.enums import enum_for
from camctl.devices.start_entry import is_hasselblad_photo, entry_from_rows, EntryPhase
from camctl.contracts.values import ConsistencyError
from camctl.persistence.transaction import event_envelope, row_facts, update_change

_DISPATCH = enum_for("device_activities.dispatch_state")
_ATTEMPT = enum_for("operation_attempts.status")
_EFFECT = enum_for("operation_attempts.effect_state")
_RUN = enum_for("operation_runs.status")
_KIND = enum_for("operation_runs.kind")
_PURPOSE = enum_for("operation_runs.query_purpose")


def _require_photo_camera(camera, activity):
    from camctl.contracts.json_values import json_equal
    from camctl.devices.drivers.hasselblad.identity import CameraIdentity

    fixed = CameraIdentity(**activity["expected_identity_json"])
    if (not json_equal(camera, activity["expected_identity_json"])
            or activity["resource_key"] != fixed.resource_key):
        raise ValueError("原照片观察与活动固定机身不一致")


class PhotoResultReads:
    """从原事务连接核对设备双源与可靠 START 结果。"""

    def __init__(self, connection):
        self.connection = connection

    def read_device_start(self, action, activity_id, *, before_event_id):
        """从原连接和完整历史核对 DEVICE 单张 START 的真实结果。"""
        import hashlib
        from camctl.contracts.json_values import json_equal
        from camctl.devices.drivers.hasselblad.files import CameraFileLocator, CameraLocatorStatus
        from camctl.devices.drivers.hasselblad.responses import parse_device_capture, parse_devices, parse_serial
        from camctl.capture.photo_format import saved_photo_format
        from camctl.history.validators import validate_event_structure
        from camctl.operations.models import AttemptStatus, EffectState
        from camctl.operations.tool_diagnostics import ToolStreamState
        from camctl.operations.result_format import read_result_document
        from camctl.persistence.fact_reads import read_event

        with closing(self.connection.execute(
            "SELECT r.id,r.action_id,r.activity_id,r.kind,a.id FROM operation_runs r"
            " JOIN operation_attempts a ON a.run_id=r.id"
            " WHERE r.responsibility_key=? ORDER BY a.attempt_no DESC LIMIT 2",
            (f"start/{action['id']}",))) as cursor:
            rows = cursor.fetchall()
        if (len(rows) != 1 or rows[0][1] != action["id"] or rows[0][2] != activity_id
                or rows[0][3] != int(_KIND.START)):
            raise ConsistencyError("照片来源缺少唯一原 START 活动责任")
        attempt = row_facts(self.connection, "operation_attempts", rows[0][4])
        if attempt is None or attempt["result_event_id"] is None:
            raise ConsistencyError("照片来源的 START 实际结果尚未可靠保存")
        intent = read_event(self.connection, attempt["intent_event_id"])
        result = read_event(self.connection, attempt["result_event_id"])
        created = [row for row in intent.event.rows if row.table == "operation_attempts" and row.row_id == attempt["id"]]
        changed = [row for row in result.event.rows if row.table == "operation_attempts" and row.row_id == attempt["id"]]
        if (validate_event_structure(intent.event)[0] != "ATTEMPT_STARTED"
                or validate_event_structure(result.event)[:2] != ("ATTEMPT_RESULT", "SUCCEED")
                or intent.transaction.last_event_id >= result.transaction.first_event_id
                or result.transaction.last_event_id >= before_event_id
                or len(created) != 1 or created[0].before.exists or not created[0].after.exists
                or len(changed) != 1 or not changed[0].before.exists or not changed[0].after.exists):
            raise ConsistencyError("照片 START 原意图和结果事务不完整或时序不符")
        initial, change = created[0].after.values, changed[0]
        if (any(not json_equal(initial.get(name), attempt[name]) for name in ("run_id", "attempt_no", "intent_event_id"))
                or any(not json_equal(initial.get(name), value) for name, value in change.before.values.items())
                or any(not json_equal(({**initial, **change.after.values}).get(name), attempt[name]) for name in
                       ("status", "effect_state", "result_json", "error_json", "result_event_id"))):
            raise ConsistencyError("照片 START 投影与原完整历史不同")
        outcome = read_result_document(attempt["status"], attempt["effect_state"],
                                       attempt["result_json"], attempt["error_json"])
        photos = [item for item in outcome.observations if item.type == "photo_taken" and item.version == 2]
        if (outcome.status is not AttemptStatus.SUCCEEDED or outcome.effect is not EffectState.CONFIRMED
                or len(photos) != 1 or photos[0].data.get("activity_id") != str(activity_id)):
            raise ConsistencyError("照片 START 没有唯一可靠的 DEVICE 控制结果")
        data = photos[0].data
        if set(data) != {"activity_id", "camera_locator", "camera_identity", "original", "preview",
                         "original_error", "preview_error", "prepared_by", "steps"}:
            raise ConsistencyError("照片 START 观察成员不完整")
        activity = row_facts(self.connection, "device_activities", activity_id)
        _require_photo_camera(data["camera_identity"], activity)
        locator = CameraFileLocator.from_json(data["camera_locator"])
        if locator.status is not CameraLocatorStatus.PATH:
            raise ConsistencyError("照片 START 没有可靠机内定位")
        steps, diagnostics = data.get("steps"), outcome.diagnostics
        if (not isinstance(steps, list) or diagnostics is None
                or len(steps) != len(diagnostics.calls) or not steps
                or [step.get("name") for step in steps] != [call.step_name for call in diagnostics.calls]
                or steps[-1].get("name") != "capture"
                or sum(step.get("name") == "capture" for step in steps) != 1):
            raise ConsistencyError("照片 START 缺少完整实际身份与拍摄调用")
        def complete_calls(actual_steps, calls):
            for step, call in zip(actual_steps, calls):
                if ((step.get("name") in ("list", "info", "capture")
                        and (call.stdout.state is not ToolStreamState.COMPLETE
                             or call.stderr.state is not ToolStreamState.COMPLETE))
                        or step.get("local_exit_code") != 0 or step.get("local_signal") is not None
                        or step.get("transport_error") is not None or step.get("output_failure") is not None
                        or (call.stdout.data is not None and (
                            step.get("stdout_size_bytes") != len(call.stdout.data)
                            or step.get("stdout_sha256") != hashlib.sha256(call.stdout.data).hexdigest()))):
                    raise ConsistencyError("照片控制调用或双流未可靠完成")
        complete_calls(steps, diagnostics.calls)
        prepared = data.get("prepared_by")
        if prepared is None:
            if len(steps) < 3 or steps[0].get("name") != "list" or steps[1].get("name") != "info":
                raise ConsistencyError("未提前准备的照片 START 缺少原身份核对")
            identity_calls = diagnostics.calls[:2]
        else:
            if not isinstance(prepared, Mapping) or set(prepared) != {"run_id", "attempt_no"}:
                raise ConsistencyError("照片 START 的原准备引用无效")
            prepare_run = row_facts(self.connection, "operation_runs", prepared["run_id"])
            with closing(self.connection.execute(
                "SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                (prepared["run_id"], prepared["attempt_no"]))) as cursor:
                found = cursor.fetchone()
            prepare_attempt = None if found is None else row_facts(self.connection, "operation_attempts", found[0])
            if (prepare_run is None or prepare_attempt is None
                    or prepare_run["kind"] != int(_KIND.PREPARE_CAPTURE)
                    or prepare_run["action_id"] != action["id"] or prepare_run["activity_id"] != activity_id
                    or prepare_attempt["result_event_id"] is None):
                raise ConsistencyError("照片 START 缺少同一活动的原设置准备")
            preparation_event = read_event(self.connection, prepare_attempt["result_event_id"])
            preparation_intent = read_event(self.connection, prepare_attempt["intent_event_id"])
            changes = [row for row in preparation_event.event.rows
                       if row.table == "operation_attempts" and row.row_id == prepare_attempt["id"]]
            created_preparation = [row for row in preparation_intent.event.rows
                                   if row.table == "operation_attempts" and row.row_id == prepare_attempt["id"]]
            if (validate_event_structure(preparation_event.event)[:2] != ("ATTEMPT_RESULT", "SUCCEED")
                    or validate_event_structure(preparation_intent.event)[0] != "ATTEMPT_STARTED"
                    or preparation_intent.transaction.last_event_id >= preparation_event.transaction.first_event_id
                    or len(created_preparation) != 1 or created_preparation[0].before.exists
                    or not created_preparation[0].after.exists
                    or len(changes) != 1 or not changes[0].before.exists or not changes[0].after.exists
                    or preparation_event.transaction.last_event_id >= intent.transaction.first_event_id):
                raise ConsistencyError("照片设置准备未在原 START 意图之前可靠完成")
            prepared_initial = created_preparation[0].after.values
            prepared_change = changes[0]
            if (any(not json_equal(prepared_initial.get(name), prepare_attempt[name])
                    for name in ("run_id", "attempt_no", "intent_event_id"))
                    or any(not json_equal(prepared_initial.get(name), value)
                           for name, value in prepared_change.before.values.items())
                    or any(not json_equal(({**prepared_initial, **prepared_change.after.values}).get(name),
                                          prepare_attempt[name])
                           for name in ("status", "effect_state", "result_json", "error_json", "result_event_id"))):
                raise ConsistencyError("照片设置准备投影与原完整历史不同")
            prepared_outcome = read_result_document(prepare_attempt["status"], prepare_attempt["effect_state"],
                prepare_attempt["result_json"], prepare_attempt["error_json"])
            preparations = [item for item in prepared_outcome.observations
                            if item.type == "photo_preparation_steps" and item.version == 1]
            if (prepared_outcome.status is not AttemptStatus.SUCCEEDED
                    or prepared_outcome.effect is not EffectState.CONFIRMED or len(preparations) != 1
                    or preparations[0].data.get("activity_id") != str(activity_id)
                    or prepared_outcome.diagnostics is None):
                raise ConsistencyError("照片原设置准备没有可靠完成观察")
            preparation_steps = preparations[0].data.get("steps")
            preparation_calls = prepared_outcome.diagnostics.calls
            from camctl.devices.drivers.hasselblad.commands import PhotoPhase, photo_start_commands
            from camctl.devices.drivers.hasselblad.parameters import parse_photo_settings
            expected_names = [command.name.value for command in photo_start_commands(
                parse_photo_settings(action["effective_params_json"]), phase=PhotoPhase.PREPARATION)]
            if (not isinstance(preparation_steps, list) or len(preparation_steps) != len(preparation_calls)
                    or len(preparation_steps) < 2
                    or [step.get("name") for step in preparation_steps] != [call.step_name for call in preparation_calls]
                    or [step.get("name") for step in preparation_steps] != expected_names
                    or preparation_steps[0].get("name") != "list" or preparation_steps[1].get("name") != "info"):
                raise ConsistencyError("照片原设置准备缺少实际身份核对")
            complete_calls(preparation_steps, preparation_calls)
            identity_calls = preparation_calls[:2]
        identity = data["camera_identity"]
        cameras = [item for item in parse_devices(identity_calls[0].stdout.data)
                   if item.vendor_id == 0x2756 and item.product_id == identity["product_id"]]
        if len(cameras) != 1 or parse_serial(identity_calls[1].stdout.data) != identity["serial"]:
            raise ConsistencyError("照片 START 的实际机身与固定身份不符")
        capture = diagnostics.calls[-1]
        response = parse_device_capture(capture.stdout.data, capture.stderr.data, output_complete=True,
            capture_format=saved_photo_format(action), output_directory="/unused")
        if response.camera_locator != locator:
            raise ConsistencyError("照片 START 的实际机内定位与保存结果不符")
        return attempt["result_event_id"], data, locator

    def verify_device_page(self, action_id, activity_id, attempt_id, page):
        """从原连接核对角色页、原 START、固定机身和实际工具结果。"""
        import hashlib
        from pathlib import PurePosixPath
        from camctl.capture.files import file_identity_key
        from camctl.contracts.json_values import json_equal
        from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
        from camctl.devices.drivers.hasselblad.files import CameraFileIdentity, decide_photo_cache
        from camctl.devices.drivers.hasselblad.responses import parse_devices, parse_serial
        from camctl.capture.photo_format import saved_photo_format
        from camctl.host_files.photo_formats import CAPTURE_FORMATS
        from camctl.host_files.capture_sources import CapturedArtifact
        from camctl.operations.models import AttemptStatus, EffectState
        from camctl.operations.tool_diagnostics import ToolStreamState

        action = row_facts(self.connection, "actions", action_id)
        attempt = row_facts(self.connection, "operation_attempts", attempt_id)
        if (action is None or attempt is None or action["driver_id"] != DRIVER_ID
                or action["execution_spec_json"].get("output_storage") != "device"):
            raise ConsistencyError("照片角色页不属于原 DEVICE 动作")
        start_id, start, locator = self.read_device_start(
            action, activity_id, before_event_id=attempt["intent_event_id"])
        outcome = page.page.outcome
        observations = [item for item in outcome.observations if item.type == "result_files_listed" and item.version == 3]
        attempts = [item for item in outcome.observations if item.type == "photo_result_attempt" and item.version in (1, 2)]
        if (outcome.status is not AttemptStatus.SUCCEEDED or outcome.effect is not EffectState.CONFIRMED
                or outcome.error is not None or len(observations) != 1 or len(attempts) != 1
                or len(page.page.entries) != 1):
            raise ConsistencyError("照片角色页没有唯一可靠来源结果")
        data, detail = observations[0].data, attempts[0].data
        entry = page.page.entries[0]
        identity = CameraFileIdentity.from_source(
            file_identity_key(action["device_id"], action["driver_id"], entry.identity), entry.locator)
        camera = start["camera_identity"]
        if ((identity.serial, identity.product_id, identity.camera_path, identity.camera_name)
                != (camera["serial"], camera["product_id"], locator.camera_path, locator.camera_name)
                or detail.get("activity_id") != str(activity_id)
                or detail.get("role") != data.get("role") or identity.variant.value != data.get("role")):
            raise ConsistencyError("照片来源角色、原机身或机内定位不一致")
        steps = detail.get("steps")
        diagnostics = outcome.diagnostics
        if (not isinstance(steps, list) or len(steps) != 3 or diagnostics is None
                or diagnostics.call_limit != 3 or len(diagnostics.calls) != 3
                or [step.get("name") for step in steps] != ["list", "info", "download"]
                or [call.step_name for call in diagnostics.calls] != ["list", "info", "download"]):
            raise ConsistencyError("照片来源页缺少完整实际身份核对及下载步骤")
        for step, call in zip(steps, diagnostics.calls):
            if (call.stdout.state is not ToolStreamState.COMPLETE or call.stderr.state is not ToolStreamState.COMPLETE
                    or step.get("local_exit_code") != 0 or step.get("local_signal") is not None
                    or step.get("transport_error") is not None or step.get("output_failure") is not None
                    or step.get("stdout_size_bytes") != len(call.stdout.data)
                    or step.get("stdout_sha256") != hashlib.sha256(call.stdout.data).hexdigest()):
                raise ConsistencyError("照片来源页工具调用或双流未可靠完成")
        matching = [camera for camera in parse_devices(diagnostics.calls[0].stdout.data)
                    if camera.vendor_id == 0x2756 and camera.product_id == identity.product_id]
        if len(matching) != 1 or parse_serial(diagnostics.calls[1].stdout.data) != identity.serial:
            raise ConsistencyError("照片来源工具实际身份与原机身不符")
        args = steps[2].get("argv")
        if (steps[0].get("argv") != ["list"] or steps[1].get("argv") != ["info"]
                or not isinstance(args, list) or len(args) != 5
                or args[:4] != ["download", identity.variant.value, identity.camera_name, "--output"]
                or not isinstance(args[4], str)):
            raise ConsistencyError("照片来源下载参数不是原机身指定角色与文件")
        path = PurePosixPath(args[4])
        if (not path.is_absolute() or path.parent.name != "result-downloads"
                or path.name != f"{page.ref.ticket.run_id}-{page.ref.ticket.attempt_id}-{identity.variant.value}"):
            raise ConsistencyError("照片来源下载工作目录与原尝试不符")
        artifact = steps[2].get("artifact")
        try:
            observed_artifact = CapturedArtifact.from_json(artifact)
        except (TypeError, ValueError) as error:
            raise ConsistencyError("照片来源下载文件事实无效") from error
        description = CAPTURE_FORMATS[saved_photo_format(action)]
        expected_name = (identity.camera_name if identity.variant.value == "original"
                         else identity.camera_name + ".thumbnail" + description.preview_extension)
        expected_media = (description.original_media_type if identity.variant.value == "original"
                          else description.preview_media_type)
        if (observed_artifact.size_bytes != identity.size_bytes
                or observed_artifact.sha256 != identity.sha256
                or observed_artifact.media_type != entry.media_type
                or entry.original_name != expected_name or entry.media_type != expected_media
                or not json_equal(entry.locator, identity.locator())):
            raise ConsistencyError("照片来源完整字节事实与原角色结果不一致")
        if attempts[0].version == 2:
            comparison = detail.get("comparison")
            if not isinstance(comparison, dict) or set(comparison) != {"status", "selected", "sync_artifact", "sync_error"}:
                raise ConsistencyError("照片角色页缺少单次同步副本决定")
            role_name = "original" if identity.variant.value == "original" else "preview"
            candidate, candidate_error = start[role_name], start[role_name + "_error"]
            actual_sync = comparison["sync_artifact"]
            try:
                if actual_sync is not None:
                    actual_sync = CapturedArtifact.from_json(actual_sync)
                    if (candidate is None or candidate_error is not None
                            or actual_sync.original_name != candidate["host_name"]
                            or actual_sync.size_bytes != candidate["size"]
                            or actual_sync.media_type != candidate["media_type"]):
                        raise ValueError("同步副本内容与原 START 记录不符")
                    status = "complete"
                else:
                    status = comparison["status"]
                    if candidate is None:
                        if (status != "unconfirmed" or comparison["sync_error"] != candidate_error
                                or not isinstance(candidate_error, str) or not candidate_error):
                            raise ValueError("无可信记录不能授予同步副本资格")
                    elif status == "missing":
                        if comparison["sync_error"] is not None:
                            raise ValueError("可靠缺失不能携带读取错误")
                    elif status in ("invalid", "unconfirmed"):
                        if not isinstance(comparison["sync_error"], dict):
                            raise ValueError("同步副本失败须保存原错误")
                    else:
                        raise ValueError("同步副本分类无效")
                decision = decide_photo_cache(identity, status=status,
                    size_bytes=None if actual_sync is None else actual_sync.size_bytes,
                    sha256=None if actual_sync is None else actual_sync.sha256)
                if ((decision.comparison, decision.selected) != (comparison["status"], comparison["selected"])
                        or actual_sync is not None and comparison["sync_error"] is not None):
                    raise ValueError("同步副本决定与原事实不符")
            except (KeyError, TypeError, ValueError) as error:
                raise ConsistencyError("照片同步副本决定与原事实不符") from error
        if identity.variant.value == "original" and entry.paired_identity is not None:
            raise ConsistencyError("照片原图结果不能携带预览配对")
        if identity.variant.value == "thumbnail" and entry.paired_identity is not None:
            from camctl.persistence.fact_reads import read_event
            from camctl.contracts.enums import load_registry
            from camctl.history.events import load_event_registry
            from .result_pages import _first_complete_event
            original_key = file_identity_key(action["device_id"], action["driver_id"], entry.paired_identity)
            with closing(self.connection.execute(
                "SELECT id FROM device_files WHERE source_action_id=? AND identity_key=? LIMIT 2",
                (action_id, original_key))) as cursor:
                rows = cursor.fetchall()
            if len(rows) != 1:
                raise ConsistencyError("照片预览缺少唯一已登记原图")
            original = row_facts(self.connection, "device_files", rows[0][0])
            old = CameraFileIdentity.from_source(original["identity_key"], original["locator_json"])
            if (old.variant.value != "original" or original["role"] != int(enum_for("device_files.role").ORIGINAL)
                    or original["completion_state"] != int(enum_for("device_files.completion_state").COMPLETE)
                    or original["sha256"] != old.sha256 or original["size_bytes"] != old.size_bytes
                    or (old.serial, old.product_id, old.camera_path, old.camera_name)
                        != (identity.serial, identity.product_id, identity.camera_path, identity.camera_name)):
                raise ConsistencyError("照片预览配对不是同一机身的已完成原图")
            boundary = attempt["intent_event_id"] if page.ref.page_no == 1 else page.ref.event_id
            completion = _first_complete_event(self.connection, original)
            if read_event(self.connection, completion.event_id).transaction.last_event_id >= boundary:
                raise ConsistencyError("照片预览引用的原图尚未在原边界前完整提交")
            checksum_event = load_event_registry()["events"]["DEVICE_FILE_OBSERVED"]
            with closing(self.connection.execute(
                "SELECT e.id FROM entity_event_links l JOIN history_events e ON e.id=l.event_id,"
                " json_each(e.body_json,'$.rows') r WHERE l.entity_type=? AND l.entity_id=?"
                " AND e.event_type=? AND json_extract(e.body_json,'$.reason')=?"
                " AND json_extract(r.value,'$.table')='device_files' AND json_extract(r.value,'$.id')=?"
                " AND json_extract(r.value,'$.after.values.sha256')=? ORDER BY e.id LIMIT 1",
                (load_registry()["history_objects"]["device_file"]["id"], original["id"],
                 checksum_event["id"], checksum_event["branches"]["CHECKSUM"]["reason"],
                 original["id"], old.sha256))) as cursor:
                checksum = cursor.fetchone()
            if checksum is None or read_event(self.connection, checksum[0]).transaction.last_event_id >= boundary:
                raise ConsistencyError("照片预览引用的原图摘要尚未在原边界前提交")
        return start_id, identity

    def verify_device_file(self, file_id, observation, *, before_event_id, role=None, sha256=None,
                           check_sha=False, size_bytes=None, check_size=False):
        """从原 START 和结果页独立核对文件事实；旧照片保持原合同。"""
        from camctl.capture.files import file_identity_key
        from camctl.contracts.json_values import json_equal
        from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
        from camctl.operations.result_format import read_result_document
        from camctl.persistence.fact_reads import read_event
        from .result_pages import _at

        facts = row_facts(self.connection, "device_files", file_id)
        action = None if facts is None else row_facts(self.connection, "actions", facts["observer_action_id"])
        if (action is None or action["driver_id"] != DRIVER_ID
                or action["execution_spec_json"].get("output_storage") != "device"):
            return
        with closing(self.connection.execute(
            "SELECT r.activity_id,a.id FROM operation_runs r JOIN operation_attempts a ON a.run_id=r.id"
            " WHERE r.responsibility_key=? ORDER BY a.attempt_no DESC LIMIT 2", (f"start/{action['id']}",))) as cursor:
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise ConsistencyError("照片设备文件缺少唯一 START 责任")
        activity_id, start_attempt_id = rows[0]
        start_attempt = row_facts(self.connection, "operation_attempts", start_attempt_id)
        if start_attempt is None or start_attempt["result_json"] is None:
            raise ConsistencyError("照片设备文件缺少原 START 结果")
        outcome = read_result_document(start_attempt["status"], start_attempt["effect_state"],
            start_attempt["result_json"], start_attempt["error_json"])
        photos = [item for item in outcome.observations if item.type == "photo_taken"]
        if len(photos) == 1 and photos[0].version == 1:
            return
        if not isinstance(observation, Mapping) or not isinstance(observation.get("photo_source"), Mapping):
            raise ConsistencyError("照片设备文件缺少原 START 及角色页引用")
        reference = observation["photo_source"]
        if set(reference) != {"start_result_event_id", "result_page_event_id", "role"}:
            raise ConsistencyError("照片设备文件的来源引用不完整")
        page_event_id = reference["result_page_event_id"]
        stored = read_event(self.connection, page_event_id)
        if stored.transaction.last_event_id >= before_event_id:
            raise ConsistencyError("照片设备文件引用尚未完成的结果页事务")
        saved, _ = _at(self.connection, page_event_id)
        if (saved.ref.ticket.target_id != str(activity_id) or len(saved.page.entries) != 1
                or (saved.page.entries[0].identity, file_id) not in saved.file_ids):
            raise ConsistencyError("照片设备文件不属于原结果页")
        with closing(self.connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (saved.ref.ticket.run_id, saved.ref.ticket.attempt_id))) as cursor:
            attempt = cursor.fetchone()
        if attempt is None:
            raise ConsistencyError("照片结果页缺少原物理尝试")
        start_id, identity = self.verify_device_page(action["id"], activity_id, attempt[0], saved)
        expected = {**saved.page.entries[0].evidence, "photo_source": {
            "start_result_event_id": start_id, "result_page_event_id": page_event_id,
            "role": identity.variant.value}}
        if (not json_equal(observation, expected) or not json_equal(facts["locator_json"], identity.locator())
                or facts["identity_key"] != file_identity_key(action["device_id"], action["driver_id"], saved.page.entries[0].identity)
                or (role is not None and role != (int(enum_for("device_files.role").ORIGINAL)
                    if identity.variant.value == "original" else int(enum_for("device_files.role").PREVIEW)))
                or (check_size and size_bytes != identity.size_bytes)
                or (check_sha and sha256 != identity.sha256)):
            raise ConsistencyError("照片设备文件事实与原实际来源页不一致")

    def verify(self, action, sources, *, before_event_id):
        from camctl.contracts.json_values import json_equal, parse_exact_json
        from camctl.devices.drivers.hasselblad.files import CameraFileIdentity, CameraFileLocator, CameraLocatorStatus
        from camctl.history.validators import validate_event_structure
        from camctl.host_files.capture_sources import CapturedArtifact
        from camctl.operations.models import AttemptStatus, EffectState
        from camctl.operations.result_format import read_result_document
        from camctl.persistence.fact_reads import read_event

        with closing(self.connection.execute(
            "SELECT r.action_id,r.activity_id,r.kind,a.id,d.action_id FROM operation_runs r"
            " JOIN operation_attempts a ON a.run_id=r.id"
            " LEFT JOIN device_activities d ON d.id=r.activity_id"
            " WHERE r.responsibility_key=? ORDER BY a.attempt_no DESC LIMIT 1",
            (f"start/{action['id']}",))) as cursor:
            row = cursor.fetchone()
        try:
            if (row is None or row[0] != action["id"] or row[2] != int(_KIND.START)
                    or row[4] != action["id"]):
                raise ValueError("缺少属于原动作和活动的 START 结果")
            attempt = row_facts(self.connection, "operation_attempts", row[3])
            intent = read_event(self.connection, attempt["intent_event_id"])
            result = read_event(self.connection, attempt["result_event_id"])
            initial = [change for change in intent.event.rows
                       if change.table == "operation_attempts" and change.row_id == attempt["id"]]
            changes = [change for change in result.event.rows
                       if change.table == "operation_attempts" and change.row_id == attempt["id"]]
            if (validate_event_structure(intent.event)[0] != "ATTEMPT_STARTED"
                    or validate_event_structure(result.event)[:2] != ("ATTEMPT_RESULT", "SUCCEED")
                    or intent.transaction.last_event_id >= result.transaction.first_event_id
                    or result.transaction.last_event_id >= before_event_id
                    or len(initial) != 1 or initial[0].before.exists or not initial[0].after.exists
                    or len(changes) != 1 or not changes[0].before.exists or not changes[0].after.exists
                    or any(not json_equal(initial[0].after.values.get(name), attempt[name])
                           for name in ("run_id", "attempt_no", "intent_event_id"))
                    or any(not json_equal(initial[0].after.values.get(name), value)
                           for name, value in changes[0].before.values.items())):
                raise ValueError("START 意图和结果事件的身份或时序不符")
            saved = {**initial[0].after.values, **changes[0].after.values}
            if any(not json_equal(saved.get(name), attempt[name]) for name in
                   ("status", "effect_state", "result_json", "error_json", "result_event_id")):
                raise ValueError("START 结果投影与原事件不一致")
            outcome = read_result_document(attempt["status"], attempt["effect_state"],
                attempt["result_json"], attempt["error_json"])
            photos = [item for item in outcome.observations if item.type == "photo_taken"]
            if (outcome.status is not AttemptStatus.SUCCEEDED or outcome.effect is not EffectState.CONFIRMED
                    or len(photos) != 1 or photos[0].version not in (1, 2)
                    or photos[0].data.get("activity_id") != str(row[1])):
                raise ValueError("原 START 没有唯一可靠照片观察")
            data = photos[0].data
            if photos[0].version == 2:
                for source in sources.values():
                    completion = source.get("completion_evidence_json")
                    if isinstance(completion, str):
                        completion = parse_exact_json(completion)
                    if not isinstance(completion, Mapping):
                        raise ValueError("相机来源缺少原完成依据")
                    self.verify_device_file(source["id"], completion.get("observation"),
                        before_event_id=before_event_id, role=source["role"],
                        sha256=source["sha256"], check_sha=True)
                return
            steps = data.get("steps")
            if not isinstance(steps, list):
                raise ValueError("原照片观察缺少步骤")
            captures = [step for step in steps if isinstance(step, Mapping) and step.get("name") == "capture"]
            if len(captures) != 1:
                raise ValueError("原照片观察缺少唯一 capture 步骤")
            camera = captures[0].get("camera_identity")
            if (not isinstance(camera, Mapping) or set(camera) != {"serial", "product_id"}
                    or not isinstance(camera["serial"], str) or type(camera["product_id"]) is not int):
                raise ValueError("原照片观察缺少机身身份")
            activity = row_facts(self.connection, "device_activities", row[1])
            _require_photo_camera(camera, activity)
            locator = CameraFileLocator.from_json(captures[0].get("camera_locator"))
            if locator.status is not CameraLocatorStatus.PATH:
                raise ValueError("原照片观察缺少机内路径")
            kinds = enum_for("outputs.kind")
            for kind, role in ((int(kinds.ORIGINAL), "original"), (int(kinds.PREVIEW), "preview")):
                source = sources[kind]
                raw_locator = source["locator_json"]
                identity = CameraFileIdentity.from_source(source["identity_key"],
                    parse_exact_json(raw_locator) if isinstance(raw_locator, str) else raw_locator)
                artifact = CapturedArtifact.from_json(data[role])
                if ((identity.serial, identity.product_id, identity.camera_path, identity.camera_name)
                        != (camera["serial"], camera["product_id"], locator.camera_path, locator.camera_name)
                        or identity.size_bytes != artifact.size_bytes or identity.sha256 != artifact.sha256
                        or source["size_bytes"] != artifact.size_bytes or source["sha256"] != artifact.sha256):
                    raise ValueError("设备源与原照片观察不一致")
        except (KeyError, TypeError, ValueError) as error:
            raise ConsistencyError("设备源缺少一致的可靠拍摄结果") from error


@dataclass(frozen=True)
class StartFacts:
    activity: dict[str, Any] | None
    run: dict[str, Any] | None
    attempts_used: int
    not_started: bool
    dispatch_state: int | None
    preparation_resolved: bool = True


def load_start_facts(connection, action: Mapping[str, Any], *, result_events=()) -> StartFacts:
    """核对本动作活动、全部启动尝试及尚未结束的启动核实责任。"""
    # 复合 START 结果事务只用已经形成并由内核共同校验的事件派生
    # 后续本地终止资格，不写入数据库或把当前 RUNNING 误认为已结束。
    projected = {}
    for event in result_events:
        for change in event.rows:
            identity = change.table, change.row_id
            projected.setdefault(identity, {}).update(change.after.values)

    def after(table, row):
        return None if row is None else {**row, **projected.get((table, row["id"]), {})}

    with closing(connection.execute(
        "SELECT id FROM device_activities WHERE action_id = ?", (action["id"],),
    )) as cursor:
        found = cursor.fetchone()
    activity = after("device_activities", None if found is None else row_facts(
        connection, "device_activities", found[0]))
    if action["execution_started"] and activity is None:
        raise ConsistencyError(f"已执行拍摄缺少设备活动: {action['id']}")
    with closing(connection.execute(
        "SELECT id FROM operation_runs WHERE responsibility_key = ?",
        (f"start/{action['id']}",),
    )) as cursor:
        found = cursor.fetchone()
    run = after("operation_runs", None if found is None else row_facts(
        connection, "operation_runs", found[0]))
    count = used = unresolved = dispatched = 0
    if run is not None:
        if (activity is None or run["kind"] != int(_KIND.START)
                or run["action_id"] != action["id"]
                or run["activity_id"] != activity["id"]):
            raise ConsistencyError(f"启动责任与动作或活动不符: {action['id']}")
        with closing(connection.execute(
            "SELECT COUNT(*), COALESCE(MAX(attempt_no), 0),"
            " COALESCE(SUM(status = ? OR effect_state != ? OR result_event_id IS NULL), 0),"
            " COALESCE(SUM(json_extract(result_json, '$.settlement.basis') != 'not_dispatched'), 0)"
            " FROM operation_attempts WHERE run_id = ?",
            (int(_ATTEMPT.RUNNING), int(_EFFECT.NO_EFFECT), run["id"]),
        )) as cursor:
            count, used, unresolved, dispatched = cursor.fetchone()
        if count != used or used != run["attempts_used"]:
            raise ConsistencyError(f"启动累计次数与原尝试不符: {action['id']}")
        for (table, row_id), values in projected.items():
            if table != "operation_attempts":
                continue
            original = row_facts(connection, table, row_id)
            if original is None or original["run_id"] != run["id"]:
                continue
            final = {**original, **values}

            def unresolved_result(row):
                return (row["status"] == int(_ATTEMPT.RUNNING)
                        or row["effect_state"] != int(_EFFECT.NO_EFFECT)
                        or row["result_event_id"] is None)

            def dispatched_result(row):
                result = row["result_json"]
                return result is not None and result["settlement"]["basis"] != "not_dispatched"

            unresolved += int(unresolved_result(final)) - int(unresolved_result(original))
            dispatched += int(dispatched_result(final)) - int(dispatched_result(original))
    with closing(connection.execute(
        "SELECT 1 FROM operation_runs WHERE action_id = ? AND kind = ?"
        " AND query_purpose = ? AND status IN (?, ?) LIMIT 1",
        (action["id"], int(_KIND.QUERY_ACTIVITY), int(_PURPOSE.START_CONFIRMATION),
         int(_RUN.PENDING), int(_RUN.ACTIVE)),
    )) as cursor:
        pending_confirmation = cursor.fetchone() is not None
    attempts = ()
    if is_hasselblad_photo(action) and used:
        with closing(connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no", (run["id"],)
        )) as cursor:
            ids = [row[0] for row in cursor.fetchall()]
        attempts = [after("operation_attempts", row_facts(connection, "operation_attempts", identity)) for identity in ids]
    facts = start_facts_from_summary(action, activity, run,
        (count, used, unresolved, dispatched), pending_confirmation, attempts)
    return replace(facts, preparation_resolved=(activity is None or
                   preparation_calls_resolved(connection, activity["id"], result_events=result_events)))


def start_facts_from_summary(action, activity, run, statistics, pending_confirmation, entry_attempts=()):
    """SQL 聚合和完整原行共用的 START 分区；不推断会话许可。"""
    count, used, unresolved, dispatched = statistics
    if action["execution_started"] and activity is None:
        raise ConsistencyError(f"已执行拍摄缺少设备活动: {action['id']}")
    if run is not None and (activity is None or run["kind"] != int(_KIND.START)
            or run["action_id"] != action["id"] or run["activity_id"] != activity["id"]
            or count != used or used != run["attempts_used"]):
        raise ConsistencyError(f"启动责任或累计次数与原事实不符: {action['id']}")
    dispatch = None if activity is None else int(_DISPATCH(activity["dispatch_state"]))
    if activity is None:
        return StartFacts(None, run, used, run is None and not pending_confirmation, None)
    no_start_observation = activity["activity_state"] == 1 and activity["started_at"] is None
    if used == 0:
        not_started = dispatch == int(_DISPATCH.NOT_DISPATCHED) and no_start_observation
    else:
        not_started = (unresolved == 0 and no_start_observation
                       and dispatch != int(_DISPATCH.SUCCESS_RETURNED))
        if not_started and dispatch == int(_DISPATCH.MAY_HAVE_DISPATCHED):
            dispatch = int(_DISPATCH.NOT_DISPATCHED if dispatched == 0
                           else _DISPATCH.REJECTED_WITHOUT_EFFECT)
    if is_hasselblad_photo(action) and used:
        not_started = entry_from_rows(action, activity, entry_attempts) is EntryPhase.BLOCKED
    return StartFacts(activity, run, used, not_started and not pending_confirmation, dispatch)


def start_facts_from_rows(action, activities, runs, attempts_by_run, *, activity_targets=()):
    """自身活动范围与 run 引用目标分别输入；完整 run/attempt 生成有限统计。"""
    if len(activities) > 1 or any(row["action_id"] != action["id"] for row in activities):
        raise ConsistencyError("原拍摄活动归属或唯一性不符")
    if set(attempts_by_run) != {run["id"] for run in runs}:
        raise ConsistencyError("原操作与完整尝试范围不一致")
    targets = {}
    for activity in (*activities, *activity_targets):
        if activity["id"] in targets and targets[activity["id"]] != activity:
            raise ConsistencyError("原操作目标活动事实矛盾")
        targets[activity["id"]] = activity
    state = {"device_activities": targets,
             "operation_runs": {run["id"]: run for run in runs}}
    starts, pending_confirmation = [], False
    for run in runs:
        if run["action_id"] != action["id"] or run["id"] not in attempts_by_run:
            raise ConsistencyError("原操作责任缺少归属或完整尝试范围")
        _verify_start_scope_run(run, state)
        attempts = attempts_by_run[run["id"]]
        numbers = {attempt["attempt_no"] for attempt in attempts}
        if (any(attempt["run_id"] != run["id"] for attempt in attempts)
                or len(numbers) != len(attempts) or len(attempts) != run["attempts_used"]
                or len(attempts) != max(numbers, default=0)
                or (numbers and min(numbers) != 1)):
            raise ConsistencyError("原操作尝试的归属或连续累计次数不符")
        if run["responsibility_key"] == f"start/{action['id']}":
            starts.append(run)
        if (run["kind"] == int(_KIND.QUERY_ACTIVITY)
                and run["query_purpose"] == int(_PURPOSE.START_CONFIRMATION)
                and run["status"] in (int(_RUN.PENDING), int(_RUN.ACTIVE))):
            pending_confirmation = True
    if len(starts) > 1:
        raise ConsistencyError("原 START 责任不唯一")
    run = starts[0] if starts else None
    attempts = () if run is None else attempts_by_run[run["id"]]
    unresolved = sum(attempt["status"] == int(_ATTEMPT.RUNNING)
        or attempt["effect_state"] != int(_EFFECT.NO_EFFECT)
        or attempt["result_event_id"] is None for attempt in attempts)
    dispatched = sum(attempt["result_json"] is not None
        and attempt["result_json"]["settlement"]["basis"] != "not_dispatched" for attempt in attempts)
    facts = start_facts_from_summary(action, activities[0] if activities else None, run,
        (len(attempts), max((a["attempt_no"] for a in attempts), default=0), unresolved, dispatched),
        pending_confirmation, attempts)
    preparation_resolved = all(
        candidate["status"] not in (int(_RUN.PENDING), int(_RUN.ACTIVE))
        and all(attempt["status"] != int(_ATTEMPT.RUNNING) and attempt["result_event_id"] is not None
                for attempt in attempts_by_run[candidate["id"]])
        for candidate in runs if candidate["kind"] == int(_KIND.PREPARE_CAPTURE))
    return replace(facts, preparation_resolved=preparation_resolved)


def _verify_start_scope_run(run, state):
    """活动目标沿原操作职责校验；其他目标只核原固定组合与责任键。"""
    from camctl.persistence.repositories.operations import _verify_run_identity, _fixed_reference
    if run["kind"] == int(_KIND.EMERGENCY_STOP):
        activity = _fixed_reference(state, "device_activities", run["activity_id"])
        if activity["action_id"] != run["action_id"]:
            raise ConsistencyError("原应急补记与目标活动所属动作不符")
        return
    if run["activity_id"] is not None or (run["copy_id"] is None and run["cleanup_item_id"] is None and run["obtain_item_id"] is None):
        _verify_run_identity(run, state)
        return
    from camctl.operations.attempts import OperationKind, QueryPurpose, AttemptTarget, operation_responsibility_key
    try:
        kind = OperationKind[_KIND(run["kind"]).name]
        purpose = None if run["query_purpose"] is None else QueryPurpose[_PURPOSE(run["query_purpose"]).name]
        expected = operation_responsibility_key(kind, run["action_id"], AttemptTarget(
            activity_id=run["activity_id"], copy_id=run["copy_id"], cleanup_item_id=run["cleanup_item_id"], obtain_item_id=run["obtain_item_id"]), purpose, session_key=run.get("session_key"))
    except (KeyError, ValueError) as error:
        raise ConsistencyError("原操作固定身份组合无效") from error
    if run["responsibility_key"] != expected:
        raise ConsistencyError("原操作责任键与固定身份不符")


def release_basis_holds(facts: Mapping[str, Any]) -> bool:
    """活动结束、可靠未派发、无效果或适用完成依据允许解除占用。"""
    return (facts.get("activity_state") == 3
            or facts.get("dispatch_state") in (1, 4)
            or facts.get("completion_basis") == 3)


def output_scope_resolved(facts: Mapping[str, Any], recovery=None) -> bool:
    """基准已固定，且原集合结论或独立集合事实证明范围确定。"""
    ordinary = (facts.get("baseline_state") == 3
            and (facts.get("result_set_state") == 3
                 or facts.get("output_set_finalized_event_id") is not None))
    if ordinary:
        return True
    if not isinstance(recovery, Mapping):
        return False
    from camctl.capture.software_recovery import RecoveryPhase
    return (facts.get("baseline_state") == 3 and facts.get("activity_state") == 3
        and recovery.get("phase") == RecoveryPhase.READY.value
        and recovery.get("prepared") is True and recovery.get("scope_finalized") is True
        and recovery.get("cursor") is None
        and recovery.get("scope_pages", 0) > 0
        and recovery.get("scope_first_event_id") is not None and recovery.get("scope_last_event_id") is not None
        and recovery.get("boot_before") is not None and recovery.get("boot_after") is not None
        and recovery["boot_before"] != recovery["boot_after"]
        and recovery.get("baseline_first") == facts.get("baseline_first_event_id")
        and recovery.get("baseline_last") == facts.get("baseline_last_event_id"))


def occupancy_release_allowed(facts: Mapping[str, Any], *, unstarted: bool = False,
                              preparation_resolved: bool = False, preparation_calls_resolved: bool = True) -> bool:
    """释放依据与输出范围限制都满足，且占用尚未释放。"""
    return (preparation_calls_resolved and facts["occupancy_state"] == 1 and release_basis_holds(facts)
            and (facts["ownership_mode"] != 2 or (unstarted and preparation_resolved)
                 or output_scope_resolved(facts)))


def start_finish_event(run: Mapping[str, Any] | None, status: int, occurred_at: int):
    """构造适用 START 结束事件；不存在或已终态时不创建、不改写。"""
    if run is None or run["status"] not in (int(_RUN.PENDING), int(_RUN.ACTIVE)):
        return None
    if status not in (int(_RUN.CANCELED), int(_RUN.EXPIRED)):
        raise ValueError("本地启动收场只接受取消或过期")
    return event_envelope(0, 0, 10, 3, (update_change(
        "operation_runs", run["id"],
        {"status": run["status"], "retry_wait_required": run["retry_wait_required"],
         "error_json": run["error_json"]},
        {"status": status, "retry_wait_required": 0, "error_json": None}),), occurred_at)


def unstarted_events(facts: StartFacts, occurred_at: int, *,
                     run_status: int | None, release: bool = True,
                     preparation_resolved: bool = False):
    """把已证明无启动效果的派发结论、责任结束和适用释放组合成事件。"""
    if not facts.not_started:
        raise ConsistencyError("本地终止缺少可靠未启动依据")
    events = []
    activity = facts.activity
    if activity is not None and activity["dispatch_state"] != facts.dispatch_state:
        events.append(event_envelope(0, 0, 13, 2, (update_change(
            "device_activities", activity["id"],
            {"dispatch_state": activity["dispatch_state"]},
            {"dispatch_state": facts.dispatch_state}),), occurred_at))
    if run_status is not None:
        finish = start_finish_event(facts.run, run_status, occurred_at)
        if finish is not None:
            events.append(finish)
    if activity is not None and release and occupancy_release_allowed(
            {**activity, "dispatch_state": facts.dispatch_state}, unstarted=True,
            preparation_resolved=(preparation_resolved or activity["baseline_state"] == 3),
            preparation_calls_resolved=facts.preparation_resolved):
        events.append(event_envelope(0, 0, 13, 3, (update_change(
            "device_activities", activity["id"],
            {"occupancy_state": 1}, {"occupancy_state": 2}),), occurred_at,
            evidence={"preparation_resolved": True} if preparation_resolved else {}))
    return events


def include_start_facts(facts: StartFacts, state, owners) -> None:
    """将本地终止涉及的原行和历史归属加入同一事务上下文。"""
    for table, row in (("device_activities", facts.activity), ("operation_runs", facts.run)):
        if row is not None:
            state.setdefault(table, {})[row["id"]] = row
            owners[(table, row["id"])] = ("action", row["action_id"])


def verify_unstarted_final(connection, action, saved=(), *, released=True,
                           preparation_error=None) -> None:
    """核实本地终止的当前可靠结果与新增事件的身份、字段和终态。"""
    facts = load_start_facts(connection, action)
    if not facts.not_started:
        raise ConsistencyError("本地终止的原未启动依据不可靠")
    if facts.run is not None and facts.run["status"] in (int(_RUN.PENDING), int(_RUN.ACTIVE)):
        raise ConsistencyError("本地终止仍有普通启动责任")
    preparation_resolved = (preparation_error is not None or any(
        event["body"]["evidence"].get("preparation_resolved") is True for event in saved))
    if released and facts.activity is not None and occupancy_release_allowed(facts.activity, unstarted=True,
            preparation_resolved=(preparation_resolved or facts.activity["baseline_state"] == 3),
            preparation_calls_resolved=facts.preparation_resolved):
        raise ConsistencyError("本地终止缺少适用的占用释放")
    for event in saved:
        kind = event["type"], event["reason"]
        if kind not in ((13, 2), (13, 3), (10, 3)):
            continue
        rows = event["body"]["rows"]
        if len(rows) != 1:
            raise ConsistencyError("本地终止的伴随事件行数不符")
        row = rows[0]
        original = facts.run if kind == (10, 3) else facts.activity
        table = "operation_runs" if kind == (10, 3) else "device_activities"
        columns = ({"status", "retry_wait_required", "error_json"} if kind == (10, 3)
                   else {"last_error_json"} if kind == (13, 2) and preparation_error is not None
                   else {"dispatch_state"} if kind == (13, 2) else {"occupancy_state"})
        if (original is None or row["table"] != table or row["id"] != original["id"]
                or set(row["after"]["values"]) - columns
                or (kind == (10, 3) and "status" not in row["after"]["values"])
                or (kind != (10, 3) and set(row["after"]["values"]) != columns)):
            raise ConsistencyError("本地终止的伴随事实与原责任不符")
        if any(original[column] != value for column, value in row["after"]["values"].items()):
            raise ConsistencyError("本地终止的伴随结果与保存事实不符")


def preparation_calls_resolved(connection, activity_id: int, *, result_events=()) -> bool:
    """从原连接核对全部设置责任和尝试，保留读取错误。"""
    if result_events:
        return _preparation_resolved_after_events(connection, activity_id, result_events)
    with closing(connection.execute(
        "SELECT 1 FROM operation_runs r WHERE r.activity_id=? AND r.kind=?"
        " AND (r.status IN (?,?) OR EXISTS(SELECT 1 FROM operation_attempts t"
        " WHERE t.run_id=r.id AND (t.status=? OR t.result_event_id IS NULL))) LIMIT 1",
        (activity_id, int(_KIND.PREPARE_CAPTURE), int(_RUN.PENDING), int(_RUN.ACTIVE),
         int(_ATTEMPT.RUNNING)),
    )) as cursor:
        return cursor.fetchone() is None


def _preparation_resolved_after_events(connection, activity_id, events):
    """只叠加已验证前序事件；未验证的未来提案不提供释放依据。"""
    projected = {}
    for event in events:
        for row in event.rows:
            if row.table not in ("operation_runs", "operation_attempts"):
                continue
            identity = (row.table, row.row_id)
            if identity not in projected:
                original = row_facts(connection, *identity)
                projected[identity] = {} if original is None else original
            projected[identity] = {**projected[identity], **row.after.values, "id": row.row_id}
    run_ids = {identity for (table, identity) in projected if table == "operation_runs"}
    run_ids.update(row["run_id"] for (table, _), row in projected.items() if table == "operation_attempts")
    relevant = {}
    for identity in run_ids:
        run = projected.get(("operation_runs", identity)) or row_facts(connection, "operation_runs", identity)
        if run is None:
            raise ConsistencyError("原设置尝试缺少所属流程")
        if run["kind"] == int(_KIND.PREPARE_CAPTURE) and run["activity_id"] == activity_id:
            relevant[identity] = run
    exclusion = "" if not relevant else " AND r.id NOT IN (" + ",".join("?" for _ in relevant) + ")"
    with closing(connection.execute(
        "SELECT 1 FROM operation_runs r WHERE r.activity_id=? AND r.kind=?" + exclusion
        + " AND (r.status IN (?,?) OR EXISTS(SELECT 1 FROM operation_attempts t WHERE t.run_id=r.id"
          " AND (t.status=? OR t.result_event_id IS NULL))) LIMIT 1",
        (activity_id, int(_KIND.PREPARE_CAPTURE), *relevant, int(_RUN.PENDING), int(_RUN.ACTIVE), int(_ATTEMPT.RUNNING)),
    )) as cursor:
        if cursor.fetchone() is not None:
            return False
    for identity, run in relevant.items():
        if run["status"] in (int(_RUN.PENDING), int(_RUN.ACTIVE)):
            return False
        attempts = {row_id: row for (table, row_id), row in projected.items()
                    if table == "operation_attempts" and row["run_id"] == identity}
        if any(row["status"] == int(_ATTEMPT.RUNNING) or row.get("result_event_id") is None for row in attempts.values()):
            return False
        excluded = "" if not attempts else " AND id NOT IN (" + ",".join("?" for _ in attempts) + ")"
        with closing(connection.execute("SELECT 1 FROM operation_attempts WHERE run_id=?" + excluded
                + " AND (status=? OR result_event_id IS NULL) LIMIT 1",
                (identity, *attempts, int(_ATTEMPT.RUNNING)))) as cursor:
            if cursor.fetchone() is not None:
                return False
    return True
