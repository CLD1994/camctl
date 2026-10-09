-- 格式 1：有限操作流程、尝试及设备事实。
CREATE TABLE operation_runs (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    action_id INTEGER NOT NULL REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,
    delivery_id INTEGER REFERENCES deliveries(id) DEFERRABLE INITIALLY DEFERRED,
    kind INTEGER NOT NULL CHECK (kind BETWEEN 1 AND 9),
    query_purpose INTEGER CHECK (query_purpose IS NULL OR query_purpose IN (1,2,3,4,5)),
    responsibility_key TEXT NOT NULL UNIQUE CHECK (length(responsibility_key) > 0),
    activity_id INTEGER REFERENCES device_activities(id) DEFERRABLE INITIALLY DEFERRED,
    copy_id INTEGER REFERENCES file_copies(id) DEFERRABLE INITIALLY DEFERRED,
    cleanup_item_id INTEGER REFERENCES cleanup_items(id) DEFERRABLE INITIALLY DEFERRED,
    session_key TEXT CHECK (session_key IS NULL OR (length(session_key) = 32 AND session_key NOT GLOB '*[^0-9a-f]*')),
    status INTEGER NOT NULL CHECK (status BETWEEN 1 AND 7),
    attempts_used INTEGER NOT NULL CHECK (attempts_used >= 0),
    max_attempts_used INTEGER CHECK (max_attempts_used IS NULL OR max_attempts_used > 0),
    timeout_s_json TEXT CHECK (timeout_s_json IS NULL OR (json_valid(timeout_s_json) AND json_type(timeout_s_json) IN ('integer','real'))),
    retry_interval_s_json TEXT CHECK (retry_interval_s_json IS NULL OR (json_valid(retry_interval_s_json) AND json_type(retry_interval_s_json) IN ('integer','real'))),
    retry_wait_required INTEGER NOT NULL CHECK (retry_wait_required IN (0,1)),
    error_json TEXT CHECK (error_json IS NULL OR (json_valid(error_json) AND json_type(error_json) = 'object')),
    CHECK ((kind = 6 AND query_purpose IS NOT NULL) OR (kind <> 6 AND query_purpose IS NULL)),
    CHECK ((kind IN (1,2,7,8,9) AND activity_id IS NOT NULL AND copy_id IS NULL AND cleanup_item_id IS NULL)
        OR (kind = 6 AND query_purpose = 1 AND activity_id IS NULL AND copy_id IS NULL AND cleanup_item_id IS NULL)
        OR (kind = 6 AND query_purpose IN (2,3,4,5) AND activity_id IS NOT NULL AND copy_id IS NULL AND cleanup_item_id IS NULL)
        OR (kind = 3 AND activity_id IS NULL AND copy_id IS NOT NULL AND cleanup_item_id IS NULL)
        OR (kind IN (4,5) AND activity_id IS NULL AND copy_id IS NULL AND cleanup_item_id IS NOT NULL)),
    CHECK (kind <> 6 OR responsibility_key = CASE query_purpose
        WHEN 1 THEN 'query/preflight/' || action_id
        WHEN 2 THEN 'query/start/' || action_id || '/' || activity_id
        WHEN 3 THEN 'query/activity/' || action_id || '/' || activity_id
        WHEN 4 THEN 'query/stop/' || action_id || '/' || activity_id
        WHEN 5 THEN 'query/residual/' || action_id || '/' || activity_id
        END),
    CHECK (kind <> 7 OR responsibility_key = 'results/' || activity_id),
    CHECK (kind NOT IN (6,7) OR (timeout_s_json IS NOT NULL AND retry_interval_s_json IS NOT NULL)),
    CHECK ((kind = 9 AND session_key IS NOT NULL) OR (kind <> 9 AND session_key IS NULL)),
    CHECK (max_attempts_used IS NOT NULL OR (kind = 9 AND attempts_used = 0)),
    CHECK (kind <> 9 OR responsibility_key = 'emergency/' || session_key || '/' || activity_id),
    CHECK (kind <> 9 OR max_attempts_used IS NULL OR attempts_used <= max_attempts_used),
    CHECK (kind <> 9 OR attempts_used = 0 OR (timeout_s_json IS NOT NULL AND retry_interval_s_json IS NOT NULL)),
    CHECK (kind <> 9 OR (status = 3 AND error_json IS NULL)
        OR (status = 4 AND attempts_used = 0 AND error_json IS NOT NULL)
        OR (status = 6 AND attempts_used > 0 AND error_json IS NOT NULL)),
    CHECK (delivery_id IS NULL OR kind = 3),
    CHECK (status NOT IN (4,6) OR error_json IS NOT NULL),
    CHECK (status <> 7 OR kind = 1 OR (kind = 8 AND attempts_used = 0)),
    CHECK (kind <> 8 OR status <> 5 OR attempts_used = 0),
    CHECK (status IN (1,2) OR retry_wait_required = 0)
) STRICT;
CREATE INDEX operations_action ON operation_runs(action_id, id);
CREATE INDEX operations_delivery ON operation_runs(delivery_id, id) WHERE delivery_id IS NOT NULL;
CREATE INDEX operations_activity ON operation_runs(activity_id, kind, id) WHERE activity_id IS NOT NULL;
CREATE UNIQUE INDEX one_read_flow ON operation_runs(copy_id) WHERE kind = 3;
CREATE UNIQUE INDEX one_cleanup_flow ON operation_runs(cleanup_item_id, kind) WHERE kind IN (4,5);

CREATE TABLE operation_attempts (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    run_id INTEGER NOT NULL REFERENCES operation_runs(id) DEFERRABLE INITIALLY DEFERRED,
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    copy_round INTEGER CHECK (copy_round IS NULL OR copy_round > 0),
    status INTEGER NOT NULL CHECK (status IN (1,2,3,4)),
    intent_event_id INTEGER REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    result_event_id INTEGER REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    result_first_page_event_id INTEGER REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    result_last_page_event_id INTEGER REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    max_attempts_used INTEGER NOT NULL CHECK (max_attempts_used > 0),
    timeout_s_json TEXT CHECK (timeout_s_json IS NULL OR (json_valid(timeout_s_json) AND json_type(timeout_s_json) IN ('integer','real'))),
    retry_interval_s_json TEXT CHECK (retry_interval_s_json IS NULL OR (json_valid(retry_interval_s_json) AND json_type(retry_interval_s_json) IN ('integer','real'))),
    effect_state INTEGER NOT NULL CHECK (effect_state IN (1,2,3)),
    result_json TEXT CHECK (result_json IS NULL OR (json_valid(result_json) AND json_type(result_json) = 'object')),
    error_json TEXT CHECK (error_json IS NULL OR (json_valid(error_json) AND json_type(error_json) = 'object')),
    UNIQUE (run_id, attempt_no),
    CHECK ((result_first_page_event_id IS NULL) = (result_last_page_event_id IS NULL)),
    CHECK (result_first_page_event_id IS NULL OR result_first_page_event_id <= result_last_page_event_id),
    -- 是否属于应急停止须在写事务内沿 run_id 核对；普通尝试仍必须有意图引用。
    CHECK (intent_event_id IS NOT NULL OR (status IN (2,3,4) AND copy_round IS NULL
        AND timeout_s_json IS NOT NULL AND retry_interval_s_json IS NOT NULL AND result_json IS NOT NULL)),
    CHECK ((status = 1 AND result_event_id IS NULL) OR (status <> 1 AND result_event_id IS NOT NULL)),
    CHECK (status NOT IN (3,4) OR error_json IS NOT NULL),
    CHECK (status <> 2 OR (result_json IS NOT NULL AND error_json IS NULL))
) STRICT;

CREATE TABLE device_activities (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    action_id INTEGER NOT NULL UNIQUE REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,
    task_key TEXT NOT NULL UNIQUE CHECK (length(task_key) = 32 AND task_key NOT GLOB '*[^0-9a-f]*'),
    task_locator_json TEXT CHECK (task_locator_json IS NULL OR (json_valid(task_locator_json) AND json_type(task_locator_json) = 'object')),
    state_query_supported INTEGER NOT NULL CHECK (state_query_supported IN (0,1)),
    stop_supported INTEGER NOT NULL CHECK (stop_supported IN (0,1)),
    safe_repeat_stop INTEGER NOT NULL CHECK (safe_repeat_stop IN (0,1)),
    start_return_meaning INTEGER NOT NULL CHECK (start_return_meaning IN (1,2,3)),
    completion_mode INTEGER NOT NULL CHECK (completion_mode IN (1,2)),
    ownership_mode INTEGER NOT NULL CHECK (ownership_mode IN (1,2)),
    output_scope_json TEXT NOT NULL CHECK (json_valid(output_scope_json) AND json_type(output_scope_json) = 'object'),
    baseline_state INTEGER NOT NULL CHECK (baseline_state IN (1,2,3)),
    baseline_first_event_id INTEGER REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    baseline_last_event_id INTEGER REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    dispatch_state INTEGER NOT NULL CHECK (dispatch_state IN (1,2,3,4)),
    activity_state INTEGER NOT NULL CHECK (activity_state IN (1,2,3)),
    occupancy_state INTEGER NOT NULL CHECK (occupancy_state IN (1,2)),
    sent_at INTEGER,
    started_at INTEGER,
    result_wait_margin_ms INTEGER CHECK (result_wait_margin_ms IS NULL OR result_wait_margin_ms >= 0),
    extra_wait_ms_used INTEGER CHECK (extra_wait_ms_used IS NULL OR extra_wait_ms_used >= 0),
    expected_check_at INTEGER,
    wait_completed_event_id INTEGER REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    capture_json TEXT CHECK (capture_json IS NULL OR (json_valid(capture_json) AND json_type(capture_json) = 'object')),
    control_elapsed_ns INTEGER CHECK (control_elapsed_ns IS NULL OR control_elapsed_ns >= 0),
    completion_basis INTEGER CHECK (completion_basis IS NULL OR completion_basis IN (1,2,3,4)),
    completion_evidence_json TEXT CHECK (completion_evidence_json IS NULL OR (json_valid(completion_evidence_json) AND json_type(completion_evidence_json) = 'object')),
    result_set_state INTEGER NOT NULL CHECK (result_set_state IN (1,2,3,4)),
    result_check_json TEXT CHECK (result_check_json IS NULL OR (json_valid(result_check_json) AND json_type(result_check_json) = 'object')),
    last_error_json TEXT CHECK (last_error_json IS NULL OR (json_valid(last_error_json) AND json_type(last_error_json) = 'object')),
    CHECK (safe_repeat_stop = 0 OR stop_supported = 1),
    CHECK ((baseline_first_event_id IS NULL) = (baseline_last_event_id IS NULL)),
    CHECK (baseline_first_event_id IS NULL OR baseline_last_event_id >= baseline_first_event_id),
    CHECK ((ownership_mode = 1 AND baseline_state = 1 AND baseline_first_event_id IS NULL)
        OR (ownership_mode = 2 AND baseline_state IN (2,3))),
    CHECK (expected_check_at IS NULL OR (sent_at IS NOT NULL AND result_wait_margin_ms IS NOT NULL AND extra_wait_ms_used IS NOT NULL)),
    CHECK (((completion_basis IS NULL OR completion_basis = 1) AND completion_evidence_json IS NULL)
        OR (completion_basis IS NOT NULL AND completion_basis IN (2,3,4) AND completion_evidence_json IS NOT NULL)),
    CHECK (completion_basis IS NOT NULL OR capture_json IS NULL),
    CHECK (result_set_state NOT IN (3,4) OR result_check_json IS NOT NULL),
    CHECK (dispatch_state NOT IN (1,4) OR (activity_state = 1
        AND (completion_basis IS NULL OR completion_basis IN (1,4)))),
    CHECK (completion_basis IS NOT 2 OR activity_state = 3),
    CHECK (completion_basis IS NOT 3 OR (completion_mode = 2 AND dispatch_state = 3
        AND activity_state IN (1,3) AND sent_at IS NOT NULL AND expected_check_at IS NOT NULL
        AND wait_completed_event_id IS NOT NULL AND result_set_state = 3)),
    CHECK (occupancy_state <> 2 OR dispatch_state IN (1,4) OR activity_state = 3
        OR COALESCE(completion_basis = 3, 0))
) STRICT;
CREATE INDEX activities_occupancy ON device_activities(action_id, id) WHERE occupancy_state = 1;

CREATE TABLE recording_processing (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    action_id INTEGER NOT NULL UNIQUE REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,
    source_device_file_id INTEGER REFERENCES device_files(id) DEFERRABLE INITIALLY DEFERRED,
    check_state INTEGER NOT NULL CHECK (check_state IN (1,2,3,4,5)),
    check_decision INTEGER NOT NULL CHECK (check_decision IN (1,2,3)),
    check_basis_json TEXT CHECK (check_basis_json IS NULL OR (json_valid(check_basis_json) AND json_type(check_basis_json) = 'object')),
    media_json TEXT NOT NULL CHECK (json_valid(media_json) AND json_type(media_json) = 'object'),
    repair_state INTEGER NOT NULL CHECK (repair_state BETWEEN 1 AND 7),
    repair_basis_json TEXT CHECK (repair_basis_json IS NULL OR (json_valid(repair_basis_json) AND json_type(repair_basis_json) = 'object')),
    repair_output_file_id INTEGER UNIQUE REFERENCES intermediate_files(id) DEFERRABLE INITIALLY DEFERRED,
    repair_error_json TEXT CHECK (repair_error_json IS NULL OR (json_valid(repair_error_json) AND json_type(repair_error_json) = 'object')),
    discard_state INTEGER NOT NULL CHECK (discard_state BETWEEN 1 AND 6),
    discard_error_json TEXT CHECK (discard_error_json IS NULL OR (json_valid(discard_error_json) AND json_type(discard_error_json) = 'object')),
    CHECK (check_decision = 1 OR check_basis_json IS NOT NULL),
    CHECK (repair_state = 1 OR repair_basis_json IS NOT NULL),
    CHECK (repair_state <> 5 OR repair_output_file_id IS NOT NULL),
    CHECK (repair_state <> 6 OR repair_error_json IS NOT NULL),
    CHECK (discard_state NOT IN (5,6) OR discard_error_json IS NOT NULL)
) STRICT;
