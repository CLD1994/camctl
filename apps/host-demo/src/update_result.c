#include "update_result.h"
#include "diagnostic.h"
#include "yyjson.h"
#include <stdlib.h>
#include <errno.h>
#include <string.h>
#include <stdio.h>
#include <pthread.h>
#include <unistd.h>

static void diagnostic_evidence(FILE *out, const char *evidence, size_t length,
                                bool present, bool truncated) {
    if (!present || truncated || !length || evidence[0] != '/' || memchr(evidence, 0, length)) return;
    const char *names[] = {"update.log", "receipt.json"};
    for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
        char path[CAMCTL_HOST_UPDATE_TEXT_MAX + 32];
        int n = snprintf(path, sizeof(path), "%s/%s", evidence, names[i]);
        if (n < 0 || (size_t)n >= sizeof(path)) continue;
        FILE *source = fopen(path, "rb");
        if (!source) {
            fprintf(out, "evidence %s open_errno=%d\n", names[i], errno);
            continue;
        }
        fprintf(out, "evidence %s begin\n", names[i]);
        char bytes[8192];
        size_t count;
        while ((count = fread(bytes, 1, sizeof(bytes), source)) != 0)
            if (fwrite(bytes, 1, count, out) != count) break;
        fprintf(out, "\nevidence %s read_error=%d\n", names[i], ferror(source) != 0);
        fclose(source);
    }
}

static void diagnostic_result(FILE *out, const char *name, const host_update_result *d, int error) {
    fprintf(out, "%s error=%d present=%d\n", name, error, d != NULL);
    if (!d) return;
    fprintf(out, "action=%d outcome=%d effect=%d eligibility=%d prior_recovery=%d "
                 "started=%d pid=%ld pgid=%ld spawn_errno=%d cwd_errno=%d "
                 "exit_known=%d signaled=%d exit_code=%d poll_error=%d parse_error=%d\n",
            d->action, d->outcome, d->effect, d->eligibility, d->prior_recovery,
            d->process_started, d->pid, d->pgid, d->spawn_errno, d->working_directory_errno,
            d->process_exit_known, d->process_signaled, d->process_exit_code, d->poll_error, d->parse_error);
    fprintf(out, "cwd=%s\nstdout length=%zu seen=%llu truncated=%d io_error=%d\n",
            d->working_directory, d->stdout_length, (unsigned long long)d->stdout_bytes_seen,
            d->stdout_truncated, d->stdout_io_error);
    fwrite(d->stdout_data, 1, d->stdout_length, out);
    fprintf(out, "\nstderr length=%zu seen=%llu truncated=%d io_error=%d\n",
            d->stderr_length, (unsigned long long)d->stderr_bytes_seen,
            d->stderr_truncated, d->stderr_io_error);
    fwrite(d->stderr_data, 1, d->stderr_length, out);
    fprintf(out, "\nprocess length=%zu truncated=%d\n", d->process_diagnostics_length,
            d->process_diagnostics_truncated);
    fwrite(d->process_diagnostics, 1, d->process_diagnostics_length, out);
    fputc('\n', out);
    diagnostic_evidence(out, d->evidence, d->evidence_length, d->evidence_present, d->evidence_truncated);
    if (d->evidence_truncated || d->prior_evidence_truncated ||
        d->prior_evidence_length != d->evidence_length ||
        memcmp(d->prior_evidence, d->evidence, d->evidence_length))
        diagnostic_evidence(out, d->prior_evidence, d->prior_evidence_length,
                            d->prior_evidence_present, d->prior_evidence_truncated);
}

typedef struct {
    const char *bundle, *config;
    const host_update_result *apply, *recovery;
    int apply_error, recovery_error, run_error;
} update_diagnostic;

static int write_update_diagnostic(int fd, void *context) {
    update_diagnostic *d = context;
    int copy = dup(fd);
    if (copy < 0) return errno;
    FILE *out = fdopen(copy, "w");
    if (!out) {
        int error = errno;
        close(copy);
        return error;
    }
    fprintf(out, "CAMCTL-UPDATE-DIAGNOSTIC 1\nbundle=%s\nconfig=%s\nrun_error=%d\n",
            d->bundle ? d->bundle : "(none)", d->config ? d->config : "(default)", d->run_error);
    diagnostic_result(out, "apply", d->apply, d->apply_error);
    diagnostic_result(out, "recovery", d->recovery, d->recovery_error);
    int error = ferror(out) ? EIO : 0;
    if (fclose(out) && !error) error = errno;
    return error;
}

void host_update_diagnostic(const char *processing, const char *bundle, const char *config,
                            const host_update_result *apply, int apply_error,
                            const host_update_result *recovery, int recovery_error, int run_error) {
    if (!processing) return;
    update_diagnostic data = {bundle, config, apply, recovery, apply_error, recovery_error, run_error};
    int error = host_diagnostic_publish_update(processing, write_update_diagnostic, &data);
    if (error) fprintf(stderr, "camctl update diagnostic errno=%d\n", error);
}

void host_update_result_project(const host_update_result *d, int error, camctl_host_update_result *r) {
    memset(r, 0, sizeof(*r));
    r->outcome = d->outcome; r->effect = d->effect; r->eligibility = d->eligibility;
    if (d->reason_present) {
        size_t n = d->reason_length < CAMCTL_HOST_UPDATE_REASON_MAX ? d->reason_length : CAMCTL_HOST_UPDATE_REASON_MAX;
        const char *nul = memchr(d->reason, 0, n);
        if (nul) n = (size_t)(nul - d->reason);
        memcpy(r->reason, d->reason, n);
    } else if (error) {
        snprintf(r->reason, sizeof(r->reason), "errno=%d", error);
    }
    if (d->evidence_present && !d->evidence_truncated && d->evidence_length &&
        d->evidence[0] == '/' && !memchr(d->evidence, 0, d->evidence_length))
        memcpy(r->diagnostic_path, d->evidence, d->evidence_length);
}

void host_update_result_deliver(const host_update_result *d, camctl_update_action action, int error) {
    camctl_host_update_result projected;
    if (d) host_update_result_project(d, error, &projected);
    bool projection_lost = d && ((d->reason_present && (d->reason_truncated ||
        d->reason_length > CAMCTL_HOST_UPDATE_REASON_MAX || memchr(d->reason, 0, d->reason_length < CAMCTL_HOST_UPDATE_TEXT_MAX ? d->reason_length : CAMCTL_HOST_UPDATE_TEXT_MAX))) ||
        (d->evidence_present && !projected.diagnostic_path[0]));
    if (d && !error && camctl_update_outcome_succeeded(d->outcome) &&
        d->eligibility == CAMCTL_UPDATE_ELIGIBILITY_ALLOWED && !projection_lost &&
        !d->stderr_length && !d->stderr_truncated && !d->stderr_io_error &&
        !d->process_diagnostics_length && !d->process_diagnostics_truncated && !d->poll_error && !d->parse_error)
        return;
    static bool disabled;
    int saved = errno;
    int cancel_state;
    if (pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &cancel_state)) {
        errno = saved;
        return;
    }
    flockfile(stderr);
    if (disabled) goto done;
    if (ferror(stderr)) goto failed;
#define DIAG(...) do { if (fprintf(stderr, __VA_ARGS__) < 0) goto failed; } while (0)
#define RAW(label, data, length) do { DIAG("%s length=%zu\n", label, (size_t)(length)); \
    if ((length) && fwrite(data, 1, (length), stderr) != (length)) goto failed; \
    if (fputc('\n', stderr) == EOF) goto failed; } while (0)
    if (!d) {
        DIAG("camctl maintenance action=%d not_started errno=%d\n", action, error);
        if (fflush(stderr)) goto failed;
        goto done;
    }
    DIAG("camctl maintenance action=%d outcome=%d effect=%d eligibility=%d prior_recovery=%d errno=%d exit_known=%d signaled=%d exit_code=%d poll_error=%d parse_error=%d\n",
         d->action, d->outcome, d->effect, d->eligibility, d->prior_recovery, error,
         d->process_exit_known, d->process_signaled, d->process_exit_code, d->poll_error, d->parse_error);
    DIAG("stdout seen=%llu truncated=%d io_error=%d stderr seen=%llu truncated=%d io_error=%d process_truncated=%d projection_lost=%d\n",
         (unsigned long long)d->stdout_bytes_seen, d->stdout_truncated, d->stdout_io_error,
         (unsigned long long)d->stderr_bytes_seen, d->stderr_truncated, d->stderr_io_error,
         d->process_diagnostics_truncated, projection_lost);
    RAW("stdout", d->stdout_data, d->stdout_length);
    RAW("stderr", d->stderr_data, d->stderr_length);
    RAW("process", d->process_diagnostics, d->process_diagnostics_length);
    if (fflush(stderr)) goto failed;
    goto done;
failed:
    disabled = true;
done:
    funlockfile(stderr);
    errno = saved;
    (void)pthread_setcancelstate(cancel_state, NULL);
    errno = saved;
#undef RAW
#undef DIAG
}

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
            text_field(field(root, "prior_evidence"), parsed->prior_evidence, &parsed->prior_evidence_length,
                       &parsed->prior_evidence_present, &parsed->prior_evidence_truncated) &&
            permitted(parsed) && exit_code == (success(parsed) ? UPDATE_SUCCESS_EXIT : UPDATE_FAILURE_EXIT);
    }
    if (valid)
        *result = *parsed;
    free(parsed);
    yyjson_doc_free(doc);
    free(pool);
    return valid;
}
