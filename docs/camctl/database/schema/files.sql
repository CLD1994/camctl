-- 格式 1：文件身份、正式产物、交付和拷贝。
CREATE TABLE device_files (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    observer_action_id INTEGER NOT NULL REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,
    source_action_id INTEGER REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,
    identity_key TEXT NOT NULL,
    locator_json TEXT NOT NULL CHECK (json_valid(locator_json) AND json_type(locator_json) = 'object'),
    ownership_evidence_json TEXT CHECK (ownership_evidence_json IS NULL OR (json_valid(ownership_evidence_json) AND json_type(ownership_evidence_json) = 'object')),
    original_name TEXT,
    media_type TEXT,
    role INTEGER NOT NULL CHECK (role IN (1,2,3)),
    original_device_file_id INTEGER REFERENCES device_files(id) DEFERRABLE INITIALLY DEFERRED,
    pairing_evidence_json TEXT CHECK (pairing_evidence_json IS NULL OR (json_valid(pairing_evidence_json) AND json_type(pairing_evidence_json) = 'object')),
    presence_state INTEGER NOT NULL CHECK (presence_state IN (1,2,3)),
    completion_state INTEGER NOT NULL CHECK (completion_state IN (1,2,3,4)),
    completion_evidence_json TEXT CHECK (completion_evidence_json IS NULL OR (json_valid(completion_evidence_json) AND json_type(completion_evidence_json) = 'object')),
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    checksum_support INTEGER NOT NULL CHECK (checksum_support IN (1,2,3)),
    sha256 TEXT CHECK (sha256 IS NULL OR (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*')),
    last_error_json TEXT CHECK (last_error_json IS NULL OR (json_valid(last_error_json) AND json_type(last_error_json) = 'object')),
    created_event_id INTEGER NOT NULL REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    last_event_id INTEGER NOT NULL REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    change_count INTEGER NOT NULL CHECK (change_count > 0),
    UNIQUE (identity_key),
    CHECK (last_event_id >= created_event_id),
    CHECK ((source_action_id IS NULL) = (ownership_evidence_json IS NULL)),
    CHECK ((original_device_file_id IS NULL) = (pairing_evidence_json IS NULL)),
    CHECK (original_device_file_id IS NULL OR (role = 3 AND original_device_file_id <> id)),
    CHECK (completion_state <> 3 OR (size_bytes IS NOT NULL AND completion_evidence_json IS NOT NULL)),
    CHECK (completion_state = 3 OR size_bytes IS NULL),
    CHECK (completion_state <> 4 OR last_error_json IS NOT NULL),
    CHECK (sha256 IS NULL OR (completion_state = 3 AND checksum_support = 2))
) STRICT;
CREATE INDEX device_files_source ON device_files(source_action_id, id);
CREATE INDEX device_files_observer ON device_files(observer_action_id, id);
CREATE INDEX device_file_previews ON device_files(original_device_file_id, id) WHERE original_device_file_id IS NOT NULL;

CREATE TABLE intermediate_files (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    owner_action_id INTEGER REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,
    owner_delivery_id INTEGER REFERENCES deliveries(id) DEFERRABLE INITIALLY DEFERRED,
    purpose INTEGER NOT NULL CHECK (purpose IN (1,2,3,4)),
    relative_path TEXT NOT NULL UNIQUE CHECK (length(relative_path) > 0),
    retention_state INTEGER NOT NULL CHECK (retention_state IN (1,2,3,4)),
    cleanup_state INTEGER NOT NULL CHECK (cleanup_state IN (1,2,3,4,5,6)),
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    sha256 TEXT CHECK (sha256 IS NULL OR (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*')),
    last_error_json TEXT CHECK (last_error_json IS NULL OR (json_valid(last_error_json) AND json_type(last_error_json) = 'object')),
    created_event_id INTEGER NOT NULL REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    last_event_id INTEGER NOT NULL REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    change_count INTEGER NOT NULL CHECK (change_count > 0),
    CHECK (last_event_id >= created_event_id),
    CHECK ((owner_action_id IS NOT NULL) + (owner_delivery_id IS NOT NULL) = 1),
    CHECK ((purpose = 1 AND owner_delivery_id IS NOT NULL) OR (purpose <> 1 AND owner_action_id IS NOT NULL)),
    CHECK (retention_state IN (2, 3) OR cleanup_state = 1),
    CHECK (retention_state <> 2 OR cleanup_state <> 1),
    CHECK (cleanup_state NOT IN (5,6) OR last_error_json IS NOT NULL),
    CHECK (sha256 IS NULL OR size_bytes IS NOT NULL)
) STRICT;
CREATE INDEX intermediate_action ON intermediate_files(owner_action_id, id) WHERE owner_action_id IS NOT NULL;
CREATE INDEX intermediate_delivery ON intermediate_files(owner_delivery_id, id) WHERE owner_delivery_id IS NOT NULL;
CREATE INDEX intermediate_cleanup ON intermediate_files(id) WHERE retention_state = 2 AND cleanup_state <> 4;

CREATE TABLE outputs (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    source_action_id INTEGER NOT NULL REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,
    kind INTEGER NOT NULL CHECK (kind IN (1,2,3)),
    device_file_id INTEGER UNIQUE REFERENCES device_files(id) DEFERRABLE INITIALLY DEFERRED,
    intermediate_file_id INTEGER UNIQUE REFERENCES intermediate_files(id) DEFERRABLE INITIALLY DEFERRED,
    original_name TEXT,
    media_type TEXT,
    availability INTEGER NOT NULL CHECK (availability IN (1,2,3,4,5)),
    cleanup_status INTEGER NOT NULL CHECK (cleanup_status IN (1,2,3,4,5,6)),
    cleanup_error_json TEXT CHECK (cleanup_error_json IS NULL OR (json_valid(cleanup_error_json) AND json_type(cleanup_error_json) = 'object')),
    media_json TEXT NOT NULL CHECK (json_valid(media_json) AND json_type(media_json) = 'object'),
    error_json TEXT CHECK (error_json IS NULL OR (json_valid(error_json) AND json_type(error_json) = 'object')),
    created_event_id INTEGER NOT NULL REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    last_event_id INTEGER NOT NULL REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    change_count INTEGER NOT NULL CHECK (change_count > 0),
    CHECK ((device_file_id IS NOT NULL) + (intermediate_file_id IS NOT NULL) = 1),
    CHECK (availability NOT IN (4,5) OR error_json IS NOT NULL),
    CHECK ((cleanup_status IN (1,6) AND availability IN (1,4,5))
        OR (cleanup_status IN (2,3,5) AND availability = 2)
        OR (cleanup_status = 4 AND availability = 3)),
    CHECK ((cleanup_status = 5 AND cleanup_error_json IS NOT NULL)
        OR (cleanup_status <> 5 AND cleanup_error_json IS NULL)),
    CHECK (last_event_id >= created_event_id)
) STRICT;
CREATE INDEX outputs_source ON outputs(source_action_id, id);

CREATE TABLE output_origins (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    output_id INTEGER NOT NULL UNIQUE REFERENCES outputs(id) DEFERRABLE INITIALLY DEFERRED,
    original_output_id INTEGER NOT NULL REFERENCES outputs(id) DEFERRABLE INITIALLY DEFERRED,
    CHECK (output_id <> original_output_id)
) STRICT;
CREATE INDEX origins_reverse ON output_origins(original_output_id, output_id);

CREATE TABLE deliveries (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    action_id INTEGER NOT NULL REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,
    output_id INTEGER NOT NULL REFERENCES outputs(id) DEFERRABLE INITIALLY DEFERRED,
    file_name TEXT NOT NULL UNIQUE CHECK (length(file_name) BETWEEN 3 AND 255),
    display_name TEXT NOT NULL,
    status INTEGER NOT NULL CHECK (status BETWEEN 1 AND 8),
    publication_intent_event_id INTEGER REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    published_event_id INTEGER REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    withdrawal_state INTEGER NOT NULL CHECK (withdrawal_state IN (1,2,3,4,5,6)),
    withdrawal_error_json TEXT CHECK (withdrawal_error_json IS NULL OR (json_valid(withdrawal_error_json) AND json_type(withdrawal_error_json) = 'object')),
    error_json TEXT CHECK (error_json IS NULL OR (json_valid(error_json) AND json_type(error_json) = 'object')),
    created_event_id INTEGER NOT NULL REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    last_event_id INTEGER NOT NULL REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    change_count INTEGER NOT NULL CHECK (change_count > 0),
    UNIQUE (action_id, output_id),
    CHECK (status NOT IN (4,5,8) OR publication_intent_event_id IS NOT NULL),
    CHECK (status <> 5 OR published_event_id IS NOT NULL),
    CHECK (status <> 6 OR error_json IS NOT NULL),
    CHECK (withdrawal_state NOT IN (5,6) OR withdrawal_error_json IS NOT NULL),
    CHECK (status <> 8 OR withdrawal_state = 3),
    CHECK (last_event_id >= created_event_id)
) STRICT;
CREATE INDEX delivery_action ON deliveries(action_id, id);
CREATE INDEX delivery_recovery ON deliveries(status, id) WHERE status IN (1,2,3,4);
CREATE INDEX delivery_withdrawals ON deliveries(withdrawal_state, id) WHERE withdrawal_state IN (2,6);

CREATE TABLE file_copies (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    delivery_id INTEGER UNIQUE REFERENCES deliveries(id) DEFERRABLE INITIALLY DEFERRED,
    processing_id INTEGER UNIQUE REFERENCES recording_processing(id) DEFERRABLE INITIALLY DEFERRED,
    source_device_file_id INTEGER REFERENCES device_files(id) DEFERRABLE INITIALLY DEFERRED,
    source_intermediate_file_id INTEGER REFERENCES intermediate_files(id) DEFERRABLE INITIALLY DEFERRED,
    target_file_id INTEGER NOT NULL UNIQUE REFERENCES intermediate_files(id) DEFERRABLE INITIALLY DEFERRED,
    round INTEGER NOT NULL CHECK (round >= 1),
    recopies_used INTEGER NOT NULL CHECK (recopies_used >= 0),
    max_recopies_used INTEGER NOT NULL CHECK (max_recopies_used >= 0),
    source_size INTEGER NOT NULL CHECK (source_size >= 0),
    source_sha256 TEXT CHECK (source_sha256 IS NULL OR (length(source_sha256) = 64 AND source_sha256 NOT GLOB '*[^0-9a-f]*')),
    committed_bytes INTEGER NOT NULL CHECK (committed_bytes BETWEEN 0 AND source_size),
    reset_state INTEGER NOT NULL CHECK (reset_state IN (1,2)),
    slot_device_id TEXT UNIQUE CHECK (slot_device_id IS NULL OR length(slot_device_id) > 0),
    verification_state INTEGER NOT NULL CHECK (verification_state BETWEEN 1 AND 6),
    target_sha256 TEXT CHECK (target_sha256 IS NULL OR (length(target_sha256) = 64 AND target_sha256 NOT GLOB '*[^0-9a-f]*')),
    verification_error_json TEXT CHECK (verification_error_json IS NULL OR (json_valid(verification_error_json) AND json_type(verification_error_json) = 'object')),
    CHECK ((delivery_id IS NOT NULL) + (processing_id IS NOT NULL) = 1),
    CHECK ((source_device_file_id IS NOT NULL) + (source_intermediate_file_id IS NOT NULL) = 1),
    CHECK (source_intermediate_file_id IS NULL OR source_intermediate_file_id <> target_file_id),
    CHECK (round = recopies_used + 1),
    CHECK (verification_state NOT IN (3,4,5) OR (target_sha256 IS NOT NULL AND committed_bytes = source_size AND reset_state = 1)),
    CHECK (verification_state <> 3 OR (source_sha256 IS NOT NULL AND source_sha256 = target_sha256)),
    CHECK (verification_state <> 4 OR (source_sha256 IS NOT NULL AND source_sha256 <> target_sha256)),
    CHECK (verification_state <> 5 OR source_sha256 IS NULL),
    CHECK (verification_state <> 6 OR verification_error_json IS NOT NULL)
) STRICT;
