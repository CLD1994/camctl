#define _GNU_SOURCE
#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include "process_group.h"

/* 真实 /proc 接口的内存替身：数字目录、stat 与 task，均不访问文件系统。 */
typedef struct { int fd; size_t index; } directory;
static directory proc_dir, leader_tasks, member_tasks;
static char member_state, thread_state;
static int member_error, thread_error, proc_error, enumeration_error, stat_error, read_error, task_error;
static bool malformed, leader_missing, vanished, changed_identity;
static int resources, reads, member_reads;
static char stat_data[512];
static size_t stat_position;
static void prepare(void) {
    proc_dir = (directory){20, 0}; leader_tasks = (directory){41, 0};
    member_tasks = (directory){42, 0};
    member_state = thread_state = 'Z';
    member_error = thread_error = proc_error = enumeration_error = resources = reads = member_reads = 0;
    stat_error = read_error = task_error = 0;
    malformed = leader_missing = vanished = changed_identity = false;
}
int __wrap_dirfd(DIR *dir) { return ((directory *)dir)->fd; }
DIR *__wrap_fdopendir(int fd) {
    assert_true(fd == 20 || fd == 41 || fd == 42);
    if (fd == 41 && task_error) { errno = task_error; return NULL; }
    directory *dir = fd == 20 ? &proc_dir : fd == 41 ? &leader_tasks : &member_tasks;
    dir->index = 0;
    return (DIR *)dir; /* openat 已计入描述符，fdopendir 接管同一描述符。 */
}
int __wrap_closedir(DIR *dir) { (void)dir; resources--; return 0; }
struct dirent *__wrap_readdir(DIR *dir) {
    directory *d = (directory *)dir;
    static struct dirent entry;
    const char *name = NULL;
    reads++;
    if (enumeration_error) { errno = enumeration_error; return NULL; }
    if (d->fd == 20) {
        const char *entries[] = {".", "self", "123", "456", "789"};
        if (d->index < 5) name = entries[d->index++];
        if (leader_missing && name && !strcmp(name, "123")) name = "999";
    } else if (d->fd == 41) {
        const char *entries[] = {".", "123"};
        if (d->index < 2) name = entries[d->index++];
    } else {
        const char *entries[] = {".", "456", "457"};
        if (d->index < 3) name = entries[d->index++];
    }
    if (!name) return NULL;
    memset(&entry, 0, sizeof(entry));
    strcpy(entry.d_name, name);
    return &entry;
}
int __wrap_openat(int fd, const char *path, int flags, ...) {
    assert_true(flags & O_CLOEXEC);
    if (fd == AT_FDCWD) {
        assert_string_equal(path, "/proc");
        if (proc_error) { errno = proc_error; return -1; }
        resources++; return 20;
    }
    if (fd == 20) {
        if (!strcmp(path, "456") && member_error) { errno = member_error; return -1; }
        if (!strcmp(path, "999")) { errno = ENOENT; return -1; }
        assert_true(!strcmp(path, "123") || !strcmp(path, "456") || !strcmp(path, "789"));
        resources++; return !strcmp(path, "123") ? 30 : !strcmp(path, "456") ? 31 : 32;
    }
    if (!strcmp(path, "task")) {
        assert_true(fd == 30 || fd == 31);
        resources++; return fd == 30 ? 41 : 42;
    }
    int pid;
    char state;
    if (fd == 30 || fd == 31 || fd == 32) {
        assert_string_equal(path, "stat");
        if (fd == 31 && stat_error) { errno = stat_error; return -1; }
        pid = fd == 30 ? 123 : fd == 31 ? 456 : 789;
        state = fd == 30 ? 'Z' : fd == 31 ? member_state : 'R';
        if (fd == 31) member_reads++;
    } else {
        assert_true(fd == 41 || fd == 42);
        pid = !strcmp(path, "123/stat") ? 123 : !strcmp(path, "456/stat") ? 456 : 457;
        if (pid == 457 && (thread_error || vanished)) {
            errno = vanished ? ENOENT : thread_error; return -1;
        }
        state = pid == 457 ? thread_state : 'Z';
    }
    unsigned long long start = changed_identity && pid == 456 && member_reads > 1 ? 701 : 700;
    snprintf(stat_data, sizeof(stat_data),
             "%d (tool ) name with spaces) %c 1 %d 0 0 0 0 0 0 0 0 0 0 0 0 0 0 %u 0 %llu 0 0\n",
             pid, state, pid == 789 ? 789 : 123, pid == 456 ? 2 : 1, start);
    if (malformed && pid == 456) strcpy(stat_data, "456 (truncated) Z 1\n");
    stat_position = 0; resources++; return 50;
}
ssize_t __wrap_read(int fd, void *buffer, size_t length) {
    assert_int_equal(fd, 50);
    if (read_error) { errno = read_error; return -1; }
    size_t left = strlen(stat_data) - stat_position;
    if (left > length) left = length;
    memcpy(buffer, stat_data + stat_position, left);
    stat_position += left; return (ssize_t)left;
}
ssize_t __wrap___read_chk(int fd, void *buffer, size_t length, size_t available) {
    assert_true(length <= available);
    return __wrap_read(fd, buffer, length);
}
int __wrap_close(int fd) {
    assert_true(fd == 20 || (fd >= 30 && fd <= 32) || fd == 41 || fd == 42 || fd == 50);
    resources--; return 0;
}
static host_group_result scan_all(host_group_scan *scan) {
    host_group_result result;
    for (int i = 0; i < 100; i++) {
        result = host_group_scan_batch(scan, 123, 2);
        if (result.status != HOST_GROUP_PENDING) return result;
    }
    fail_msg("%s", "扫描未完成"); return (host_group_result){0};
}
static void parses_comm_and_identity(void **context) {
    (void)context;
    const char *data = "123 (a ) b ( c) Z 1 123 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 700 0\n";
    host_proc_stat value;
    assert_int_equal(host_proc_stat_parse(data, strlen(data), 123, &value), 0);
    assert_int_equal(value.pid, 123); assert_int_equal(value.pgid, 123);
    assert_int_equal(value.state, 'Z'); assert_int_equal(value.start_time, 700);
}
static void rejects_invalid_stat(void **context) {
    (void)context;
    const char *data[] = {"", "123 (short) Z 1 123",
        "123 (a) ? 1 123 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 700 0\n",
        "123 (a) Z 1 bad 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 700 0\n",
        "456 (a) Z 1 123 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 700 0\n"};
    host_proc_stat value;
    for (size_t i = 0; i < sizeof(data)/sizeof(data[0]); i++)
        assert_int_equal(host_proc_stat_parse(data[i], strlen(data[i]), 123, &value), EBADMSG);
}
static void only_complete_round_releases(void **context) {
    (void)context; prepare(); host_group_scan scan; host_group_scan_init(&scan);
    assert_int_equal(host_group_scan_batch(&scan, 123, 1).status, HOST_GROUP_PENDING);
    assert_true(reads <= 1);
    assert_int_equal(scan_all(&scan).status, HOST_GROUP_COMPLETE);
    assert_int_equal(resources, 0);
}
static void zombie_leader_with_live_thread_is_live(void **context) {
    (void)context; prepare(); thread_state = 'S'; host_group_scan scan; host_group_scan_init(&scan);
    assert_int_equal(scan_all(&scan).status, HOST_GROUP_LIVE);
    host_group_scan_reset(&scan); assert_int_equal(resources, 0);
}
static void active_states_do_not_count_as_stopped(void **context) {
    (void)context;
    const char *states = "RSDTtWKPI";
    for (const char *state = states; *state; state++) {
        prepare(); member_state = *state; host_group_scan scan; host_group_scan_init(&scan);
        assert_int_equal(scan_all(&scan).status, HOST_GROUP_LIVE);
        host_group_scan_reset(&scan); assert_int_equal(resources, 0);
    }
}
static void denied_member_is_unknown(void **context) {
    (void)context; prepare(); member_error = EACCES; host_group_scan scan; host_group_scan_init(&scan);
    host_group_result result = scan_all(&scan);
    assert_int_equal(result.status, HOST_GROUP_UNKNOWN); assert_int_equal(result.error, EACCES);
    assert_int_equal(result.member, 456); host_group_scan_reset(&scan); assert_int_equal(resources, 0);
}
static void denied_thread_is_unknown(void **context) {
    (void)context; prepare(); thread_error = EACCES; host_group_scan scan; host_group_scan_init(&scan);
    host_group_result result = scan_all(&scan);
    assert_int_equal(result.status, HOST_GROUP_UNKNOWN); assert_int_equal(result.error, EACCES);
    host_group_scan_reset(&scan); assert_int_equal(resources, 0);
}
static void incomplete_stat_is_unknown(void **context) {
    (void)context; prepare(); malformed = true; host_group_scan scan; host_group_scan_init(&scan);
    assert_int_equal(scan_all(&scan).status, HOST_GROUP_UNKNOWN);
    host_group_scan_reset(&scan); assert_int_equal(resources, 0);
}
static void vanished_member_requires_fresh_round(void **context) {
    (void)context; prepare(); member_error = ENOENT; host_group_scan scan; host_group_scan_init(&scan);
    assert_int_equal(host_group_scan_batch(&scan, 123, 64).status, HOST_GROUP_PENDING);
    member_error = 0;
    assert_int_equal(scan_all(&scan).status, HOST_GROUP_COMPLETE);
    assert_int_equal(resources, 0);
}
static void missing_original_leader_is_unknown(void **context) {
    (void)context; prepare(); leader_missing = true; host_group_scan scan; host_group_scan_init(&scan);
    assert_int_equal(scan_all(&scan).status, HOST_GROUP_UNKNOWN);
    host_group_scan_reset(&scan); assert_int_equal(resources, 0);
}
static void changed_member_identity_is_unknown(void **context) {
    (void)context; prepare(); changed_identity = true; host_group_scan scan; host_group_scan_init(&scan);
    assert_int_equal(scan_all(&scan).status, HOST_GROUP_UNKNOWN);
    host_group_scan_reset(&scan); assert_int_equal(resources, 0);
}
static void enumeration_failure_is_unknown(void **context) {
    (void)context; prepare(); enumeration_error = EIO; host_group_scan scan; host_group_scan_init(&scan);
    host_group_result result = scan_all(&scan);
    assert_int_equal(result.status, HOST_GROUP_UNKNOWN); assert_int_equal(result.error, EIO);
    host_group_scan_reset(&scan); assert_int_equal(resources, 0);
}
static void proc_open_failure_is_unknown(void **context) {
    (void)context; prepare(); proc_error = EACCES; host_group_scan scan; host_group_scan_init(&scan);
    host_group_result result = scan_all(&scan);
    assert_int_equal(result.status, HOST_GROUP_UNKNOWN); assert_int_equal(result.error, EACCES);
    assert_int_equal(resources, 0);
}
static void stat_open_failure_is_unknown(void **context) {
    (void)context; prepare(); stat_error = EACCES; host_group_scan scan; host_group_scan_init(&scan);
    host_group_result result = scan_all(&scan);
    assert_int_equal(result.status, HOST_GROUP_UNKNOWN); assert_int_equal(result.error, EACCES);
    assert_int_equal(result.operation, HOST_GROUP_READ_MEMBER); assert_int_equal(resources, 0);
}
static void stat_read_failure_is_unknown(void **context) {
    (void)context; prepare(); read_error = EIO; host_group_scan scan; host_group_scan_init(&scan);
    host_group_result result = scan_all(&scan);
    assert_int_equal(result.status, HOST_GROUP_UNKNOWN); assert_int_equal(result.error, EIO);
    assert_int_equal(resources, 0);
}
static void task_open_failure_releases_scan_descriptors(void **context) {
    (void)context; prepare(); task_error = ENOMEM; host_group_scan scan; host_group_scan_init(&scan);
    host_group_result result = scan_all(&scan);
    assert_int_equal(result.status, HOST_GROUP_UNKNOWN); assert_int_equal(result.error, ENOMEM);
    assert_int_equal(resources, 0);
}
static void vanished_thread_never_completes_partial_round(void **context) {
    (void)context; prepare(); vanished = true; host_group_scan scan; host_group_scan_init(&scan);
    assert_int_equal(scan_all(&scan).status, HOST_GROUP_UNKNOWN);
    assert_int_equal(resources, 0);
    vanished = false;
    assert_int_equal(scan_all(&scan).status, HOST_GROUP_COMPLETE);
}
int main(void) {
    const struct CMUnitTest tests[] = {
        cmocka_unit_test(parses_comm_and_identity), cmocka_unit_test(rejects_invalid_stat),
        cmocka_unit_test(only_complete_round_releases),
        cmocka_unit_test(zombie_leader_with_live_thread_is_live),
        cmocka_unit_test(active_states_do_not_count_as_stopped), cmocka_unit_test(denied_member_is_unknown),
        cmocka_unit_test(denied_thread_is_unknown), cmocka_unit_test(incomplete_stat_is_unknown),
        cmocka_unit_test(vanished_member_requires_fresh_round),
        cmocka_unit_test(missing_original_leader_is_unknown),
        cmocka_unit_test(changed_member_identity_is_unknown), cmocka_unit_test(enumeration_failure_is_unknown),
        cmocka_unit_test(proc_open_failure_is_unknown), cmocka_unit_test(stat_open_failure_is_unknown),
        cmocka_unit_test(stat_read_failure_is_unknown), cmocka_unit_test(task_open_failure_releases_scan_descriptors),
        cmocka_unit_test(vanished_thread_never_completes_partial_round)};
    return cmocka_run_group_tests(tests, NULL, NULL);
}
