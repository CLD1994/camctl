-- 格式 1：输入诊断、报告与单份状态。语义见 ../reports-runtime.md。
CREATE TABLE plan_file_diagnostics (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    input_path TEXT NOT NULL,
    request_id INTEGER CHECK (request_id IS NULL OR request_id > 0),
    plan_id INTEGER REFERENCES plans(id) DEFERRABLE INITIALLY DEFERRED,
    errors_json TEXT NOT NULL CHECK (json_valid(errors_json) AND json_type(errors_json) = 'array' AND json_array_length(errors_json) > 0),
    created_event_id INTEGER NOT NULL REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE INDEX diagnostics_request ON plan_file_diagnostics(request_id, id) WHERE request_id IS NOT NULL;

CREATE TABLE reports (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    frozen_event_id INTEGER NOT NULL CHECK (frozen_event_id >= 0),
    from_wm INTEGER NOT NULL CHECK (from_wm >= 0),
    to_wm INTEGER NOT NULL CHECK (to_wm >= from_wm),
    format_version INTEGER NOT NULL CHECK (format_version = 1),
    status INTEGER NOT NULL CHECK (status IN (1,2,3,4,5)),
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    sha256 TEXT CHECK (sha256 IS NULL OR (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*')),
    publication_count INTEGER NOT NULL CHECK (publication_count >= 0),
    last_published_event_id INTEGER REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    last_error_json TEXT CHECK (last_error_json IS NULL OR (json_valid(last_error_json) AND json_type(last_error_json) = 'object')),
    created_event_id INTEGER NOT NULL REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    last_event_id INTEGER NOT NULL REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    CHECK ((size_bytes IS NULL) = (sha256 IS NULL)),
    CHECK (status NOT IN (2,3,4) OR sha256 IS NOT NULL),
    CHECK (status <> 4 OR publication_count > 0),
    CHECK (status <> 5 OR last_error_json IS NOT NULL),
    CHECK ((publication_count = 0 AND last_published_event_id IS NULL)
        OR (publication_count > 0 AND last_published_event_id IS NOT NULL)),
    CHECK (last_event_id >= created_event_id),
    CHECK (frozen_event_id < created_event_id)
) STRICT;
CREATE INDEX reports_coverage ON reports(to_wm, id);

CREATE TABLE state_syncs (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    action_id INTEGER NOT NULL UNIQUE REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,
    mode INTEGER NOT NULL CHECK (mode IN (1,2)),
    after_report_id INTEGER REFERENCES reports(id) DEFERRABLE INITIALLY DEFERRED,
    from_wm INTEGER NOT NULL CHECK (from_wm >= 0),
    started_boundary_event_id INTEGER NOT NULL REFERENCES history_transactions(last_event_id) DEFERRABLE INITIALLY DEFERRED,
    status INTEGER NOT NULL CHECK (status IN (1,2,3)),
    local_report_id INTEGER REFERENCES reports(id) DEFERRABLE INITIALLY DEFERRED,
    ack_report_id INTEGER REFERENCES reports(id) DEFERRABLE INITIALLY DEFERRED,
    ended_event_id INTEGER REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    CHECK ((mode = 1 AND after_report_id IS NULL AND from_wm = 0) OR (mode = 2 AND after_report_id IS NOT NULL)),
    CHECK ((status = 1 AND ack_report_id IS NULL AND ended_event_id IS NULL)
        OR (status = 2 AND ack_report_id IS NOT NULL AND ended_event_id IS NOT NULL)
        OR (status = 3 AND ack_report_id IS NULL AND ended_event_id IS NOT NULL))
) STRICT;
CREATE INDEX syncs_outstanding ON state_syncs(from_wm, id) WHERE status = 1;

CREATE TABLE runtime_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    acknowledged_wm INTEGER NOT NULL CHECK (acknowledged_wm >= 0),
    acknowledged_report_id INTEGER REFERENCES reports(id) DEFERRABLE INITIALLY DEFERRED,
    trusted_time_lower_bound INTEGER,
    trusted_time_event_id INTEGER REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    cleanup_cursor_file_id INTEGER REFERENCES intermediate_files(id) DEFERRABLE INITIALLY DEFERRED,
    CHECK ((trusted_time_lower_bound IS NULL) = (trusted_time_event_id IS NULL)),
    CHECK (acknowledged_report_id IS NOT NULL OR acknowledged_wm = 0)
) STRICT;

CREATE TABLE database_metadata (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    application_id TEXT NOT NULL CHECK (application_id = 'camctl'),
    instance_id TEXT NOT NULL CHECK (length(instance_id) = 32 AND instance_id NOT GLOB '*[^0-9a-f]*'),
    format_version INTEGER NOT NULL CHECK (format_version = 1),
    staging_path TEXT NOT NULL CHECK (length(staging_path) > 1 AND substr(staging_path, 1, 1) = '/' AND instr(staging_path, char(0)) = 0),
    ready_path TEXT NOT NULL CHECK (length(ready_path) > 1 AND substr(ready_path, 1, 1) = '/' AND instr(ready_path, char(0)) = 0),
    processing_path TEXT NOT NULL CHECK (length(processing_path) > 1 AND substr(processing_path, 1, 1) = '/' AND instr(processing_path, char(0)) = 0),
    CHECK (staging_path <> ready_path AND staging_path <> processing_path AND ready_path <> processing_path)
) STRICT;
