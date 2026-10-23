#ifndef HOST_PROCESS_H
#define HOST_PROCESS_H
#include "result.h"
#include "camctl_host.h"
#include "process_group.h"
#include <sys/types.h>
#include <stdint.h>
#define HOST_DIAGNOSTIC_STDERR_CAPTURE_MAX (256 * 1024)
typedef enum { HOST_EXEC_UNKNOWN, HOST_EXEC_OBSERVED } host_exec_evidence;
typedef struct {
    pid_t pid, pgid;
    int out_fd, err_fd, notification_fd, notification_write_fd;
    bool notifications_enabled;
    char *output;
    size_t length, capacity;
    uint64_t output_bytes_seen;
    bool overflow, io_error, ended, reaped, wait_fault;
    char *stderr_output;
    size_t stderr_length, stderr_capacity;
    uint64_t stderr_bytes_seen;
    bool stderr_overflow, stderr_io_error;
    host_group_scan scan;
    int settlement_error;
    host_group_operation settlement_operation;
    host_exec_evidence execution;
    host_exit termination;
    int exit_code;
    uint64_t call_id, attempt_id;
    host_command command;
    bool automatic;
    uint32_t retries_used;
    char plan_path[CAMCTL_HOST_PATH_MAX + 1];
    char working_directory[CAMCTL_HOST_PATH_MAX + 1];
    int working_directory_errno;
    bool started;
    int spawn_errno;
    pid_t completed_pid, completed_pgid;
    host_result_kind result_kind;
    bool result_needs_run;
    bool submit_completion_pending;
    int submit_completion_error;
    enum { HOST_DIAGNOSTIC_IDLE, HOST_DIAGNOSTIC_PENDING, HOST_DIAGNOSTIC_QUEUED,
           HOST_DIAGNOSTIC_WRITING } diagnostic_state;
} host_child;
typedef void (*host_child_log)(void *context, const char *stream, const char *data, size_t length);
void host_child_init(host_child *c, char *output, size_t output_capacity, char *stderr_output,
                     size_t stderr_capacity);
int host_child_spawn(host_child *c, const camctl_host_config *config, host_command command,
                     const char *plan);
void host_child_collect(host_child *c, host_child_log log, void *context);
bool host_child_done(const host_child *c);
int host_pipe(int fds[2]);
int host_child_prepare_notifications(host_child *c);
void host_child_close_notifications(host_child *c);
int host_check_reaping_contract(void);
#endif
