"""本地配置的精确覆盖与冻结。

纯函数：先取得完整默认值，再按字段应用文件提供的覆盖值，最后
校验整组组合。容量字符串按十进制精确换算为字节；配置一旦加载
为 ConfigSnapshot 不可被后续流程修改。文件读取与路径解析属于
装配适配器，不在本模块执行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping

__all__ = [
    "ConfigDefaults",
    "ConfigError",
    "ConfigSnapshot",
    "load_config",
    "parse_capacity",
]

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

#: 容量字符串：十进制数值（小数点前后均有数字）+ 区分大小写的单位，
#: 数值与单位之间及字符串首尾允许零个或多个 ASCII 空格。
_CAPACITY = re.compile(r"\A *([0-9]+(?:\.[0-9]+)?) *(B|KiB|MiB|GiB|KB|MB|GB) *\Z")

_CAPACITY_UNITS = {
    "B": Decimal(1),
    "KiB": Decimal(1024),
    "MiB": Decimal(1024) ** 2,
    "GiB": Decimal(1024) ** 3,
    "KB": Decimal(1000),
    "MB": Decimal(1000) ** 2,
    "GB": Decimal(1000) ** 3,
}

_DATE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


class ConfigError(ValueError):
    """本地配置缺失、类型不合法、组合矛盾或包含未知字段。"""


def parse_capacity(raw: Any) -> int:
    """把带单位的容量字符串精确换算为整数个字节。"""
    if not isinstance(raw, str):
        raise ConfigError(f"容量必须是字符串: {raw!r}")
    match = _CAPACITY.match(raw)
    if match is None:
        raise ConfigError(f"容量写法不合法: {raw!r}")
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation as error:  # pragma: no cover - 正则已保证数字
        raise ConfigError(f"容量数值不合法: {raw!r}") from error
    total = amount * _CAPACITY_UNITS[match.group(2)]
    if total != total.to_integral_value():
        raise ConfigError(f"容量不是整数个字节: {raw!r}")
    return int(total)


def _require_int(value: Any, name: str, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} 必须是整数: {value!r}")
    if value < minimum:
        raise ConfigError(f"{name} 不能小于 {minimum}: {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} 不能大于 {maximum}: {value}")
    return value


def _require_positive_seconds(value: Any, name: str, *, allow_zero: bool = False) -> Decimal:
    minimum = Decimal(0) if allow_zero else None
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ConfigError(f"{name} 必须是数值秒: {value!r}")
    seconds = value if isinstance(value, Decimal) else Decimal(value)
    if not seconds.is_finite():
        raise ConfigError(f"{name} 必须是有限数值: {value!r}")
    if minimum is None and seconds <= 0:
        raise ConfigError(f"{name} 必须是正数秒: {value!r}")
    if minimum is not None and seconds < 0:
        raise ConfigError(f"{name} 不能为负: {value!r}")
    return seconds


def _require_probability(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ConfigError(f"{name} 必须是数值: {value!r}")
    probability = value if isinstance(value, Decimal) else Decimal(value)
    if not probability.is_finite():
        raise ConfigError(f"{name} 必须是有限数值: {value!r}")
    if probability < 0 or probability > 1:
        raise ConfigError(f"{name} 必须在闭区间 [0, 1]: {value!r}")
    return probability


def _require_nonempty_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} 必须是非空字符串: {value!r}")
    return value


def _require_date(value: Any, name: str) -> date:
    if not isinstance(value, str) or _DATE.match(value) is None:
        raise ConfigError(f"{name} 必须是 YYYY-MM-DD 日期: {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ConfigError(f"{name} 不是真实日期: {value!r}") from error


def _take(mapping: Mapping[str, Any], section: str, known: tuple[str, ...]) -> Mapping[str, Any]:
    if not isinstance(mapping, Mapping):
        raise ConfigError(f"{section} 必须是表: {mapping!r}")
    unknown = sorted(set(mapping) - set(known))
    if unknown:
        raise ConfigError(f"{section} 包含未知字段: {unknown}")
    return mapping


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class DatabaseSection:
    queue_capacity: int
    busy_timeout_ms: int
    wal_autocheckpoint_pages: int


@dataclass(frozen=True)
class LogSection:
    level: str
    max_size_bytes: int
    file_count: int
    queue_capacity: int
    queue_low_watermark: int
    queue_high_watermark: int
    info_sample_probability: Decimal


@dataclass(frozen=True)
class SessionSection:
    plan_poll_interval_ms: int
    wakeup_margin_ms: int


@dataclass(frozen=True)
class HistorySection:
    snapshot_change_threshold: int
    snapshot_batch_size: int
    entity_batch_size: int
    event_batch_size: int


@dataclass(frozen=True)
class CopySection:
    segment_size_bytes: int


@dataclass(frozen=True)
class CleanupSection:
    max_delete_attempts: int
    max_query_attempts: int
    work_file_batch_size: int
    work_file_limit_per_run: int


@dataclass(frozen=True)
class ClockSection:
    min_plausible_date: date
    lower_bound_tolerance_s: Decimal
    recheck_delay_s: Decimal
    recheck_count: int
    recovery_wait_cap_s: Decimal


@dataclass(frozen=True)
class AdbSection:
    terminate_grace_s: Decimal


@dataclass(frozen=True)
class PathsSection:
    state_db: str
    log_file: str
    staging: str
    ready: str
    processing: str


@dataclass(frozen=True)
class ConfigSnapshot:
    """本次运行内固定不变的生效配置。"""

    database: DatabaseSection
    session: SessionSection
    log: LogSection
    history: HistorySection
    copy: CopySection
    cleanup: CleanupSection
    clock: ClockSection
    adb: AdbSection
    paths: PathsSection
    #: 设备目录的原始声明；驱动专属字段的深校验由设备接入契约承接。
    devices: Mapping[str, Any]


@dataclass(frozen=True)
class ConfigDefaults:
    """完整内置默认值；含环境因素的路径默认由装配适配器提供。"""

    state_db: str = "$HOME/.camctl/state.db"
    log_file: str = "$HOME/.camctl/camctl.log"
    staging: str = "$HOME/.camctl/staging"
    ready: str = "$HOME/.camctl/ready"
    processing: str = "$HOME/.camctl/processing"
    log_level: str = "WARNING"
    log_max_size: str = "10 MiB"
    log_file_count: int = 3
    log_queue_capacity: int = 1000
    log_queue_low_watermark: int = 700
    log_queue_high_watermark: int = 800
    log_info_sample_probability: str = "0.1"
    adb_terminate_grace_s: str = "3"
    plan_poll_interval_ms: int = 500
    wakeup_margin_ms: int = 50
    queue_capacity: int = 64
    busy_timeout_ms: int = 9000
    wal_autocheckpoint_pages: int = 1000
    snapshot_change_threshold: int = 64
    snapshot_batch_size: int = 16
    entity_batch_size: int = 32
    event_batch_size: int = 256
    segment_size: str = "128 MiB"
    max_delete_attempts: int = 3
    max_query_attempts: int = 3
    work_file_batch_size: int = 32
    work_file_limit_per_run: int = 128
    min_plausible_date: str = "2025-01-01"
    lower_bound_tolerance_s: str = "5"
    recheck_delay_s: str = "5"
    recheck_count: int = 1
    recovery_wait_cap_s: str = "60"


def load_config(document: Any, defaults: ConfigDefaults) -> ConfigSnapshot:
    """按默认值、字段覆盖与整组校验加载生效配置。

    document 为已解析的配置文档（tomllib 以 Decimal 读取小数）或
    None（无配置文件，使用完整默认值）。任何字段类型不合法、组
    合矛盾或存在未知字段都按配置错误拒绝，不用默认值掩盖。
    """
    if document is None:
        document = {}
    if not isinstance(document, Mapping):
        raise ConfigError(f"配置文档必须是表: {type(document).__name__}")
    root = _take(document, "配置", ("database", "session", "log", "history", "copy", "cleanup", "clock", "adb", "paths", "devices"))

    database = _take(root.get("database", {}), "database", ("queue_capacity", "busy_timeout_ms", "wal_autocheckpoint_pages"))
    db_section = DatabaseSection(
        queue_capacity=_require_int(database.get("queue_capacity", defaults.queue_capacity), "database.queue_capacity", 1),
        busy_timeout_ms=_require_int(
            database.get("busy_timeout_ms", defaults.busy_timeout_ms),
            "database.busy_timeout_ms",
            1,
            2147483647,
        ),
        wal_autocheckpoint_pages=_require_int(
            database.get("wal_autocheckpoint_pages", defaults.wal_autocheckpoint_pages),
            "database.wal_autocheckpoint_pages",
            1,
            2147483647,
        ),
    )

    session = _take(root.get("session", {}), "session", ("plan_poll_interval_ms", "wakeup_margin_ms"))
    session_section = SessionSection(
        plan_poll_interval_ms=_require_int(
            session.get("plan_poll_interval_ms", defaults.plan_poll_interval_ms),
            "session.plan_poll_interval_ms",
            1,
        ),
        wakeup_margin_ms=_require_int(
            session.get("wakeup_margin_ms", defaults.wakeup_margin_ms),
            "session.wakeup_margin_ms",
            0,
        ),
    )

    log = _take(
        root.get("log", {}),
        "log",
        ("level", "max_size", "file_count", "queue_capacity", "queue_low_watermark", "queue_high_watermark", "info_sample_probability"),
    )
    level = log.get("level", defaults.log_level)
    if level not in _LOG_LEVELS:
        raise ConfigError(f"log.level 必须是 {_LOG_LEVELS}: {level!r}")
    log_section = LogSection(
        level=level,
        max_size_bytes=_capacity_field(log, "max_size", defaults.log_max_size, "log.max_size", 1),
        file_count=_require_int(log.get("file_count", defaults.log_file_count), "log.file_count", 1),
        queue_capacity=_require_int(log.get("queue_capacity", defaults.log_queue_capacity), "log.queue_capacity", 1),
        queue_low_watermark=_require_int(
            log.get("queue_low_watermark", defaults.log_queue_low_watermark),
            "log.queue_low_watermark",
            1,
        ),
        queue_high_watermark=_require_int(
            log.get("queue_high_watermark", defaults.log_queue_high_watermark),
            "log.queue_high_watermark",
            1,
        ),
        info_sample_probability=_probability_field(log, "info_sample_probability", defaults.log_info_sample_probability),
    )
    if not (
        0
        < log_section.queue_low_watermark
        < log_section.queue_high_watermark
        < log_section.queue_capacity
    ):
        raise ConfigError(
            "日志队列组合不合法: 须满足 0 < low < high < capacity，"
            f"实际 {log_section.queue_low_watermark}/{log_section.queue_high_watermark}/{log_section.queue_capacity}"
        )

    history = _take(
        root.get("history", {}),
        "history",
        ("snapshot_change_threshold", "snapshot_batch_size", "entity_batch_size", "event_batch_size"),
    )
    history_section = HistorySection(
        snapshot_change_threshold=_require_int(
            history.get("snapshot_change_threshold", defaults.snapshot_change_threshold),
            "history.snapshot_change_threshold",
            1,
        ),
        snapshot_batch_size=_require_int(history.get("snapshot_batch_size", defaults.snapshot_batch_size), "history.snapshot_batch_size", 1),
        entity_batch_size=_require_int(history.get("entity_batch_size", defaults.entity_batch_size), "history.entity_batch_size", 1),
        event_batch_size=_require_int(history.get("event_batch_size", defaults.event_batch_size), "history.event_batch_size", 1),
    )

    copy = _take(root.get("copy", {}), "copy", ("segment_size",))
    copy_section = CopySection(
        segment_size_bytes=_capacity_field(copy, "segment_size", defaults.segment_size, "copy.segment_size", 1)
    )

    cleanup = _take(
        root.get("cleanup", {}),
        "cleanup",
        ("max_delete_attempts", "max_query_attempts", "work_file_batch_size", "work_file_limit_per_run"),
    )
    cleanup_section = CleanupSection(
        max_delete_attempts=_require_int(cleanup.get("max_delete_attempts", defaults.max_delete_attempts), "cleanup.max_delete_attempts", 1),
        max_query_attempts=_require_int(cleanup.get("max_query_attempts", defaults.max_query_attempts), "cleanup.max_query_attempts", 1),
        work_file_batch_size=_require_int(cleanup.get("work_file_batch_size", defaults.work_file_batch_size), "cleanup.work_file_batch_size", 1),
        work_file_limit_per_run=_require_int(cleanup.get("work_file_limit_per_run", defaults.work_file_limit_per_run), "cleanup.work_file_limit_per_run", 1),
    )

    clock = _take(
        root.get("clock", {}),
        "clock",
        ("min_plausible_date", "lower_bound_tolerance_s", "recheck_delay_s", "recheck_count", "recovery_wait_cap_s"),
    )
    clock_section = ClockSection(
        min_plausible_date=_require_date(clock.get("min_plausible_date", defaults.min_plausible_date), "clock.min_plausible_date"),
        lower_bound_tolerance_s=_seconds_field(clock, "lower_bound_tolerance_s", defaults.lower_bound_tolerance_s, allow_zero=False),
        recheck_delay_s=_seconds_field(clock, "recheck_delay_s", defaults.recheck_delay_s, allow_zero=False),
        recheck_count=_require_int(clock.get("recheck_count", defaults.recheck_count), "clock.recheck_count", 1),
        recovery_wait_cap_s=_seconds_field(clock, "recovery_wait_cap_s", defaults.recovery_wait_cap_s, allow_zero=True),
    )

    paths = _take(root.get("paths", {}), "paths", ("state_db", "log_file", "staging", "ready", "processing"))
    paths_section = PathsSection(
        state_db=_require_nonempty_str(paths.get("state_db", defaults.state_db), "paths.state_db"),
        log_file=_require_nonempty_str(paths.get("log_file", defaults.log_file), "paths.log_file"),
        staging=_require_nonempty_str(paths.get("staging", defaults.staging), "paths.staging"),
        ready=_require_nonempty_str(paths.get("ready", defaults.ready), "paths.ready"),
        processing=_require_nonempty_str(paths.get("processing", defaults.processing), "paths.processing"),
    )

    devices = _validate_devices(root.get("devices", {}))

    adb = _take(root.get("adb", {}), "adb", ("terminate_grace_s",))
    raw_grace = adb.get("terminate_grace_s", defaults.adb_terminate_grace_s)
    if isinstance(raw_grace, str):
        try:
            raw_grace = Decimal(raw_grace)
        except InvalidOperation as error:
            raise ConfigError(f"adb.terminate_grace_s 必须是数值秒: {raw_grace!r}") from error
    adb_section = AdbSection(
        terminate_grace_s=_require_positive_seconds(raw_grace, "adb.terminate_grace_s")
    )
    return ConfigSnapshot(
        database=db_section,
        session=session_section,
        log=log_section,
        history=history_section,
        copy=copy_section,
        cleanup=cleanup_section,
        clock=clock_section,
        adb=adb_section,
        paths=paths_section,
        devices=devices,
    )


def _capacity_field(
    section: Mapping[str, Any], key: str, default: str, name: str, minimum: int
) -> int:
    raw = section.get(key, default)
    value = parse_capacity(raw)
    if value < minimum:
        raise ConfigError(f"{name} 换算后不能小于 {minimum} 字节: {raw!r}")
    return value


def _seconds_field(
    section: Mapping[str, Any], key: str, default: str, *, allow_zero: bool
) -> Decimal:
    raw = section.get(key, default)
    if isinstance(raw, str):
        try:
            raw = Decimal(raw)
        except InvalidOperation as error:
            raise ConfigError(f"clock.{key} 必须是数值秒: {raw!r}") from error
    return _require_positive_seconds(raw, f"clock.{key}", allow_zero=allow_zero)


def _probability_field(section: Mapping[str, Any], key: str, default: str) -> Decimal:
    raw = section.get(key, default)
    if isinstance(raw, str):
        try:
            raw = Decimal(raw)
        except InvalidOperation as error:
            raise ConfigError(f"log.{key} 必须是数值: {raw!r}") from error
    return _require_probability(raw, f"log.{key}")


def _validated_seconds_subtable(
    device_id: str, raw: Any, section: str, keys: tuple,
    positive_keys: tuple = (),
) -> Mapping[str, Any] | None:
    """校验并规范化设备子表中的秒数字段；未提供子表时返回 None。

    列出的键默认允许有限非负秒数（数字或精确文本），positive_keys
    中的键要求正秒数；加载时统一为 Decimal，其余键按原样冻结，随
    消费方接入再校验。
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ConfigError(f"devices.{device_id}.{section} 必须是表: {raw!r}")
    normalized = dict(raw)
    for key in keys:
        if key not in normalized:
            continue
        name = f"devices.{device_id}.{section}.{key}"
        value = normalized[key]
        if isinstance(value, str):
            try:
                value = Decimal(value)
            except InvalidOperation as error:
                raise ConfigError(
                    f"{name} 必须是数值秒: {normalized[key]!r}"
                ) from error
        normalized[key] = _require_positive_seconds(
            value, name, allow_zero=key not in positive_keys)
    return normalized


def _validated_recording(device_id: str, raw: Any) -> Mapping[str, Any] | None:
    """校验并规范化设备录像配置键；未提供时返回 None。"""
    return _validated_seconds_subtable(
        device_id, raw, "recording",
        ("repair_margin_s", "start_retry_interval_s", "stop_retry_interval_s"))


def _validated_attempts_subtable(
    device_id: str, raw: Any, section: str,
) -> Mapping[str, Any] | None:
    """校验查询与残留收场子表：正整数次数加间隔与时限秒。

    次数必须是不小于 1 的整数（bool 不作次数）；重试间隔允许有限
    非负秒数，0 表示不额外等待；调用时限要求正秒数（configuration
    .md#状态查询与产物核实的配置、camera-recovery.md#后续动作触发
    的残留收场）。
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ConfigError(f"devices.{device_id}.{section} 必须是表: {raw!r}")
    normalized = dict(raw)
    if "max_attempts" in normalized:
        attempts = normalized["max_attempts"]
        name = f"devices.{device_id}.{section}.max_attempts"
        if (isinstance(attempts, bool) or not isinstance(attempts, int)
                or attempts < 1):
            raise ConfigError(f"{name} 必须是正整数: {attempts!r}")
    for key, positive in (("retry_interval_s", False), ("timeout_s", True)):
        if key not in normalized:
            continue
        name = f"devices.{device_id}.{section}.{key}"
        value = normalized[key]
        if isinstance(value, str):
            try:
                value = Decimal(value)
            except InvalidOperation as error:
                raise ConfigError(
                    f"{name} 必须是数值秒: {normalized[key]!r}"
                ) from error
        normalized[key] = _require_positive_seconds(
            value, name, allow_zero=not positive)
    return normalized


def _validate_devices(raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"devices 必须是表: {raw!r}")
    validated: dict[str, Any] = {}
    for device_id, declaration in raw.items():
        if not isinstance(device_id, str) or not device_id:
            raise ConfigError(f"设备身份必须是非空字符串: {device_id!r}")
        if not isinstance(declaration, Mapping):
            raise ConfigError(f"devices.{device_id} 必须是表: {declaration!r}")
        _require_nonempty_str(declaration.get("kind"), f"devices.{device_id}.kind")
        _require_nonempty_str(declaration.get("driver"), f"devices.{device_id}.driver")
        normalized = dict(declaration)
        recording = _validated_recording(
            device_id, normalized.pop("recording", None))
        if recording is not None:
            normalized["recording"] = recording
        for section in ("query", "residual_stop"):
            subtable = _validated_attempts_subtable(
                device_id, normalized.pop(section, None), section)
            if subtable is not None:
                normalized[section] = subtable
        for section, keys, positive in (
                ("copy", ("retry_interval_s",), ()),
                ("cleanup", ("delete_retry_interval_s", "query_retry_interval_s",
                             "delete_timeout_s", "query_timeout_s"),
                 ("delete_timeout_s", "query_timeout_s")),
                ("result_check", ("retry_interval_s",), ())):
            subtable = _validated_seconds_subtable(
                device_id, normalized.pop(section, None), section, keys,
                positive_keys=positive)
            if subtable is not None:
                normalized[section] = subtable
        validated[device_id] = _freeze(normalized)
    return MappingProxyType(validated)
