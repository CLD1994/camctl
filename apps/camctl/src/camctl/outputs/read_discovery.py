"""读取责任的有界发现；继续位置只保存值，资格由机会事务复核。"""

from contextlib import closing
from dataclasses import dataclass

from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError, MAX_OBJECT_ID, ObjectId, UtcMicros
from camctl.contracts.enums import enum_for
from camctl.devices.capture_preparation import CAPTURE_ACTION_TYPES
from camctl.outputs.database_reads import read_all

_INSPECTION_STATE = enum_for("segment_inspections.state")
_INSPECTION_PENDING = int(_INSPECTION_STATE.PENDING)


DEFAULT_BATCH_SIZE = 100


def positive_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("发现批量上限必须是非布尔正整数")
    return limit


@dataclass(frozen=True)
class ReadDiscoveryScope:
    database: str
    kind: str
    now_us: int
    generation: int
    device_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.database, str) or not self.database:
            raise ValueError("发现范围必须指定数据库")
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("发现范围必须指定查询种类")
        UtcMicros(self.now_us)
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 0:
            raise ValueError("发现代次必须是非布尔非负整数")
        if self.device_id is not None and (not isinstance(self.device_id, str) or not self.device_id):
            raise ValueError("设备范围必须使用非空设备身份")


@dataclass(frozen=True)
class ReadDiscoveryCursor:
    scope: ReadDiscoveryScope
    after: tuple

    def __post_init__(self):
        if not isinstance(self.scope, ReadDiscoveryScope) or not isinstance(self.after, tuple) or not self.after:
            raise ValueError("继续位置必须保存范围和稳定排序键")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in self.after):
            raise ValueError("继续位置的排序键必须是整数")


def check_cursor(scope, cursor):
    if cursor is not None and cursor.scope != scope:
        raise ValueError("继续位置不属于本次数据库、范围、时刻或代次")


def database_identity(connection):
    with closing(connection.execute("PRAGMA database_list")) as cursor:
        database = next(row[2] for row in cursor.fetchall() if row[1] == "main")
    return database or f"memory:{id(connection)}"


# 普通取回与录像内部输入使用同一归属及资格定义。
_READ_FROM = (
    " FROM file_copies fc"
    " LEFT JOIN device_files df ON df.id=fc.source_device_file_id"
    " LEFT JOIN intermediate_files hf ON hf.id=fc.source_intermediate_file_id"
    " LEFT JOIN actions ob ON ob.id=df.observer_action_id"
    " LEFT JOIN actions origin ON origin.id=df.source_action_id"
    " LEFT JOIN deliveries d ON d.id=fc.delivery_id"
    " LEFT JOIN recording_processing rp ON rp.id=fc.processing_id"
    " LEFT JOIN segment_inspections si ON si.id=fc.inspection_id"
    " LEFT JOIN actions a ON a.id=COALESCE(d.action_id,rp.action_id,si.action_id)"
    " LEFT JOIN plans p ON p.id=a.plan_id"
    " LEFT JOIN operation_runs r ON r.responsibility_key='read/'||fc.id"
)
_ACTIVE_COPY = (
    " (fc.slot_device_id IS NOT NULL OR (fc.verification_state NOT IN (3,5)"
    " AND (d.status IN (1,2) OR rp.id IS NOT NULL OR (d.id IS NULL AND rp.id IS NULL"
    f" AND si.state={_INSPECTION_PENDING}))))"
)
_ELIGIBLE = (
    " a.status=2 AND a.cancel_requested=0 AND a.scheduled_at<=?"
    " AND r.status IN (1,2) AND r.retry_wait_required=0"
    " AND df.completion_state=3 AND fc.slot_device_id IS NULL"
    " AND fc.verification_state NOT IN (3,5)"
    " AND (fc.delivery_id IS NULL OR d.status IN (1,2))"
)
_READ_ORDER = "a.scheduled_at,a.plan_id,a.input_index,fc.id"
_READ_COPY_ELIGIBLE = (
    "fc.verification_state NOT IN (3,5)"
    " AND (fc.delivery_id IS NULL OR d.status IN (1,2))"
    f" AND (fc.inspection_id IS NULL OR si.state={_INSPECTION_PENDING})"
)


def read_copy_dispatch_eligible(connection, copy_id):
    """实际开源前复核原副本仍属于有限发现允许推进的输入分区。"""
    with closing(connection.execute(
        "SELECT 1" + _READ_FROM + " WHERE fc.id=? AND " + _READ_COPY_ELIGIBLE + " LIMIT 1",
        (copy_id,),
    )) as cursor:
        return cursor.fetchone() is not None


def validate_read_basis(connection, device_id=None):
    """错误探测与候选筛选分开；坏关联不能被 JOIN 或最优项截断隐藏。"""
    condition = ""
    parameters = ()
    if device_id is not None:
        condition = " AND fc.source_device_file_id IS NOT NULL AND (ob.device_id=? OR ob.id IS NULL OR df.id IS NULL)"
        parameters = (device_id,)
    with closing(connection.execute(
        "SELECT fc.id" + _READ_FROM + " WHERE "
        + _ACTIVE_COPY + condition + " AND ((fc.source_device_file_id IS NOT NULL AND ("
        "df.id IS NULL OR ob.id IS NULL OR origin.id IS NULL"
        " OR ob.device_id IS NULL OR ob.driver_id IS NULL OR ob.device_id='' OR ob.driver_id=''"
        " OR ob.device_id IS NOT origin.device_id OR ob.driver_id IS NOT origin.driver_id"
        " OR (fc.slot_device_id IS NOT NULL AND fc.slot_device_id IS NOT ob.device_id)))"
        " OR (fc.source_device_file_id IS NULL AND (hf.id IS NULL OR fc.slot_device_id IS NOT NULL))"
        " OR (fc.inspection_id IS NOT NULL AND (si.id IS NULL OR si.action_id IS NOT a.id"
        f" OR si.state!={_INSPECTION_PENDING} OR si.source_device_file_id IS NOT df.id))"
        " OR (fc.inspection_id IS NULL AND fc.processing_id IS NULL AND fc.delivery_id IS NULL)"
        " OR a.id IS NULL OR p.id IS NULL OR a.scheduled_at IS NULL"
        " OR a.input_index IS NULL OR r.id IS NULL OR r.kind<>3 OR r.copy_id IS NOT fc.id"
        " OR r.action_id IS NOT a.id OR r.delivery_id IS NOT fc.delivery_id"
        " OR (SELECT COUNT(*) FROM operation_runs rr WHERE rr.copy_id=fc.id AND rr.kind=3)<>1) LIMIT 1",
        parameters,
    )) as cursor:
        bad = cursor.fetchone()
    if bad is not None:
        raise ConsistencyError(f"读取候选缺少可靠关联、流程或排序依据: copy={bad[0]}")


def device_holder(connection, device_id):
    with closing(connection.execute(
        "SELECT id FROM file_copies WHERE slot_device_id=? ORDER BY id LIMIT 2", (device_id,),
    )) as cursor:
        rows = cursor.fetchall()
    if len(rows) > 1:
        raise ConsistencyError("同一设备的读取机会存在多个持有者")
    return rows[0][0] if rows else None


def first_eligible_read(connection, device_id, now_us, *, retry_wait_copy_id=None):
    validate_read_basis(connection, device_id)
    eligibility = _ELIGIBLE
    parameters = [device_id, now_us]
    if retry_wait_copy_id is not None:
        ObjectId(retry_wait_copy_id)
        # A caller may include only the one persisted local-to-device
        # fallback whose retry interval it has already observed as elapsed.
        # All other retry-wait rows remain ineligible; the ordinary ordering
        # still decides whether this candidate may acquire the device slot.
        eligibility = eligibility.replace(
            " AND r.retry_wait_required=0",
            " AND (r.retry_wait_required=0 OR fc.id=?)",
        )
        parameters.append(retry_wait_copy_id)
    with closing(connection.execute(
        "SELECT fc.id" + _READ_FROM + " WHERE ob.device_id=? AND " + eligibility
        + " ORDER BY " + _READ_ORDER + " LIMIT 1", parameters,
    )) as cursor:
        row = cursor.fetchone()
    return None if row is None else row[0]


@dataclass(frozen=True)
class ReadCandidate:
    copy_id: int
    action_id: int
    scheduled_at: int
    plan_id: int
    input_index: int
    device_id: str | None
    driver_id: str | None
    source_file_id: int | None
    held: bool
    attempts_used: int = 0
    retry_wait_required: int = 0

    def __post_init__(self):
        for identity in (self.copy_id, self.action_id, self.plan_id):
            ObjectId(identity)
        UtcMicros(self.scheduled_at)
        if isinstance(self.input_index, bool) or not isinstance(self.input_index, int) or self.input_index < 0:
            raise ConsistencyError("读取候选缺少可靠的计划内顺序")
        object.__setattr__(self, "held", bool(self.held))

    @property
    def key(self):
        return (0 if self.held else 1, self.scheduled_at, self.plan_id, self.input_index, self.copy_id)


def read_candidates(connection, scope, cursor=None, *, limit):
    positive_limit(limit)
    check_cursor(scope, cursor)
    if scope.database != database_identity(connection):
        raise ValueError("发现范围不属于当前数据库")
    if cursor is not None and len(cursor.after) != 5:
        raise ValueError("读取继续位置缺少完整排序键")
    if scope.kind not in ("delivery", "reads", "internal", "all", "local"):
        raise ValueError("读取候选种类无效")
    validate_read_basis(connection, scope.device_id)
    condition = "a.status=2 AND a.cancel_requested=0 AND " + _READ_COPY_ELIGIBLE
    parameters = []
    if scope.kind == "local":
        condition += " AND fc.source_device_file_id IS NULL AND d.status IN (1,2)"
    else:
        condition += " AND fc.source_device_file_id IS NOT NULL"
        condition += " AND (fc.delivery_id IS NULL OR d.status IN (1,2))"
        if scope.kind in ("delivery", "reads"):
            condition += " AND fc.delivery_id IS NOT NULL"
        elif scope.kind == "internal":
            condition += (" AND (fc.processing_id IS NOT NULL OR "
                          f"(fc.inspection_id IS NOT NULL AND si.state={_INSPECTION_PENDING}))")
    if scope.device_id is not None:
        condition += " AND ob.device_id=?"
        parameters.append(scope.device_id)
    priority = "CASE WHEN fc.slot_device_id IS NOT NULL THEN 0 ELSE 1 END"
    key_sql = f"{priority},a.scheduled_at,a.plan_id,a.input_index,fc.id"
    if cursor is not None:
        condition += f" AND ({key_sql})>(?,?,?,?,?)"
        parameters.extend(cursor.after)
    parameters.append(limit)
    with closing(connection.execute(
        "SELECT fc.id,a.id,a.scheduled_at,a.plan_id,a.input_index,ob.device_id,ob.driver_id,"
        "fc.source_device_file_id,fc.slot_device_id IS NOT NULL,r.attempts_used,r.retry_wait_required" + _READ_FROM
        + " WHERE " + condition + " ORDER BY " + key_sql + " LIMIT ?", parameters,
    )) as result:
        items = tuple(ReadCandidate(*row) for row in result.fetchall())
    position = ReadDiscoveryCursor(scope, items[-1].key) if len(items) == limit else None
    return Page(items, position)


class ReadDiscoveryOwner:
    """会话内的有限继续位置；不持有候选详情、连接或查询游标。

    每种查询只有一个位置。历史尾变化或相关计划时间到达时重查；
    查询结束后下一轮重新观察仍未完成的责任，不把查空当业务结束。
    """

    _KINDS = frozenset(
        (
            "start",
            "actions",
            "selections",
            "items",
            "finish",
            "reads",
            "local",
            "failed",
            "capture",
            "residual",
            "software_recovery",
            "attempts",
            "queries",
            "segment_queries",
        )
    )

    def __init__(self, batch_size=DEFAULT_BATCH_SIZE):
        self.batch_size = positive_limit(batch_size)
        self.generation = 0
        self._positions = {}
        self._stamp = None
        self._deadline = None

    @property
    def pending(self):
        return bool(self._positions)

    @property
    def cursor_count(self):
        return len(self._positions)

    def invalidate(self):
        self.generation += 1
        self._positions.clear()

    def observe(self, stamp, now_us):
        if stamp != self._stamp or (self._deadline is not None and now_us >= self._deadline):
            self.invalidate()
            self._stamp = stamp
            self._deadline = None

    async def begin_round(self, database, now_us):
        def read(owned):
            connection = owned.connection
            with closing(connection.execute("SELECT MAX(id) FROM history_events")) as cursor:
                tail = cursor.fetchone()[0]
            identity = database_identity(connection)
            with closing(connection.execute(
                "SELECT MIN(scheduled_at) FROM actions WHERE status IN (1,2)"
                " AND cancel_requested=0 AND scheduled_at>?", (now_us,),
            )) as cursor:
                deadline = cursor.fetchone()[0]
            return identity, tail, deadline
        identity, tail, deadline = await database.read(read)
        self.observe((identity, tail), now_us)
        self._deadline = deadline
        return identity

    def scope(self, kind, database, now_us):
        if kind not in self._KINDS:
            raise ValueError(f"未登记的发现范围: {kind}")
        saved = self._positions.get(kind)
        return saved.scope if saved is not None else ReadDiscoveryScope(
            database, kind, now_us, self.generation)

    def accept(self, scope, page):
        if scope.kind not in self._KINDS:
            raise ValueError(f"未登记的发现范围: {scope.kind}")
        if scope.generation != self.generation:
            raise ValueError("不能保存旧代次的继续位置")
        check_cursor(scope, page.next_cursor)
        if page.next_cursor is None:
            self._positions.pop(scope.kind, None)
        else:
            old = self._positions.get(scope.kind)
            if old is not None and page.next_cursor.after <= old.after:
                raise ValueError("继续位置必须严格推进")
            self._positions[scope.kind] = page.next_cursor

    async def rows(self, database, kind, now_us, query, parameters=(), *, identity="id"):
        """对内部查询作稳定 ID 续读；query 含 SELECT/FROM/WHERE，不含排序。"""
        database_id = self._stamp[0] if self._stamp is not None else await self.begin_round(database, now_us)
        scope = self.scope(kind, database_id, now_us)
        saved = self._positions.get(kind)
        after = saved.after[0] if saved is not None else 0
        parameters = parameters(scope.now_us) if callable(parameters) else parameters
        rows = await read_all(database,
            query + f" AND {identity}>? ORDER BY {identity} LIMIT ?",
            (*parameters, after, self.batch_size),
        )
        next_cursor = (ReadDiscoveryCursor(scope, (rows[-1][0],))
                       if len(rows) == self.batch_size else None)
        page = Page(items=rows, next_cursor=next_cursor)
        self.accept(scope, page)
        return page

    async def reads(self, database, kind, now_us):
        database_id = self._stamp[0] if self._stamp is not None else await self.begin_round(database, now_us)
        scope = self.scope(kind, database_id, now_us)
        cursor = self._positions.get(kind)
        limit = self.batch_size
        page = await database.read(lambda owned: read_candidates(owned.connection, scope, cursor, limit=limit))
        self.accept(scope, page)
        return page

    async def captures(self, database, now_us, preparation_leads=None):
        """拍摄入口按完整业务顺序续页，避免较晚动作先取得启动资格。"""
        database_id = self._stamp[0] if self._stamp is not None else await self.begin_round(database, now_us)
        scope = self.scope("capture", database_id, now_us)
        saved = self._positions.get("capture")
        order = "scheduled_at,plan_id,input_index,id"
        eligibility = "(status IN (1,2) AND cancel_requested=0 AND scheduled_at<=?) OR (status=2 AND cancel_requested=1)"
        parameters = (scope.now_us,)
        capture_types = ",".join(str(value) for value in CAPTURE_ACTION_TYPES)
        from camctl.capture.preparation_discovery import (
            photo_preparation_start_where, preparation_responsibility_where,
        )
        photo_start = photo_preparation_start_where()
        if preparation_leads:
            for device_id, lead_s in preparation_leads.items():
                horizon = min(MAX_OBJECT_ID, scope.now_us + int(lead_s * 1_000_000))
                eligibility += (f" OR (type IN ({capture_types}) AND status=1 AND cancel_requested=0"
                    f" AND device_id=? AND scheduled_at<=? AND {photo_start})")
                parameters += device_id, horizon
            at = await database.read(lambda owned: next_preparation_at(owned.connection, scope.now_us, preparation_leads))
            if at is not None:
                self._deadline = at if self._deadline is None else min(at, self._deadline)
        eligibility += (" OR EXISTS (SELECT 1 FROM device_activities d WHERE d.action_id=a.id AND "
                        + preparation_responsibility_where(include_pending=True) + ")")
        query = (
            "SELECT id,device_id,scheduled_at,plan_id,input_index FROM actions a"
            f" WHERE type IN ({capture_types}) AND (" + eligibility + ")"
        )
        if saved is not None:
            query += f" AND ({order})>(?,?,?,?)"
            parameters += saved.after
        rows = await read_all(database, query + f" ORDER BY {order} LIMIT ?", (*parameters, self.batch_size))
        next_cursor = (ReadDiscoveryCursor(scope, (rows[-1][2], rows[-1][3], rows[-1][4], rows[-1][0]))
                       if len(rows) == self.batch_size else None)
        page = Page(items=rows, next_cursor=next_cursor)
        self.accept(scope, page)
        return page


def next_preparation_at(connection, now_us, preparation_leads):
    """取尚未进入提前区间的最早时刻，不为已阻塞候选制造空转截止。"""
    result = None
    from camctl.capture.preparation_discovery import photo_preparation_start_where
    capture_types = ",".join(str(value) for value in CAPTURE_ACTION_TYPES)
    photo_start = photo_preparation_start_where()
    for device_id, lead_s in preparation_leads.items():
        lead_us = int(lead_s * 1_000_000)
        horizon = min(MAX_OBJECT_ID, now_us + lead_us)
        with closing(connection.execute(
            f"SELECT MIN(scheduled_at) FROM actions a WHERE type IN ({capture_types})"
            " AND status=1 AND cancel_requested=0 AND device_id=? AND scheduled_at>?"
            f" AND {photo_start}",
            (device_id, horizon),
        )) as cursor:
            at = cursor.fetchone()[0]
        if at is not None:
            at -= lead_us
            result = at if result is None else min(result, at)
    return result
