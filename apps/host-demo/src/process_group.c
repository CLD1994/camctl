#include "process_group.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static bool space(char c) { return c == ' ' || c == '\n' || c == '\t'; }
static bool number(const char *begin, const char *end, unsigned long long max,
                   unsigned long long *value) {
    if (begin == end) return false;
    unsigned long long n = 0;
    for (const char *p = begin; p < end; p++) {
        if (*p < '0' || *p > '9' || n > (max - (unsigned)(*p - '0')) / 10)
            return false;
        n = n * 10 + (unsigned)(*p - '0');
    }
    *value = n;
    return true;
}
int host_proc_stat_parse(const char *data, size_t length, pid_t pid, host_proc_stat *result) {
    if (!data || !length || memchr(data, 0, length)) return EBADMSG;
    const char *end = data + length, *p = data, *close = NULL;
    while (p < end && *p >= '0' && *p <= '9') p++;
    unsigned long long identity;
    if (!number(data, p, INT_MAX, &identity) || identity != (unsigned)pid ||
        (size_t)(end - p) <= 1 || *p != ' ' || p[1] != '(') return EBADMSG;
    /* comm 可以包含空格和括号；字段从最后一个右括号之后开始。 */
    for (const char *q = p + 2; q < end; q++) if (*q == ')') close = q;
    if (!close || (size_t)(end - close) <= 4 || close[1] != ' ' || close[3] != ' ' ||
        !strchr("RSDZTtWXxKPI", close[2])) return EBADMSG;
    host_proc_stat value = {.pid = pid, .state = close[2]};
    p = close + 4;
    for (unsigned field = 4; field <= 22; field++) {
        while (p < end && space(*p)) p++;
        const char *begin = p;
        if (p < end && *p == '-') p++;
        const char *digits = p;
        while (p < end && *p >= '0' && *p <= '9') p++;
        if (digits == p || p == end || !space(*p)) return EBADMSG;
        if (field == 4 || field == 5 || field == 20 || field == 22) {
            unsigned long long n;
            if (begin != digits || !number(digits, p, field == 22 ? ULLONG_MAX : INT_MAX, &n))
                return EBADMSG;
            if (field == 5) value.pgid = (pid_t)n;
            if (field == 20) value.threads = (unsigned)n;
            if (field == 22) value.start_time = n;
        }
    }
    while (p < end && space(*p)) p++;
    if (p == end || !value.threads) return EBADMSG;
    *result = value;
    return 0;
}
void host_group_scan_init(host_group_scan *scan) {
    memset(scan, 0, sizeof(*scan));
    scan->member_fd = -1;
}
static void close_member(host_group_scan *scan) {
    if (scan->tasks) closedir(scan->tasks);
    if (scan->member_fd >= 0) close(scan->member_fd);
    scan->tasks = NULL;
    scan->member_fd = -1;
    scan->member = 0;
}
void host_group_scan_reset(host_group_scan *scan) {
    close_member(scan);
    if (scan->processes) closedir(scan->processes);
    host_group_scan_init(scan);
}
const char *host_group_operation_name(host_group_operation operation) {
    static const char *names[] = {"open_proc", "read_proc", "open_member", "read_member",
                                 "open_tasks", "read_tasks", "read_thread", "verify_member"};
    return names[operation];
}
static host_group_result report(host_group_scan *scan, host_group_status status,
                                host_group_operation operation, int error, pid_t member) {
    host_group_scan_reset(scan);
    return (host_group_result){status, operation, error, member};
}
static bool execution_ended(char state) { return state == 'Z' || state == 'X' || state == 'x'; }
static int owned_fd(int fd) {
    if (fd < 0 || fd >= 3) return fd;
    int moved = fcntl(fd, F_DUPFD_CLOEXEC, 3);
    int error = errno;
    close(fd);
    errno = error;
    return moved;
}
static int read_stat(int directory, const char *path, pid_t pid, host_proc_stat *stat) {
    int fd = owned_fd(openat(directory, path, O_RDONLY | O_CLOEXEC));
    if (fd < 0) return errno;
    char data[4096];
    size_t length = 0;
    int error = 0;
    while (length < sizeof(data)) {
        ssize_t n = read(fd, data + length, sizeof(data) - length);
        if (n < 0) { error = errno; break; }
        if (!n) break;
        length += (size_t)n;
    }
    if (!error && length == sizeof(data)) error = EOVERFLOW;
    close(fd);
    return error ? error : host_proc_stat_parse(data, length, pid, stat);
}
static pid_t numeric_name(const char *name) {
    unsigned long long value;
    return number(name, name + strlen(name), INT_MAX, &value) && value ? (pid_t)value : 0;
}
host_group_result host_group_scan_batch(host_group_scan *scan, pid_t pgid, size_t limit) {
    if (!scan->processes) {
        int fd = owned_fd(openat(AT_FDCWD, "/proc", O_RDONLY | O_DIRECTORY | O_CLOEXEC));
        if (fd < 0)
            return report(scan, HOST_GROUP_UNKNOWN, HOST_GROUP_OPEN_PROC, errno, 0);
        scan->processes = fdopendir(fd);
        if (!scan->processes)
        {
            int error = errno;
            close(fd);
            return report(scan, HOST_GROUP_UNKNOWN, HOST_GROUP_OPEN_PROC, error, 0);
        }
    }
    /* 同一配额用于进程目录、线程目录和成员复核，每批不保存成员清单。 */
    for (size_t used = 0; used < limit; used++) {
        bool tasks = scan->tasks != NULL;
        errno = 0;
        struct dirent *entry = readdir(tasks ? scan->tasks : scan->processes);
        if (!entry) {
            if (errno)
                return report(scan, HOST_GROUP_UNKNOWN,
                              tasks ? HOST_GROUP_READ_TASKS : HOST_GROUP_READ_PROC,
                              errno, scan->member);
            if (!tasks) {
                if (!scan->leader_seen)
                    return report(scan, HOST_GROUP_UNKNOWN, HOST_GROUP_VERIFY_MEMBER, ECHILD, pgid);
                return report(scan, scan->changed ? HOST_GROUP_PENDING : HOST_GROUP_COMPLETE,
                              HOST_GROUP_READ_PROC, 0, 0);
            }
            host_proc_stat stat;
            int error = read_stat(scan->member_fd, "stat", scan->member, &stat);
            if (error == ENOENT && scan->member != pgid) {
                scan->changed = true;
            } else if (error || stat.pgid != pgid || stat.start_time != scan->start_time) {
                return report(scan, HOST_GROUP_UNKNOWN, HOST_GROUP_VERIFY_MEMBER,
                              error ? error : ESTALE, scan->member);
            } else if (!execution_ended(stat.state)) {
                return report(scan, HOST_GROUP_LIVE, HOST_GROUP_VERIFY_MEMBER, 0, scan->member);
            } else if (stat.threads != scan->threads || scan->threads_seen != stat.threads) {
                /* 数量不符也可能是主线程退出后的不可枚举 task，不能判成空组。 */
                return report(scan, HOST_GROUP_UNKNOWN, HOST_GROUP_VERIFY_MEMBER, EAGAIN, scan->member);
            }
            close_member(scan);
            continue;
        }
        pid_t pid = numeric_name(entry->d_name);
        if (!pid) continue;
        if (tasks) {
            char path[64];
            snprintf(path, sizeof(path), "%ld/stat", (long)pid);
            host_proc_stat stat;
            int error = read_stat(dirfd(scan->tasks), path, pid, &stat);
            if (error == ENOENT) { scan->changed = true; continue; }
            if (error || stat.pgid != pgid)
                return report(scan, HOST_GROUP_UNKNOWN, HOST_GROUP_READ_THREAD,
                              error ? error : ESTALE, pid);
            scan->threads_seen++;
            if (!execution_ended(stat.state))
                return report(scan, HOST_GROUP_LIVE, HOST_GROUP_READ_THREAD, 0, pid);
            continue;
        }
        scan->member = pid;
        scan->member_fd = owned_fd(openat(dirfd(scan->processes), entry->d_name,
                                         O_RDONLY | O_DIRECTORY | O_CLOEXEC));
        if (scan->member_fd < 0) {
            int error = errno;
            if (error == ENOENT && pid != pgid) { scan->changed = true; continue; }
            return report(scan, HOST_GROUP_UNKNOWN, HOST_GROUP_OPEN_MEMBER, error, pid);
        }
        host_proc_stat stat;
        int error = read_stat(scan->member_fd, "stat", pid, &stat);
        if (error == ENOENT && pid != pgid) {
            scan->changed = true; close_member(scan); continue;
        }
        if (error)
            return report(scan, HOST_GROUP_UNKNOWN, HOST_GROUP_READ_MEMBER, error, pid);
        if (stat.pgid != pgid) {
            if (pid == pgid)
                return report(scan, HOST_GROUP_UNKNOWN, HOST_GROUP_VERIFY_MEMBER, ESTALE, pid);
            close_member(scan);
            continue;
        }
        if (pid == pgid) scan->leader_seen = true;
        if (!execution_ended(stat.state))
            return report(scan, HOST_GROUP_LIVE, HOST_GROUP_READ_MEMBER, 0, pid);
        scan->start_time = stat.start_time;
        scan->threads = stat.threads;
        scan->threads_seen = 0;
        int fd = owned_fd(openat(scan->member_fd, "task", O_RDONLY | O_DIRECTORY | O_CLOEXEC));
        if (fd < 0) {
            error = errno;
            if (error == ENOENT && pid != pgid) { scan->changed = true; close_member(scan); continue; }
            return report(scan, HOST_GROUP_UNKNOWN, HOST_GROUP_OPEN_TASKS, error, pid);
        }
        scan->tasks = fdopendir(fd);
        if (!scan->tasks) {
            error = errno; close(fd);
            return report(scan, HOST_GROUP_UNKNOWN, HOST_GROUP_OPEN_TASKS, error, pid);
        }
    }
    return (host_group_result){HOST_GROUP_PENDING, HOST_GROUP_READ_PROC, 0, 0};
}
