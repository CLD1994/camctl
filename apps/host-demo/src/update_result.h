#ifndef HOST_UPDATE_RESULT_H
#define HOST_UPDATE_RESULT_H
#include "camctl_host.h"
#include "camctl_update_protocol.h"
#include "result.h"
#include <stdbool.h>
#include <stddef.h>

#define CAMCTL_HOST_UPDATE_STDOUT_MAX (64 * 1024)
#define CAMCTL_HOST_UPDATE_TEXT_MAX 4095
#define CAMCTL_HOST_UPDATE_STDERR_MAX 8192
#define CAMCTL_HOST_UPDATE_PROCESS_MAX 8192
#define CAMCTL_HOST_UPDATE_REASON_MAX 255

typedef struct camctl_host_update_config {
    const char *camctl_path, *updater_path, *config_path;
} camctl_host_update_config;
typedef struct camctl_host_update_request {
    camctl_update_action action;
    const char *bundle_path, *client_data_path;
} camctl_host_update_request;
typedef struct camctl_host_update_result {
    camctl_update_outcome outcome;
    camctl_update_effect effect;
    camctl_update_eligibility eligibility;
    char reason[CAMCTL_HOST_UPDATE_REASON_MAX + 1];
    char diagnostic_path[CAMCTL_HOST_PATH_MAX + 1];
} camctl_host_update_result;

typedef struct host_update_result {
    camctl_update_action action;
    camctl_update_outcome outcome;
    camctl_update_effect effect;
    camctl_update_eligibility eligibility;
    camctl_update_prior_recovery prior_recovery;
    char reason[CAMCTL_HOST_UPDATE_TEXT_MAX + 1], stage[CAMCTL_HOST_UPDATE_TEXT_MAX + 1];
    char evidence[CAMCTL_HOST_UPDATE_TEXT_MAX + 1];
    char prior_evidence[CAMCTL_HOST_UPDATE_TEXT_MAX + 1];
    size_t reason_length, stage_length, evidence_length;
    bool reason_present, stage_present, evidence_present;
    bool reason_truncated, stage_truncated, evidence_truncated;
    size_t prior_evidence_length;
    bool prior_evidence_present, prior_evidence_truncated;
    char stderr_data[CAMCTL_HOST_UPDATE_STDERR_MAX + 1];
    size_t stderr_length;
    uint64_t stderr_bytes_seen;
    bool stderr_truncated, stderr_io_error;
    char process_diagnostics[CAMCTL_HOST_UPDATE_PROCESS_MAX + 1];
    size_t process_diagnostics_length;
    bool process_diagnostics_truncated;
    char stdout_data[CAMCTL_HOST_UPDATE_STDOUT_MAX + 1];
    size_t stdout_length;
    uint64_t stdout_bytes_seen;
    bool stdout_truncated, stdout_io_error;
    bool process_signaled, process_exit_known;
    int process_exit_code, poll_error, parse_error;
    bool process_started;
    int spawn_errno, working_directory_errno;
    long pid, pgid;
    char working_directory[CAMCTL_HOST_PATH_MAX + 1];
} host_update_result;

typedef struct {
    bool attempted;
    int original_error, cleanup_error;
    int close_errors[32], close_fds[32];
    const char *cleanup_operations[32];
    size_t close_error_count;
} host_construction_report;

void host_update_result_project(const host_update_result *detail, int error, camctl_host_update_result *result);
void host_update_result_deliver(const host_update_result *detail, camctl_update_action action, int error);
void host_update_diagnostic(const char *processing, const char *bundle, const char *config,
                            const host_update_result *apply, int apply_error,
                            const host_update_result *recovery, int recovery_error,
                            int run_error, const host_construction_report *construction);

/* 对不完整、非法、重复、信号退出或矛盾结果保留未知，不从退出码推断效果。 */
bool host_update_result_parse(camctl_update_action expected, const char *data, size_t length,
                              host_exit termination, int exit_code, host_update_result *result);
#endif
