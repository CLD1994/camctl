-- 格式 1：权威历史、查询目录和快照。语义见 ../history.md 与 ../history-formats.md。
CREATE TABLE history_transactions (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    operation_key TEXT NOT NULL UNIQUE CHECK (length(operation_key) = 32 AND operation_key NOT GLOB '*[^0-9a-f]*'),
    first_event_id INTEGER NOT NULL UNIQUE REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    last_event_id INTEGER NOT NULL UNIQUE REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    CHECK (first_event_id > 0 AND last_event_id >= first_event_id)
) STRICT;

CREATE TABLE history_events (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    transaction_id INTEGER NOT NULL REFERENCES history_transactions(id) DEFERRABLE INITIALLY DEFERRED,
-- 事件登记生成开始
    event_type INTEGER NOT NULL CHECK (event_type IN (1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33)),
-- 事件登记生成结束
    event_version INTEGER NOT NULL CHECK (event_version = 1),
    occurred_at INTEGER NOT NULL,
    clock_status INTEGER NOT NULL CHECK (clock_status IN (1,2,3)),
    change_seq INTEGER UNIQUE CHECK (change_seq IS NULL OR change_seq > 0),
    body_json TEXT NOT NULL CHECK (json_valid(body_json) AND json_type(body_json) = 'object'),
    UNIQUE (id, change_seq)
) STRICT;
CREATE INDEX history_events_transaction ON history_events(transaction_id, id);

CREATE TABLE entity_event_links (
    id INTEGER PRIMARY KEY CHECK (id > 0),
-- 登记生成开始：history_types
    entity_type INTEGER NOT NULL CHECK (entity_type IN (1,2,3,4,5,6,7,8,9,10)),
-- 登记生成结束：history_types
    entity_id INTEGER NOT NULL CHECK (entity_id > 0),
    event_id INTEGER NOT NULL REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    change_count INTEGER NOT NULL CHECK (change_count > 0),
    UNIQUE (entity_type, entity_id, event_id),
    UNIQUE (entity_type, entity_id, change_count)
) STRICT;
CREATE INDEX entity_links_event ON entity_event_links(event_id, entity_type, entity_id);

CREATE TABLE report_entity_changes (
    id INTEGER PRIMARY KEY CHECK (id > 0),
-- 登记生成开始：report_types
    entity_type INTEGER NOT NULL CHECK (entity_type IN (1,2,3,4,5)),
-- 登记生成结束：report_types
    entity_id INTEGER NOT NULL CHECK (entity_id > 0),
    event_id INTEGER NOT NULL,
    change_seq INTEGER NOT NULL CHECK (change_seq > 0),
    UNIQUE (entity_type, entity_id, event_id),
    FOREIGN KEY (event_id, change_seq) REFERENCES history_events(id, change_seq) DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE INDEX report_changes_by_sequence ON report_entity_changes(entity_type, change_seq, entity_id);
CREATE INDEX report_changes_by_entity ON report_entity_changes(entity_type, entity_id, change_seq);

CREATE TABLE entity_snapshots (
    id INTEGER PRIMARY KEY CHECK (id > 0),
-- 登记生成开始：snapshot_types
    entity_type INTEGER NOT NULL CHECK (entity_type IN (1,2,3,4,9,10)),
-- 登记生成结束：snapshot_types
    entity_id INTEGER NOT NULL CHECK (entity_id > 0),
    boundary_event_id INTEGER NOT NULL REFERENCES history_transactions(last_event_id) DEFERRABLE INITIALLY DEFERRED,
    change_count INTEGER NOT NULL CHECK (change_count > 0),
    format_version INTEGER NOT NULL CHECK (format_version = 1),
    content BLOB NOT NULL CHECK (length(content) > 0),
    UNIQUE (entity_type, entity_id, boundary_event_id)
) STRICT;

CREATE TABLE entity_snapshot_progress (
    id INTEGER PRIMARY KEY CHECK (id > 0),
-- 登记生成开始：progress_types
    entity_type INTEGER NOT NULL CHECK (entity_type IN (1,2,3,4,9,10)),
-- 登记生成结束：progress_types
-- 登记生成开始：snapshot_type_name
    entity_type_name TEXT GENERATED ALWAYS AS (CASE entity_type
        WHEN 1 THEN 'action'
        WHEN 2 THEN 'delivery'
        WHEN 3 THEN 'output'
        WHEN 4 THEN 'plan'
        WHEN 9 THEN 'device_file'
        WHEN 10 THEN 'intermediate_file'
    END) VIRTUAL,
-- 登记生成结束：snapshot_type_name
    entity_id INTEGER NOT NULL CHECK (entity_id > 0),
    current_change_count INTEGER NOT NULL CHECK (current_change_count > 0),
    snapshot_change_count INTEGER NOT NULL CHECK (snapshot_change_count >= 0),
    pending_changes INTEGER GENERATED ALWAYS AS (current_change_count - snapshot_change_count) STORED,
    latest_snapshot_id INTEGER REFERENCES entity_snapshots(id) DEFERRABLE INITIALLY DEFERRED,
    UNIQUE (entity_type, entity_id),
    CHECK (current_change_count >= snapshot_change_count),
    CHECK ((latest_snapshot_id IS NULL AND snapshot_change_count = 0)
        OR (latest_snapshot_id IS NOT NULL AND snapshot_change_count > 0))
) STRICT;
CREATE INDEX snapshot_candidates ON entity_snapshot_progress(pending_changes DESC, entity_type_name COLLATE BINARY, entity_id);
