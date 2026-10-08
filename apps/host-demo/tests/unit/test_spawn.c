#define _GNU_SOURCE
#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include "process.h"

static struct sigaction disposition;
static int group_error, executions, pipe_calls;
int __real_posix_spawnattr_setpgroup(posix_spawnattr_t *, pid_t);
int __wrap_sigaction(int signal, const struct sigaction *action, struct sigaction *old) {
    assert_int_equal(signal, SIGCHLD);
    assert_null(action); /* 模块只查询，不改变主程序的设置。 */
    *old = disposition;
    return 0;
}
int __wrap_pipe2(int fds[2], int flags) {
    assert_int_equal(flags, O_CLOEXEC);
    fds[0] = 10 + 2 * pipe_calls++;
    fds[1] = fds[0] + 1;
    return 0;
}
int __wrap_fcntl(int fd, int command, ...) {
    assert_true(fd == 10 || fd == 12);
    assert_int_equal(command, F_SETFL);
    va_list args;
    va_start(args, command);
    assert_int_equal(va_arg(args, int), O_NONBLOCK);
    va_end(args);
    return 0;
}
int __wrap_close(int fd) {
    assert_true(fd >= 10 && fd <= 13);
    return 0;
}
int __wrap_posix_spawnattr_setpgroup(posix_spawnattr_t *attr, pid_t group) {
    assert_int_equal(group, 0);
    return group_error ? group_error : __real_posix_spawnattr_setpgroup(attr, group);
}
int __wrap_posix_spawn(pid_t *pid, const char *path, const posix_spawn_file_actions_t *actions,
                       const posix_spawnattr_t *attr, char *const argv[], char *const env[]) {
    (void)path; (void)actions; (void)argv; (void)env;
    short flags;
    pid_t group;
    assert_int_equal(posix_spawnattr_getflags(attr, &flags), 0);
    if (!group_error && disposition.sa_handler != SIG_IGN &&
        !(disposition.sa_flags & SA_NOCLDWAIT))
        assert_true(flags & POSIX_SPAWN_SETPGROUP);
    assert_int_equal(posix_spawnattr_getpgroup(attr, &group), 0);
    assert_int_equal(group, 0);
    executions++;
    *pid = 123;
    return 0;
}
static void prepare(void) {
    disposition = (struct sigaction){0};
    disposition.sa_handler = SIG_DFL;
    group_error = executions = pipe_calls = 0;
}
static int launch(void) {
    camctl_host_config cfg = CAMCTL_HOST_CONFIG_INIT;
    cfg.camctl_path = "/camctl";
    host_child child;
    char output[64];
    host_child_init(&child, output, sizeof(output));
    return host_child_spawn(&child, &cfg, HOST_RUN, NULL);
}
static void test_group_failure_never_executes_camctl(void **state) {
    (void)state; prepare(); group_error = EPERM;
    assert_int_equal(launch(), EPERM);
    assert_int_equal(executions, 0);
}
static void ignored_sigchld_rejects_launch(void **state) {
    (void)state; prepare(); disposition.sa_handler = SIG_IGN;
    assert_int_equal(launch(), EINVAL);
    assert_int_equal(executions, 0);
    assert_int_equal(pipe_calls, 0);
}
static void no_child_wait_rejects_launch(void **state) {
    (void)state; prepare(); disposition.sa_flags = SA_NOCLDWAIT;
    assert_int_equal(launch(), EINVAL);
    assert_int_equal(executions, 0);
}
static void default_sigchld_launches_separate_group(void **state) {
    (void)state; prepare();
    assert_int_equal(launch(), 0);
    assert_int_equal(executions, 1);
}
static void handler(int signal) { (void)signal; }
static void legal_handler_launches_separate_group(void **state) {
    (void)state; prepare(); disposition.sa_handler = handler;
    assert_int_equal(launch(), 0);
    assert_int_equal(executions, 1);
}
int main(void) {
    const struct CMUnitTest tests[] = {
        cmocka_unit_test(test_group_failure_never_executes_camctl),
        cmocka_unit_test(ignored_sigchld_rejects_launch),
        cmocka_unit_test(no_child_wait_rejects_launch),
        cmocka_unit_test(default_sigchld_launches_separate_group),
        cmocka_unit_test(legal_handler_launches_separate_group)};
    return cmocka_run_group_tests(tests, NULL, NULL);
}
