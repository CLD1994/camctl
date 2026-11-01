#include "update_result.h"
#include "yyjson.h"
#include <stdlib.h>
#include <errno.h>
#include <string.h>

struct update_name { int value; const char *text; const char *description; };
struct update_combination { int action, outcome, effect, eligibility, prior_recovery; };
#include "update_protocol_generated.h"
#define COUNT(a) (sizeof(a) / sizeof((a)[0]))

static const char *description(int value, const struct update_name *names, size_t count) {
    for (size_t i = 0; i < count; ++i)
        if (names[i].value == value)
            return names[i].description;
    return "未知";
}
const char *camctl_update_effect_description(camctl_update_effect value) {
    return description(value, update_effect_names, COUNT(update_effect_names));
}
const char *camctl_update_eligibility_description(camctl_update_eligibility value) {
    return description(value, update_eligibility_names, COUNT(update_eligibility_names));
}

static yyjson_val *field(yyjson_val *object, const char *name) {
    yyjson_val *key, *value, *found = NULL;
    size_t index, count;
    yyjson_obj_foreach(object, index, count, key, value) {
        if (yyjson_equals_str(key, name)) {
            if (found)
                return NULL;
            found = value;
        }
    }
    return found;
}
static int named(yyjson_val *value, const struct update_name *names, size_t count) {
    if (!yyjson_is_str(value))
        return 0;
    const char *text = yyjson_get_str(value);
    size_t length = yyjson_get_len(value);
    for (size_t i = 0; i < count; ++i) {
        if (strlen(names[i].text) == length && !memcmp(text, names[i].text, length))
            return names[i].value;
    }
    return 0;
}
static bool text_field(yyjson_val *value, char *target, size_t *length, bool *present,
                       bool *truncated) {
    if (yyjson_is_null(value)) {
        *length = 0;
        *present = *truncated = false;
        target[0] = '\0';
        return true;
    }
    if (!yyjson_is_str(value))
        return false;
    *present = true;
    *length = yyjson_get_len(value);
    *truncated = *length > CAMCTL_HOST_UPDATE_TEXT_MAX;
    size_t copy = *truncated ? CAMCTL_HOST_UPDATE_TEXT_MAX : *length;
    memcpy(target, yyjson_get_str(value), copy);
    target[copy] = '\0';
    return true;
}
static bool permitted(host_update_result *r) {
    for (size_t i = 0; i < COUNT(update_combinations); ++i) {
        const struct update_combination *c = &update_combinations[i];
        if (c->action == (int)r->action && c->outcome == (int)r->outcome &&
            c->effect == (int)r->effect && c->eligibility == (int)r->eligibility &&
            c->prior_recovery == (int)r->prior_recovery)
            return true;
    }
    return false;
}
bool camctl_update_outcome_succeeded(camctl_update_outcome value) {
    for (size_t i = 0; i < COUNT(update_success_outcomes); ++i)
        if ((int)value == update_success_outcomes[i])
            return true;
    return false;
}
static bool success(host_update_result *r) {
    return r->eligibility == CAMCTL_UPDATE_ELIGIBILITY_ALLOWED &&
           camctl_update_outcome_succeeded(r->outcome);
}
bool host_update_result_parse(camctl_update_action expected, const char *data, size_t length,
                              host_exit termination, int exit_code, host_update_result *result) {
    if (!result)
        return false;
    memset(result, 0, sizeof(*result));
    result->action = expected;
    result->outcome = CAMCTL_UPDATE_OUTCOME_UNKNOWN;
    result->effect = CAMCTL_UPDATE_EFFECT_UNKNOWN;
    result->eligibility = CAMCTL_UPDATE_ELIGIBILITY_UNKNOWN;
    result->prior_recovery = CAMCTL_UPDATE_PRIOR_RECOVERY_UNKNOWN;
    if (!data || !length || termination != HOST_EXIT_NORMAL || data[length - 1] != '\n' ||
        memchr(data, '\n', length - 1))
        return false;
    size_t size = yyjson_read_max_memory_usage(length, 0);
    if (!size)
        return false;
    void *pool = malloc(size);
    yyjson_alc allocator;
    if (!pool) {
        result->parse_error = ENOMEM;
        return false;
    }
    if (!yyjson_alc_pool_init(&allocator, pool, size)) {
        free(pool);
        return false;
    }
    yyjson_doc *doc = yyjson_read_opts((char *)data, length, 0, &allocator, NULL);
    yyjson_val *root = yyjson_doc_get_root(doc);
    host_update_result *parsed = calloc(1, sizeof(*parsed));
    if (!parsed)
        result->parse_error = ENOMEM;
    bool valid = false;
    if (parsed && yyjson_is_obj(root) && yyjson_obj_size(root) == UPDATE_PROTOCOL_FIELDS) {
        yyjson_val *version = field(root, "version");
        parsed->action = named(field(root, "action"), update_action_names, COUNT(update_action_names));
        parsed->outcome = named(field(root, "outcome"), update_outcome_names, COUNT(update_outcome_names));
        parsed->effect = named(field(root, "effect"), update_effect_names, COUNT(update_effect_names));
        parsed->eligibility = named(field(root, "eligibility"), update_eligibility_names, COUNT(update_eligibility_names));
        parsed->prior_recovery = named(field(root, "prior_recovery"), update_prior_recovery_names,
                                      COUNT(update_prior_recovery_names));
        valid = yyjson_is_int(version) && yyjson_get_int(version) == UPDATE_PROTOCOL_VERSION && parsed->action == expected &&
            text_field(field(root, "reason"), parsed->reason, &parsed->reason_length, &parsed->reason_present,
                       &parsed->reason_truncated) &&
            text_field(field(root, "stage"), parsed->stage, &parsed->stage_length, &parsed->stage_present,
                       &parsed->stage_truncated) &&
            text_field(field(root, "evidence"), parsed->evidence, &parsed->evidence_length, &parsed->evidence_present,
                       &parsed->evidence_truncated) &&
            permitted(parsed) && exit_code == (success(parsed) ? UPDATE_SUCCESS_EXIT : UPDATE_FAILURE_EXIT);
    }
    if (valid)
        *result = *parsed;
    free(parsed);
    yyjson_doc_free(doc);
    free(pool);
    return valid;
}
