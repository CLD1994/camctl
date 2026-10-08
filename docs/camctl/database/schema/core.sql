-- 格式 1：计划与动作。字段语义见 ../plans-actions.md。
-- 各文件合并后，在开启 foreign_keys 的同一个初始化事务中执行。
CREATE TABLE plans (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    request_id INTEGER NOT NULL UNIQUE CHECK (request_id > 0),
    name TEXT NOT NULL CHECK (length(name) > 0),
    created_at INTEGER NOT NULL CHECK (created_at % 1000000 = 0),
    status INTEGER NOT NULL CHECK (status IN (1,2,3)),
    created_event_id INTEGER NOT NULL REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    last_event_id INTEGER NOT NULL REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    change_count INTEGER NOT NULL CHECK (change_count >= 1),
    CHECK (last_event_id >= created_event_id)
) STRICT;

CREATE TABLE actions (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    plan_id INTEGER NOT NULL REFERENCES plans(id) DEFERRABLE INITIALLY DEFERRED,
    input_index INTEGER NOT NULL CHECK (input_index >= 0),
    name TEXT NOT NULL CHECK (length(name) > 0),
    type INTEGER NOT NULL CHECK (type IN (1,2,3,4,5,6,7,8)),
    device_id TEXT CHECK (device_id IS NULL OR length(device_id) > 0),
    scheduled_at INTEGER CHECK (scheduled_at IS NULL OR scheduled_at % 1000000 = 0),
    group_name TEXT CHECK (group_name IS NULL OR length(group_name) > 0),
    input_fields_json TEXT NOT NULL CHECK (json_valid(input_fields_json) AND json_type(input_fields_json) = 'object'),
    effective_params_json TEXT CHECK (effective_params_json IS NULL OR (json_valid(effective_params_json) AND json_type(effective_params_json) = 'object')),
    driver_id TEXT CHECK (driver_id IS NULL OR length(driver_id) > 0),
    max_delay_ms INTEGER CHECK (max_delay_ms IS NULL OR max_delay_ms >= 0),
    execution_spec_json TEXT CHECK (execution_spec_json IS NULL OR (json_valid(execution_spec_json) AND json_type(execution_spec_json) = 'object')),
    status INTEGER NOT NULL CHECK (status IN (1,2,3,4,5,6)),
    execution_started INTEGER NOT NULL CHECK (execution_started IN (0,1)),
    cancel_requested INTEGER NOT NULL CHECK (cancel_requested IN (0,1)),
    error_code INTEGER CHECK (error_code IS NULL OR error_code IN (1,2,3,4,10,11,12,13,14,15,16,17,18,20,21,22,23,24,25,26,27,28,29)),
    error_details_json TEXT CHECK (error_details_json IS NULL OR (json_valid(error_details_json) AND json_type(error_details_json) = 'object')),
    first_window_observed_at INTEGER,
    expiration_reason INTEGER CHECK (expiration_reason IS NULL OR expiration_reason IN (1,2)),
    source_resolution_state INTEGER CHECK (source_resolution_state IS NULL OR source_resolution_state IN (1,2,3)),
    resolved_source_plan_id INTEGER REFERENCES plans(id) DEFERRABLE INITIALLY DEFERRED,
    target_selection_state INTEGER CHECK (target_selection_state IS NULL OR target_selection_state IN (1,2,3)),
    created_event_id INTEGER NOT NULL REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    last_event_id INTEGER NOT NULL REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    change_count INTEGER NOT NULL CHECK (change_count >= 1),
    UNIQUE (plan_id, input_index),
    UNIQUE (plan_id, name),
    CHECK (last_event_id >= created_event_id),
    CHECK ((status = 1 AND execution_started = 0 AND cancel_requested = 0)
        OR (status = 2 AND execution_started = 1)
        OR (status = 3 AND execution_started = 1 AND cancel_requested = 0)
        OR (status = 4 AND cancel_requested = 0)
        OR (status = 5 AND cancel_requested = 0)
        OR (status = 6 AND cancel_requested = 1)),
    CHECK ((status = 4 AND error_code IS NOT NULL AND error_details_json IS NOT NULL)
        OR (status <> 4 AND error_code IS NULL AND error_details_json IS NULL)),
    CHECK (status <> 4 OR (execution_started = 0 AND error_code IN (1,2,3,4))
        OR (execution_started = 1 AND error_code IN (10,11,12,13,14,15,16,17,18,20,21,22,23,24,25,26,27,28,29))),
    CHECK ((execution_spec_json IS NULL AND status = 4 AND execution_started = 0)
        OR (execution_spec_json IS NOT NULL AND NOT (status = 4 AND execution_started = 0))),
    CHECK ((type IN (1,2,3) AND execution_spec_json IS NOT NULL AND effective_params_json IS NOT NULL
            AND driver_id IS NOT NULL AND device_id IS NOT NULL AND scheduled_at IS NOT NULL AND max_delay_ms IS NOT NULL)
        OR ((type NOT IN (1,2,3) OR execution_spec_json IS NULL) AND effective_params_json IS NULL AND driver_id IS NULL)),
    CHECK (type IN (1,2,3,8) OR (device_id IS NULL AND max_delay_ms IS NULL AND first_window_observed_at IS NULL AND expiration_reason IS NULL)),
    CHECK (execution_spec_json IS NULL OR type NOT IN (4,5) OR scheduled_at IS NOT NULL),
    CHECK (type <> 4 OR group_name IS NULL),
    CHECK (type <> 8 OR device_id IS NULL),
    CHECK (type <> 8 OR execution_spec_json IS NULL OR (scheduled_at IS NOT NULL AND max_delay_ms IS NOT NULL AND execution_spec_json = '{}')),
    CHECK (execution_spec_json IS NOT NULL OR first_window_observed_at IS NULL),
    CHECK ((status = 5 AND type IN (1,2,3,8) AND expiration_reason IS NOT NULL
            AND ((expiration_reason = 1 AND first_window_observed_at IS NULL) OR (expiration_reason = 2 AND first_window_observed_at IS NOT NULL)))
        OR (status <> 5 AND expiration_reason IS NULL)),
    CHECK ((source_resolution_state IS 2 AND resolved_source_plan_id IS NOT NULL)
        OR ((source_resolution_state IS NULL OR source_resolution_state IN (1,3)) AND resolved_source_plan_id IS NULL)),
    CHECK (source_resolution_state IS NULL OR (type IN (4,5) AND execution_spec_json IS NOT NULL)),
    CHECK (source_resolution_state IS NULL OR source_resolution_state <> 3 OR (status = 4 AND execution_started = 1)),
    CHECK (target_selection_state IS NULL OR (type IN (5,6) AND execution_spec_json IS NOT NULL)),
    CHECK (target_selection_state IS NULL OR target_selection_state <> 3 OR (status = 4 AND execution_started = 1))
) STRICT;

CREATE INDEX actions_plan_members ON actions(plan_id, id);
CREATE INDEX actions_group_members ON actions(plan_id, group_name, id) WHERE group_name IS NOT NULL;
CREATE INDEX actions_schedule ON actions(scheduled_at, id) WHERE status IN (1,2);
CREATE INDEX actions_device_active ON actions(device_id, scheduled_at, id) WHERE status IN (1,2) AND device_id IS NOT NULL;
