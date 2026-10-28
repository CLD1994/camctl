"""单次 START 的会话入口许可及随原结果保存的入口事实。"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.devices.evidence import DeviceObservation, EvidenceContract
from camctl.operations.models import AttemptTicket

ENTERED = EvidenceContract("hasselblad_start_entered", 1, "control",
                          frozenset({"activity_id"}), identity_field="activity_id")
PREVENTED = EvidenceContract("start_call_prevented", 1, "control", frozenset())


class EntryPhase(Enum):
    READY = "ready"
    GRANTING = "granting"
    RESERVED = "reserved"
    ENTERED = "entered"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


def is_hasselblad_photo(action):
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    return (isinstance(action, Mapping) and action.get("type") == 1
            and action.get("driver_id") == DRIVER_ID
            and isinstance(action.get("execution_spec_json"), Mapping)
            and action["execution_spec_json"].get("output_storage") in ("host", "device"))


@dataclass
class StartEntryPermit:
    database_id: str
    action_id: int
    ticket: AttemptTicket | None = None
    phase: EntryPhase = EntryPhase.READY
    active: bool = False
    cancel_pending: bool = False

    def bind(self, database_id, ticket):
        if (database_id != self.database_id or self.phase not in (EntryPhase.READY, EntryPhase.GRANTING, EntryPhase.UNKNOWN)
                or self.ticket is not None or not isinstance(ticket, AttemptTicket)
                or ticket.operation != "control" or ticket.responsibility_key != f"start/{self.action_id}"):
            raise ConsistencyError("START 入口许可不能改绑或重新开放")
        self.ticket = ticket
        self.phase = EntryPhase.READY

    def enter(self, ticket):
        if self.ticket is None or self.ticket != ticket or self.phase is not EntryPhase.READY:
            raise ConsistencyError("START 入口没有匹配的原许可")
        self.phase = EntryPhase.ENTERED
        self.active = True


class StartEntryCoordinator:
    """仅由同一事件循环同步访问；不跨真实工具等待持锁。"""

    def __init__(self):
        self.permits = {}
        self.pending_cancels = {}
        self.pending_grants = {}

    def prepare(self, database_id, action_id):
        key = database_id, action_id
        if key in self.permits:
            raise ConsistencyError("原 START 入口许可仍承担责任")
        permit = StartEntryPermit(database_id, action_id)
        self.permits[key] = permit
        return permit

    def get(self, database_id, action_id):
        return self.permits.get((database_id, action_id))

    def reserve(self, database_id, action_id, *, no_intent):
        permit = self.get(database_id, action_id)
        if permit is None and no_intent:
            permit = self.prepare(database_id, action_id)
        if permit is not None and permit.phase in (EntryPhase.GRANTING, EntryPhase.UNKNOWN):
            permit.cancel_pending = True
            return None
        if permit is None or permit.phase is not EntryPhase.READY:
            return None
        permit.phase = EntryPhase.RESERVED
        return permit


def entered_observation(ticket):
    return DeviceObservation(ENTERED.type, ENTERED.version, {"activity_id": ticket.target_id})


def saved_entry(result, activity_id):
    """只解释原结构化入口；没有依据为未知，不从采集效果补造。"""
    if result is None:
        return EntryPhase.UNKNOWN
    entries = [item for item in result["observations"] if item["type"] == ENTERED.type]
    prevented = result["settlement"]["evidence"]["type"] == PREVENTED.type
    if entries:
        if (len(entries) != 1 or entries[0] != {"type": ENTERED.type, "version": 1,
                "data": {"activity_id": str(activity_id)}} or prevented):
            raise ConsistencyError("原 START 入口观察重复、身份不符或矛盾")
        return EntryPhase.ENTERED
    if prevented:
        if (result["settlement"] != {"basis": "not_dispatched", "evidence": {
                "type": PREVENTED.type, "version": 1, "data": {}}}
                or result["observations"]):
            raise ConsistencyError("原 START 未进入依据不完整")
        return EntryPhase.BLOCKED
    return EntryPhase.UNKNOWN


@dataclass(frozen=True)
class EntryBlock:
    database_id: str
    action_id: int
    ticket: AttemptTicket | None

    def __post_init__(self):
        ObjectId(self.action_id)
        if not isinstance(self.database_id, str) or not self.database_id:
            raise ValueError("入口阻止缺少原状态库身份")
        if self.ticket is not None and (not isinstance(self.ticket, AttemptTicket)
                or self.ticket.operation != "control"
                or self.ticket.responsibility_key != f"start/{self.action_id}"):
            raise ValueError("入口阻止缺少匹配的原 START 票据")


def entry_from_rows(action, activity, attempts):
    if not attempts:
        return EntryPhase.BLOCKED if activity is None or (
            activity['dispatch_state'] == 1 and activity['activity_state'] == 1
            and activity['started_at'] is None) else EntryPhase.UNKNOWN
    from camctl.contracts.enums import enum_for
    phases = []
    for row in attempts:
        phase = saved_entry(row['result_json'], activity['id'])
        if phase is not EntryPhase.UNKNOWN and row['result_event_id'] is None:
            raise ConsistencyError('START 入口事实缺少原结果事件')
        if phase is EntryPhase.BLOCKED and (row['status'] != int(enum_for('operation_attempts.status').FAILED)
                or row['effect_state'] != int(enum_for('operation_attempts.effect_state').NO_EFFECT)):
            raise ConsistencyError('START 未进入结果状态或采集效果矛盾')
        phases.append(phase)
    if EntryPhase.ENTERED in phases:
        return EntryPhase.ENTERED
    return EntryPhase.BLOCKED if all(value is EntryPhase.BLOCKED for value in phases) else EntryPhase.UNKNOWN
