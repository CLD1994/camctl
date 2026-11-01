/* 用进程边界替身检验当前进程中的原始通道事实；不创建线程、管道或子进程。 */
#include "maintenance.h"
#include "process.h"
#include <assert.h>
#include <errno.h>
#include <poll.h>
#include <string.h>

static const char *stdout_text;
static const char *stderr_text;
static bool stdout_overflow, stderr_overflow, stderr_error;
static host_exit termination = HOST_EXIT_NORMAL;
static int exit_code;

int host_path_validate(const char *path) { return path && path[0] == '/' ? 0 : EINVAL; }
void host_child_init(host_child *c, char *out, size_t out_capacity, char *err, size_t err_capacity) {
    memset(c, 0, sizeof(*c));
    c->output = out; c->capacity = out_capacity;
    c->stderr_output = err; c->stderr_capacity = err_capacity;
    c->out_fd = c->err_fd = c->notification_fd = -1;
}
int host_maintenance_spawn(host_child *c, const camctl_host_update_config *config,
                           const camctl_host_update_request *request) {
    (void)config; (void)request; c->pid = 10; return 0;
}
void host_child_collect(host_child *c, host_child_log log, void *context) {
    c->length = strlen(stdout_text);
    memcpy(c->output, stdout_text, c->length);
    c->output_bytes_seen = c->length + (stdout_overflow ? 5 : 0);
    c->overflow = stdout_overflow;
    c->stderr_length = strlen(stderr_text);
    memcpy(c->stderr_output, stderr_text, c->stderr_length);
    c->stderr_bytes_seen = c->stderr_length + (stderr_overflow ? 3 : 0);
    c->stderr_overflow = stderr_overflow;
    c->stderr_io_error = stderr_error;
    c->termination = termination;
    c->exit_code = exit_code;
    c->reaped = true;
    log(context, "child_problem", "detail", 6);
}
bool host_child_done(const host_child *c) { return c->reaped; }
void host_group_scan_reset(host_group_scan *scan) { (void)scan; }
int poll(struct pollfd *fds, nfds_t n, int timeout) {
    (void)fds; (void)n; (void)timeout; assert(!"不得调用真实等待"); return -1;
}

int main(void) {
    const char *valid = "{\"version\":1,\"action\":\"check\",\"outcome\":\"unchanged\","
        "\"effect\":\"unchanged\",\"eligibility\":\"allowed\",\"prior_recovery\":\"none\","
        "\"reason\":null,\"stage\":null,\"evidence\":null}\n";
    const char *multiple = "{\"bad\":true}\n{\"bad\":true}\n";
    camctl_host_update_config config = {"/camctl", "/updater", NULL};
    camctl_host_update_request request = {CAMCTL_UPDATE_ACTION_CHECK, NULL, NULL};
    camctl_host_update_result result;
    config.config_path = "relative.toml";
    assert(host_maintenance_validate(&config, &request, true) == EINVAL);
    config.config_path = "/config.toml";
    assert(host_maintenance_validate(&config, &request, true) == 0);
    stdout_text = valid; stderr_text = "diagnostic"; stderr_overflow = true; stderr_error = true;
    assert(host_maintenance_run(&config, &request, &result) == 0);
    assert(result.eligibility == CAMCTL_UPDATE_ELIGIBILITY_ALLOWED);
    assert(result.stderr_truncated && result.stderr_io_error && result.stderr_bytes_seen == 13);
    assert(result.stderr_length == 10 && !memcmp(result.stderr_data, "diagnostic", 10));
    assert(strstr(result.process_diagnostics, "child_problem: detail") != NULL);
    stdout_text = multiple; stderr_text = ""; stderr_overflow = stderr_error = false;
    assert(host_maintenance_run(&config, &request, &result) == EPROTO);
    assert(result.outcome == CAMCTL_UPDATE_OUTCOME_UNKNOWN && result.effect == CAMCTL_UPDATE_EFFECT_UNKNOWN);
    assert(result.stdout_length == strlen(multiple) && !memcmp(result.stdout_data, multiple, result.stdout_length));
    stdout_text = valid; stdout_overflow = true;
    assert(host_maintenance_run(&config, &request, &result) == EPROTO);
    assert(result.stdout_truncated && result.stdout_bytes_seen == strlen(valid) + 5);
    assert(result.stdout_length == strlen(valid) && !memcmp(result.stdout_data, valid, result.stdout_length));
    stdout_overflow = false; termination = HOST_EXIT_SIGNAL;
    assert(host_maintenance_run(&config, &request, &result) == EPROTO);
    assert(result.process_signaled && result.effect == CAMCTL_UPDATE_EFFECT_UNKNOWN);
    return 0;
}
