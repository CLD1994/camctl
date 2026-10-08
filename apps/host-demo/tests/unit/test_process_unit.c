#define _GNU_SOURCE
#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <errno.h>
#include <string.h>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>
#include "process.h"
static const char *stdout_data;
static size_t position;
static bool exited, lost, eof, stderr_stays_open;
static int close_count, wait_code, reaps, signals, kill_error, scans, observations;
static int wait_error, reap_error;
static pid_t current_group;
static host_group_status group_state;
int __wrap_waitid(idtype_t type, id_t pid, siginfo_t *info, int options) {
    assert_int_equal(type, P_PID);
    assert_int_equal(pid, 123);
    assert_int_equal(options, WEXITED | WNOHANG | WNOWAIT);
    observations++;
    if (lost || wait_error) { errno = lost ? ECHILD : wait_error; return -1; }
    *info = (siginfo_t){0};
    if (exited) {
        info->si_pid = 123;
        info->si_code = wait_code;
        info->si_status = 0;
    }
    return 0;
}
pid_t __wrap_waitpid(pid_t pid, int *status, int options) {
    assert_int_equal(pid, 123);
    assert_int_equal(options, WNOHANG);
    assert_int_equal(group_state, HOST_GROUP_COMPLETE);
    reaps++;
    if (lost || reap_error) { errno = lost ? ECHILD : reap_error; return -1; }
    *status = 0;
    return exited ? pid : 0;
}
pid_t __wrap_getpgid(pid_t pid) {
    assert_int_equal(pid, 123);
    return current_group;
}
int __wrap_kill(pid_t pid, int signal) {
    assert_int_equal(pid, -123);
    assert_int_equal(signal, SIGKILL);
    assert_true(exited);
    assert_int_equal(reaps, 0);
    signals++;
    if (kill_error) { errno = kill_error; return -1; }
    return 0;
}
void __wrap_host_group_scan_init(host_group_scan *scan) { memset(scan, 0, sizeof(*scan)); }
void __wrap_host_group_scan_reset(host_group_scan *scan) { (void)scan; }
host_group_result __wrap_host_group_scan_batch(host_group_scan *scan, pid_t pgid, size_t limit) {
    (void)scan;
    assert_int_equal(pgid, 123);
    assert_true(limit > 0);
    scans++;
    return (host_group_result){group_state, HOST_GROUP_READ_THREAD,
                              group_state == HOST_GROUP_UNKNOWN ? EACCES : 0, 456};
}
const char *__wrap_host_group_operation_name(host_group_operation operation) {
    (void)operation; return "read_thread";
}
ssize_t __wrap_read(int fd, void *buffer, size_t length) {
    assert_true(fd == 10 || fd == 11);
    if (fd == 10 && position < strlen(stdout_data)) {
        size_t n = strlen(stdout_data) - position;
        if (n > 3) n = 3;
        if (n > length) n = length;
        memcpy(buffer, stdout_data + position, n);
        position += n;
        return (ssize_t)n;
    }
    if (eof && !(fd == 11 && stderr_stays_open)) return 0;
    errno = EAGAIN; return -1;
}
ssize_t __wrap___read_chk(int fd, void *buffer, size_t length, size_t available) {
    assert_true(length <= available);
    return __wrap_read(fd, buffer, length);
}
int __wrap_close(int fd) {
    assert_true(fd == 10 || fd == 11); close_count++; return 0;
}
static void prepare(host_child *c, char *buffer, size_t length) {
    host_child_init(c, buffer, length);
    c->pid = c->pgid = 123; c->out_fd = 10; c->err_fd = 11;
    stdout_data = "{\"kind\":\"succeeded\"}\n";
    position = 0; exited = lost = eof = stderr_stays_open = false;
    close_count = reaps = signals = kill_error = scans = observations = 0;
    wait_error = reap_error = 0; wait_code = CLD_EXITED;
    current_group = 123; group_state = HOST_GROUP_COMPLETE;
}
static void final_message_before_exit(void **p) {
    (void)p; char buffer[128]; host_child c; prepare(&c, buffer, sizeof(buffer));
    host_child_collect(&c, NULL, NULL);
    assert_int_equal(c.length, strlen(stdout_data));
    assert_false(host_child_done(&c)); assert_int_equal(reaps, 0);
    exited = eof = true; host_child_collect(&c, NULL, NULL);
    assert_true(host_child_done(&c)); assert_int_equal(close_count, 2);
}
static void overflow_drains(void **p) {
    (void)p; char buffer[4]; host_child c; prepare(&c, buffer, sizeof(buffer));
    exited = eof = true; host_child_collect(&c, NULL, NULL);
    assert_true(c.overflow); assert_int_equal(c.length, 4);
    assert_int_equal(position, strlen(stdout_data)); assert_true(host_child_done(&c));
}
static void lost_wait_is_not_confirmed_exit(void **p) {
    (void)p; char buffer[128]; host_child c; prepare(&c, buffer, sizeof(buffer));
    lost = eof = true; host_child_collect(&c, NULL, NULL);
    assert_true(c.wait_fault); assert_false(c.ended); assert_false(host_child_done(&c));
    assert_int_equal(c.termination, HOST_EXIT_UNKNOWN); assert_int_equal(signals, 0);
}
static void inherited_stderr_closes_only_after_settlement(void **p) {
    (void)p; char buffer[128]; host_child c; prepare(&c, buffer, sizeof(buffer));
    exited = eof = stderr_stays_open = true; group_state = HOST_GROUP_PENDING;
    host_child_collect(&c, NULL, NULL);
    assert_false(host_child_done(&c)); assert_int_equal(c.err_fd, 11);
    group_state = HOST_GROUP_COMPLETE; host_child_collect(&c, NULL, NULL);
    assert_true(host_child_done(&c)); assert_false(c.io_error);
}
static void stopped_child_is_not_terminated(void **p) {
    (void)p; char buffer[128]; host_child c; prepare(&c, buffer, sizeof(buffer));
    exited = true; wait_code = CLD_STOPPED; host_child_collect(&c, NULL, NULL);
    assert_false(c.ended); assert_false(host_child_done(&c)); assert_int_equal(reaps, 0);
}
static void test_kill_does_not_release_run_slot(void **p) {
    (void)p; char buffer[128]; host_child c; prepare(&c, buffer, sizeof(buffer));
    exited = eof = true; group_state = HOST_GROUP_LIVE;
    host_child_collect(&c, NULL, NULL);
    assert_true(c.ended); assert_false(host_child_done(&c));
    assert_int_equal(reaps, 0); assert_int_equal(signals, 1);
    group_state = HOST_GROUP_PENDING; host_child_collect(&c, NULL, NULL);
    assert_false(host_child_done(&c)); assert_int_equal(reaps, 0);
    group_state = HOST_GROUP_COMPLETE; host_child_collect(&c, NULL, NULL);
    assert_true(host_child_done(&c)); assert_int_equal(reaps, 1);
}
static void unknown_scan_keeps_exit_record(void **p) {
    (void)p; char buffer[128]; host_child c; prepare(&c, buffer, sizeof(buffer));
    exited = eof = true; group_state = HOST_GROUP_UNKNOWN;
    host_child_collect(&c, NULL, NULL);
    assert_true(c.ended); assert_false(host_child_done(&c));
    assert_int_equal(reaps, 0); assert_int_equal(signals, 0);
    group_state = HOST_GROUP_COMPLETE; host_child_collect(&c, NULL, NULL);
    assert_true(host_child_done(&c));
}
static void early_reap_stops_group_signals(void **p) {
    (void)p; char buffer[128]; host_child c; prepare(&c, buffer, sizeof(buffer));
    exited = eof = true; group_state = HOST_GROUP_PENDING;
    host_child_collect(&c, NULL, NULL);
    lost = true; group_state = HOST_GROUP_LIVE; host_child_collect(&c, NULL, NULL);
    assert_true(c.wait_fault); assert_false(host_child_done(&c));
    assert_int_equal(signals, 0); assert_int_equal(reaps, 0);
}
static void changed_group_never_signals_old_number(void **p) {
    (void)p; char buffer[128]; host_child c; prepare(&c, buffer, sizeof(buffer));
    exited = eof = true; current_group = 789; group_state = HOST_GROUP_LIVE;
    host_child_collect(&c, NULL, NULL);
    assert_false(host_child_done(&c)); assert_int_equal(signals, 0);
}
static void signal_failure_keeps_call(void **p) {
    (void)p; char buffer[128]; host_child c; prepare(&c, buffer, sizeof(buffer));
    exited = eof = true; group_state = HOST_GROUP_LIVE; kill_error = EPERM;
    host_child_collect(&c, NULL, NULL);
    assert_false(host_child_done(&c)); assert_int_equal(reaps, 0);
    kill_error = 0; host_child_collect(&c, NULL, NULL);
    assert_false(host_child_done(&c)); assert_int_equal(signals, 2);
}
static void interrupted_observation_retries(void **p) {
    (void)p; char buffer[128]; host_child c; prepare(&c, buffer, sizeof(buffer));
    exited = eof = true; wait_error = EINTR; host_child_collect(&c, NULL, NULL);
    assert_false(c.wait_fault); assert_false(host_child_done(&c));
    wait_error = 0; host_child_collect(&c, NULL, NULL); assert_true(host_child_done(&c));
}
static void final_reap_failure_keeps_call(void **p) {
    (void)p; char buffer[128]; host_child c; prepare(&c, buffer, sizeof(buffer));
    exited = eof = true; reap_error = ECHILD; host_child_collect(&c, NULL, NULL);
    assert_false(host_child_done(&c)); assert_true(c.wait_fault); assert_int_equal(signals, 0);
}
int main(void) {
    const struct CMUnitTest t[] = {
        cmocka_unit_test(final_message_before_exit), cmocka_unit_test(overflow_drains),
        cmocka_unit_test(lost_wait_is_not_confirmed_exit),
        cmocka_unit_test(inherited_stderr_closes_only_after_settlement),
        cmocka_unit_test(stopped_child_is_not_terminated),
        cmocka_unit_test(test_kill_does_not_release_run_slot),
        cmocka_unit_test(unknown_scan_keeps_exit_record), cmocka_unit_test(early_reap_stops_group_signals),
        cmocka_unit_test(changed_group_never_signals_old_number), cmocka_unit_test(signal_failure_keeps_call),
        cmocka_unit_test(interrupted_observation_retries), cmocka_unit_test(final_reap_failure_keeps_call)};
    return cmocka_run_group_tests(t, NULL, NULL);
}
