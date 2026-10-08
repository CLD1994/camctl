#ifndef HOST_PROCESS_GROUP_H
#define HOST_PROCESS_GROUP_H
#include <dirent.h>
#include <stdbool.h>
#include <stddef.h>
#include <sys/types.h>
typedef enum { HOST_GROUP_PENDING, HOST_GROUP_LIVE, HOST_GROUP_COMPLETE, HOST_GROUP_UNKNOWN }
    host_group_status;
typedef enum {
    HOST_GROUP_OPEN_PROC, HOST_GROUP_READ_PROC, HOST_GROUP_OPEN_MEMBER,
    HOST_GROUP_READ_MEMBER, HOST_GROUP_OPEN_TASKS, HOST_GROUP_READ_TASKS,
    HOST_GROUP_READ_THREAD, HOST_GROUP_VERIFY_MEMBER
} host_group_operation;
typedef struct {
    host_group_status status;
    host_group_operation operation;
    int error;
    pid_t member;
} host_group_result;
typedef struct {
    DIR *processes, *tasks;
    int member_fd;
    pid_t member;
    unsigned long long start_time;
    unsigned threads, threads_seen;
    bool leader_seen, changed;
} host_group_scan;
typedef struct {
    pid_t pid, pgid;
    char state;
    unsigned threads;
    unsigned long long start_time;
} host_proc_stat;
int host_proc_stat_parse(const char *data, size_t length, pid_t pid, host_proc_stat *result);
void host_group_scan_init(host_group_scan *scan);
void host_group_scan_reset(host_group_scan *scan);
host_group_result host_group_scan_batch(host_group_scan *scan, pid_t pgid, size_t limit);
const char *host_group_operation_name(host_group_operation operation);
#endif
