#ifndef HOST_PROCESS_H
#define HOST_PROCESS_H
#include "result.h"
#include "camctl_host.h"
#include <sys/types.h>
#include <stdint.h>
typedef enum { HOST_EXEC_UNKNOWN, HOST_EXEC_OBSERVED } host_exec_evidence;
typedef struct {
    pid_t pid;
    int out_fd, err_fd;
    char *output;
    size_t length, capacity;
    bool overflow, io_error, ended, wait_fault;
    host_exec_evidence execution;
    host_exit termination;
    int exit_code;
    uint64_t id;
} host_child;
typedef void (*host_child_log)(void *context, const char *stream, const char *data, size_t length);
void host_child_init(host_child *c, char *buffer, size_t capacity);
int host_child_spawn(host_child *c, const camctl_host_config *config, host_command command,
                     const char *plan);
void host_child_collect(host_child *c, host_child_log log, void *context);
bool host_child_done(const host_child *c);
int host_pipe(int fds[2]);
#endif
