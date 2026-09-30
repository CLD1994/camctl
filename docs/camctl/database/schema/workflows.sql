-- 格式 1：来源、选择、读取保护与取消。字段解释及状态机见对应专题。
CREATE TABLE auto_preview_links (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    obtain_action_id INTEGER NOT NULL UNIQUE REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,
    source_action_id INTEGER REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,
    preview_support INTEGER CHECK (preview_support IS NULL OR preview_support IN (0,1)),
    parameter_type TEXT CHECK (parameter_type IS NULL OR length(parameter_type) > 0),
    is_valid INTEGER NOT NULL CHECK (is_valid IN (0,1)),
    CHECK (is_valid = 0 OR (source_action_id IS NOT NULL AND preview_support IS 1 AND parameter_type IS NOT NULL))
) STRICT;
CREATE UNIQUE INDEX one_valid_auto_preview ON auto_preview_links(source_action_id) WHERE is_valid = 1;
CREATE INDEX preview_cancel_targets ON auto_preview_links(source_action_id, obtain_action_id);

CREATE TABLE action_dependencies (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    action_id INTEGER NOT NULL REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,
    depends_on_action_id INTEGER NOT NULL REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,
    UNIQUE (action_id, depends_on_action_id),
    CHECK (action_id <> depends_on_action_id)
) STRICT;
CREATE INDEX dependency_members ON action_dependencies(action_id, id);
CREATE INDEX source_consumers ON action_dependencies(depends_on_action_id, action_id);

CREATE TABLE obtain_source_selections (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    dependency_id INTEGER NOT NULL UNIQUE REFERENCES action_dependencies(id) DEFERRABLE INITIALLY DEFERRED,
    status INTEGER NOT NULL CHECK (status IN (1,2)),
    error_code INTEGER CHECK (error_code IS NULL OR error_code IN (1,2)),
    error_details_json TEXT CHECK (error_details_json IS NULL OR (json_valid(error_details_json) AND json_type(error_details_json) = 'object')),
    CHECK ((error_code IS NULL) = (error_details_json IS NULL)),
    CHECK (status = 2 OR error_code IS NULL)
) STRICT;

CREATE TABLE obtain_items (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    selection_id INTEGER NOT NULL REFERENCES obtain_source_selections(id) DEFERRABLE INITIALLY DEFERRED,
    requested_output_id INTEGER CHECK (requested_output_id IS NULL OR requested_output_id > 0),
    output_id INTEGER REFERENCES outputs(id) DEFERRABLE INITIALLY DEFERRED,
    basis INTEGER NOT NULL CHECK (basis IN (1,2,3,4,5)),
    original_output_id INTEGER REFERENCES outputs(id) DEFERRABLE INITIALLY DEFERRED,
    preview_output_id INTEGER REFERENCES outputs(id) DEFERRABLE INITIALLY DEFERRED,
    preview_size INTEGER CHECK (preview_size IS NULL OR preview_size >= 0),
    repaired_size INTEGER CHECK (repaired_size IS NULL OR repaired_size >= 0),
    status INTEGER NOT NULL CHECK (status IN (1,2,3,4,5)),
    source_dependency INTEGER NOT NULL CHECK (source_dependency IN (0,1)),
    delivery_id INTEGER UNIQUE REFERENCES deliveries(id) DEFERRABLE INITIALLY DEFERRED,
    error_code INTEGER CHECK (error_code IS NULL OR error_code IN (1,2,3,4,5,6,8)),
    error_details_json TEXT CHECK (error_details_json IS NULL OR (json_valid(error_details_json) AND json_type(error_details_json) = 'object')),
    UNIQUE (selection_id, requested_output_id),
    UNIQUE (selection_id, output_id),
    CHECK (requested_output_id IS NOT NULL OR output_id IS NOT NULL OR (basis = 3 AND original_output_id IS NOT NULL AND status = 4 AND error_code = 8)),
    CHECK (source_dependency = 0 OR (delivery_id IS NOT NULL AND output_id IS NOT NULL)),
    CHECK (status NOT IN (2,3) OR output_id IS NOT NULL),
    CHECK ((preview_size IS NULL) = (repaired_size IS NULL)),
    CHECK (basis <> 4 OR (original_output_id IS NOT NULL AND preview_output_id IS NOT NULL AND preview_size IS NOT NULL AND repaired_size IS NOT NULL AND repaired_size <= preview_size)),
    CHECK (basis <> 2 OR original_output_id IS NOT NULL),
    CHECK (basis <> 3 OR (original_output_id IS NOT NULL AND
        ((output_id IS NOT NULL AND preview_output_id IS NOT NULL AND output_id = preview_output_id)
         OR (output_id IS NULL AND preview_output_id IS NULL AND status = 4 AND error_code IS 8)))),
    CHECK (basis NOT IN (1,5) OR original_output_id IS NULL),
    CHECK (basis <> 5 OR output_id IS NULL OR output_id = requested_output_id),
    CHECK (basis NOT IN (1,2,5) OR (preview_output_id IS NULL AND preview_size IS NULL AND repaired_size IS NULL)),
    CHECK ((status = 3 AND delivery_id IS NOT NULL AND output_id IS NOT NULL AND error_code IS NULL AND error_details_json IS NULL)
        OR (status <> 3 AND delivery_id IS NULL)),
    CHECK ((status = 4 AND error_code IS NOT NULL AND error_details_json IS NOT NULL)
        OR (status <> 4 AND error_code IS NULL AND error_details_json IS NULL)),
    CHECK ((basis = 5 AND requested_output_id IS NOT NULL) OR (basis <> 5 AND requested_output_id IS NULL)),
    CHECK (status <> 1 OR (basis = 5 AND output_id IS NULL))
) STRICT;
CREATE UNIQUE INDEX one_preview_choice ON obtain_items(selection_id, original_output_id) WHERE basis = 3;
CREATE INDEX output_readers ON obtain_items(output_id, id) WHERE source_dependency = 1;

CREATE TABLE cleanup_items (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    action_id INTEGER NOT NULL REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,
    requested_output_id INTEGER NOT NULL CHECK (requested_output_id > 0),
    output_id INTEGER REFERENCES outputs(id) DEFERRABLE INITIALLY DEFERRED,
    status INTEGER NOT NULL CHECK (status IN (1,2,3,4,5,6)),
    restriction_state INTEGER NOT NULL CHECK (restriction_state IN (1,2,3,4)),
    outcome INTEGER CHECK (outcome IS NULL OR outcome IN (1,2,3)),
    final_event_id INTEGER REFERENCES history_events(id) DEFERRABLE INITIALLY DEFERRED,
    error_code INTEGER CHECK (error_code IS NULL OR error_code BETWEEN 1 AND 6),
    error_details_json TEXT CHECK (error_details_json IS NULL OR (json_valid(error_details_json) AND json_type(error_details_json) = 'object')),
    UNIQUE (action_id, requested_output_id),
    CHECK (output_id IS NULL OR output_id = requested_output_id),
    CHECK (restriction_state = 1 OR output_id IS NOT NULL),
    CHECK (status NOT IN (2,3,4) OR output_id IS NOT NULL),
    CHECK ((error_code IS NULL) = (error_details_json IS NULL)),
    CHECK ((status = 4 AND outcome IS NOT NULL) OR (status <> 4 AND outcome IS NULL)),
    CHECK ((status IN (4,5,6) AND final_event_id IS NOT NULL AND final_event_id > 0)
        OR (status IN (1,2,3) AND final_event_id IS NULL)),
    CHECK ((status = 1 AND restriction_state = 1 AND error_code IS NULL)
        OR (status = 2 AND restriction_state = 2 AND error_code IS NULL)
        OR (status = 3 AND restriction_state IN (2,4) AND error_code IS NULL)
        OR (status = 4 AND restriction_state = 4 AND error_code IS NULL)
        OR (status = 5 AND restriction_state IN (1,2,4) AND error_code IS NOT NULL)
        OR (status = 6 AND restriction_state IN (1,3) AND error_code IS NULL)
        OR (status = 6 AND restriction_state = 4 AND error_code IS NOT NULL AND error_code IN (4,6)))
) STRICT;
CREATE INDEX output_delete_restrictions ON cleanup_items(output_id, id) WHERE restriction_state IN (2,4);
CREATE UNIQUE INDEX one_cleanup_holder ON cleanup_items(output_id) WHERE status = 3;
CREATE INDEX cleanup_action_members ON cleanup_items(action_id, id);
CREATE INDEX cleanup_final_errors ON cleanup_items(output_id, final_event_id DESC, id DESC)
    WHERE status IN (5,6) AND restriction_state IN (2,4);

CREATE TABLE cancel_items (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    action_id INTEGER NOT NULL REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,
    target_action_id INTEGER NOT NULL REFERENCES actions(id) DEFERRABLE INITIALLY DEFERRED,
    selection_basis INTEGER NOT NULL CHECK (selection_basis IN (1,2,3)),
    status INTEGER NOT NULL CHECK (status IN (1,2,3,4,5)),
    cancellation_effect INTEGER NOT NULL CHECK (cancellation_effect IN (1,2,3)),
    outcome INTEGER CHECK (outcome IS NULL OR outcome IN (1,2)),
    error_code INTEGER CHECK (error_code IS NULL OR error_code BETWEEN 1 AND 5),
    error_details_json TEXT CHECK (error_details_json IS NULL OR (json_valid(error_details_json) AND json_type(error_details_json) = 'object')),
    UNIQUE (action_id, target_action_id),
    CHECK (action_id <> target_action_id),
    CHECK ((status = 3 AND outcome IS NOT NULL) OR (status <> 3 AND outcome IS NULL)),
    CHECK ((status = 4 AND error_code IS NOT NULL AND error_details_json IS NOT NULL)
        OR (status <> 4 AND error_code IS NULL AND error_details_json IS NULL))
) STRICT;
CREATE INDEX cancel_target_waiters ON cancel_items(target_action_id, id) WHERE status IN (1,2);

CREATE TABLE cancel_delivery_items (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    cancel_item_id INTEGER NOT NULL REFERENCES cancel_items(id) DEFERRABLE INITIALLY DEFERRED,
    delivery_id INTEGER NOT NULL REFERENCES deliveries(id) DEFERRABLE INITIALLY DEFERRED,
    status INTEGER NOT NULL CHECK (status IN (1,2,3,4)),
    error_code INTEGER CHECK (error_code IS NULL OR error_code IN (1,2)),
    error_details_json TEXT CHECK (error_details_json IS NULL OR (json_valid(error_details_json) AND json_type(error_details_json) = 'object')),
    UNIQUE (cancel_item_id, delivery_id),
    CHECK ((status = 4 AND error_code IS NOT NULL AND error_details_json IS NOT NULL)
        OR (status <> 4 AND error_code IS NULL AND error_details_json IS NULL))
) STRICT;
CREATE INDEX cancel_delivery_waiters ON cancel_delivery_items(delivery_id, id) WHERE status = 1;
