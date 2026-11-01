#include "maintenance.h"
#include "config.h"
#include "process.h"
#include "update_result.h"
#include <errno.h>
#include <poll.h>
#include <stdlib.h>
#include <string.h>


typedef struct {
    char data[CAMCTL_HOST_UPDATE_PROCESS_MAX + 1];
    size_t length;
    bool truncated;
} process_diagnostics;

static void process_log(void *context, const char *stream, const char *data, size_t length) {
    process_diagnostics *out = context;
    if (!strcmp(stream, "stderr") || !strcmp(stream, "exit_observed") ||
        !strcmp(stream, "group_reaped"))
        return;
    const char *parts[] = {stream, ": ", data, "\n"};
    size_t lengths[] = {strlen(stream), 2, length, 1};
    for (size_t i = 0; i < 4; ++i) {
        size_t available = CAMCTL_HOST_UPDATE_PROCESS_MAX - out->length;
        size_t copy = lengths[i] < available ? lengths[i] : available;
        memcpy(out->data + out->length, parts[i], copy);
        out->length += copy;
        if (copy < lengths[i])
            out->truncated = true;
    }
    out->data[out->length] = '\0';
}

int host_maintenance_validate(const camctl_host_update_config *config,
                              const camctl_host_update_request *request, bool allow_check) {
    if (!config || !request)
        return EINVAL;
    int error = host_path_validate(config->camctl_path);
    if (!error)
        error = host_path_validate(config->updater_path);
    if (!error && config->config_path)
        error = host_path_validate(config->config_path);
    if (error)
        return error;
    if (request->action == CAMCTL_UPDATE_ACTION_APPLY) {
        error = host_path_validate(request->bundle_path);
        if (!error && request->client_data_path)
            error = host_path_validate(request->client_data_path);
        return error;
    }
    if ((request->action == CAMCTL_UPDATE_ACTION_RECOVER ||
         (allow_check && (request->action == CAMCTL_UPDATE_ACTION_CHECK ||
                          request->action == CAMCTL_UPDATE_ACTION_FINALIZE))) &&
        !request->bundle_path && !request->client_data_path)
        return 0;
    return EINVAL;
}

void host_maintenance_result_init(host_update_result *result, camctl_update_action action) {
    memset(result, 0, sizeof(*result));
    result->action = action;
    result->outcome = CAMCTL_UPDATE_OUTCOME_NOT_STARTED;
    result->effect = CAMCTL_UPDATE_EFFECT_NOT_STARTED;
    result->eligibility = CAMCTL_UPDATE_ELIGIBILITY_UNKNOWN;
    result->prior_recovery = CAMCTL_UPDATE_PRIOR_RECOVERY_NONE;
}

int host_maintenance_run_interruptible(const camctl_host_update_config *config,
                         const camctl_host_update_request *request, host_update_result *result,
                         const atomic_bool *stop) {
    int error = host_maintenance_validate(config, request, true);
    if (!result)
        return EINVAL;
    host_maintenance_result_init(result, request ? request->action : CAMCTL_UPDATE_ACTION_INVALID);
    if (error)
        return error;
    if (stop && atomic_load(stop))
        return ECANCELED;
    char *output = malloc(CAMCTL_HOST_UPDATE_STDOUT_MAX);
    char *stderr_data = malloc(CAMCTL_HOST_UPDATE_STDERR_MAX);
    if (!output || !stderr_data) {
        free(output);
        free(stderr_data);
        return ENOMEM;
    }
    process_diagnostics diagnostics = {0};
    host_child child;
    host_child_init(&child, output, CAMCTL_HOST_UPDATE_STDOUT_MAX, stderr_data, CAMCTL_HOST_UPDATE_STDERR_MAX);
    error = host_maintenance_spawn(&child, config, request);
    if (error) {
        result->spawn_errno = error;
        result->working_directory_errno = child.working_directory_errno;
        memcpy(result->working_directory, child.working_directory, sizeof(result->working_directory));
        free(output);
        free(stderr_data);
        return error;
    }
    int poll_error = 0;
    while (!host_child_done(&child)) {
        if (stop && atomic_load(stop))
            host_child_request_stop(&child, process_log, &diagnostics);
        host_child_collect(&child, process_log, &diagnostics);
        if (host_child_done(&child))
            break;
        struct pollfd descriptors[2];
        nfds_t count = 0;
        if (child.out_fd >= 0)
            descriptors[count++] = (struct pollfd){child.out_fd, POLLIN, 0};
        if (child.err_fd >= 0)
            descriptors[count++] = (struct pollfd){child.err_fd, POLLIN, 0};
        if (poll(descriptors, count, 50) < 0 && errno != EINTR) {
            if (!poll_error)
                poll_error = errno;
            (void)poll(NULL, 0, 50);
        }
    }
    bool valid = !child.overflow && !child.io_error && !child.wait_fault &&
        host_update_result_parse(request->action, child.output, child.length, child.termination,
                                 child.exit_code, result);
    int parse_error = result->parse_error;
    if (!valid) {
        host_maintenance_result_init(result, request->action);
        result->parse_error = parse_error;
        result->outcome = CAMCTL_UPDATE_OUTCOME_UNKNOWN;
        result->effect = CAMCTL_UPDATE_EFFECT_UNKNOWN;
        result->prior_recovery = CAMCTL_UPDATE_PRIOR_RECOVERY_UNKNOWN;
    }
    memcpy(result->stderr_data, stderr_data, child.stderr_length);
    result->stderr_data[child.stderr_length] = '\0';
    result->stderr_length = child.stderr_length;
    result->stderr_bytes_seen = child.stderr_bytes_seen;
    result->stderr_truncated = child.stderr_overflow;
    result->stderr_io_error = child.stderr_io_error;
    memcpy(result->process_diagnostics, diagnostics.data, diagnostics.length + 1);
    result->process_diagnostics_length = diagnostics.length;
    result->process_diagnostics_truncated = diagnostics.truncated;
    memcpy(result->stdout_data, output, child.length);
    result->stdout_data[child.length] = '\0';
    result->stdout_length = child.length;
    result->stdout_bytes_seen = child.output_bytes_seen;
    result->stdout_truncated = child.overflow;
    result->stdout_io_error = child.io_error;
    result->process_exit_known = child.termination != HOST_EXIT_UNKNOWN;
    result->process_signaled = child.termination == HOST_EXIT_SIGNAL;
    result->process_exit_code = child.exit_code;
    result->process_started = child.started;
    result->spawn_errno = child.spawn_errno;
    result->pid = child.completed_pid ? child.completed_pid : child.pid;
    result->pgid = child.completed_pgid ? child.completed_pgid : child.pgid;
    result->working_directory_errno = child.working_directory_errno;
    memcpy(result->working_directory, child.working_directory, sizeof(result->working_directory));
    result->poll_error = poll_error;
    host_group_scan_reset(&child.scan);
    free(output);
    free(stderr_data);
    return valid ? 0 : parse_error ? parse_error : EPROTO;
}

int host_maintenance_run(const camctl_host_update_config *config,
                         const camctl_host_update_request *request, host_update_result *result) {
    return host_maintenance_run_interruptible(config, request, result, NULL);
}
