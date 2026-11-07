#define _GNU_SOURCE
#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <stdlib.h>
#include <string.h>
#include "process.h"

static struct sigaction disposition;
static int group_error, executions, pipe_calls;
static bool notifications;
static bool maintenance;
static host_command command;
static int pipe_failure, fcntl_failure, spawn_error, closed;
typedef struct { int kind, cloexec; } fake_fd;
typedef struct { int source, target; } fake_action;
static fake_fd parent_fds[16];
static fake_action actions_record[16];
static int action_count, action_failure;
int __real_posix_spawn_file_actions_adddup2(posix_spawn_file_actions_t *, int, int);
int __real_posix_spawn_file_actions_addclose(posix_spawn_file_actions_t *, int);
int __wrap_posix_spawn_file_actions_adddup2(posix_spawn_file_actions_t *actions, int source, int target) {
    if (action_failure && !--action_failure)
        return ENOMEM;
    actions_record[action_count++] = (fake_action){source, target};
    return __real_posix_spawn_file_actions_adddup2(actions, source, target);
}
int __wrap_posix_spawn_file_actions_addclose(posix_spawn_file_actions_t *actions, int fd) {
    actions_record[action_count++] = (fake_action){-1, fd};
    return __real_posix_spawn_file_actions_addclose(actions, fd);
}
int __real_posix_spawnattr_setpgroup(posix_spawnattr_t *, pid_t);
int __wrap_sigaction(int signal, const struct sigaction *action, struct sigaction *old) {
    assert_int_equal(signal, SIGCHLD);
    assert_null(action); /* 模块只查询，不改变主程序的设置。 */
    *old = disposition;
    return 0;
}
int __wrap_pipe2(int fds[2], int flags) {
    assert_int_equal(flags, O_CLOEXEC);
    if (pipe_failure && pipe_calls + 1 == pipe_failure) {
        errno = EMFILE;
        pipe_calls++;
        return -1;
    }
    fds[0] = 10 + 2 * pipe_calls++;
    fds[1] = fds[0] + 1;
    parent_fds[fds[0]] = (fake_fd){fds[0], 1};
    parent_fds[fds[1]] = (fake_fd){fds[1], 1};
    return 0;
}
int __wrap_fcntl(int fd, int command, ...) {
    assert_true(fd == 10 || fd == 12 || fd == 14);
    assert_int_equal(command, F_SETFL);
    va_list args;
    va_start(args, command);
    assert_int_equal(va_arg(args, int), O_NONBLOCK);
    va_end(args);
    if (fcntl_failure && !--fcntl_failure) {
        errno = EIO;
        return -1;
    }
    return 0;
}
int __wrap_close(int fd) {
    assert_true(fd >= 10 && fd <= 15);
    closed++;
    parent_fds[fd].kind = 0;
    return 0;
}
int __wrap_posix_spawnattr_setpgroup(posix_spawnattr_t *attr, pid_t group) {
    assert_int_equal(group, 0);
    return group_error ? group_error : __real_posix_spawnattr_setpgroup(attr, group);
}
int __wrap_posix_spawn(pid_t *pid, const char *path, const posix_spawn_file_actions_t *actions,
                       const posix_spawnattr_t *attr, char *const argv[], char *const env[]) {
    (void)path;
    (void)actions;
    (void)env;
    if (maintenance) {
        assert_string_equal(path, "/updater");
        const char *expected[] = {"/updater", "apply", "--camctl-path", "/camctl",
                                  "--config", "/config", "--bundle", "/bundle.zip",
                                  "--client-data", "/client", NULL};
        for (size_t i = 0; expected[i]; ++i)
            assert_string_equal(argv[i], expected[i]);
        assert_null(argv[10]);
    } else if (notifications && command == HOST_RUN) {
        assert_string_equal(argv[2], "--host-notification-fd");
        int notification_fd = atoi(argv[3]);
        fake_fd child_fds[16];
        memcpy(child_fds, parent_fds, sizeof(child_fds));
        assert_int_equal(parent_fds[10].cloexec, 1);
        assert_int_equal(parent_fds[11].cloexec, 1);
        for (int i = 0; i < action_count; ++i) {
            int source = actions_record[i].source, target = actions_record[i].target;
            if (source < 0) child_fds[target].kind = 0;
            else if (source != target) {
                child_fds[target] = child_fds[source];
                child_fds[target].cloexec = 0;
            }
        }
        for (int i = 3; i < 16; ++i)
            if (child_fds[i].cloexec) child_fds[i].kind = 0;
        assert_true(notification_fd >= 3 && notification_fd < 16);
        assert_int_equal(child_fds[notification_fd].kind, 11);
        for (int i = 3; i < 16; ++i)
            if (i != notification_fd) assert_int_equal(child_fds[i].kind, 0);
        assert_null(argv[4]);
    } else
        assert_null(argv[2]);
    short flags;
    pid_t group;
    assert_int_equal(posix_spawnattr_getflags(attr, &flags), 0);
    if (!group_error && disposition.sa_handler != SIG_IGN && !(disposition.sa_flags & SA_NOCLDWAIT))
        assert_true(flags & POSIX_SPAWN_SETPGROUP);
    assert_int_equal(posix_spawnattr_getpgroup(attr, &group), 0);
    assert_int_equal(group, 0);
    executions++;
    if (spawn_error)
        return spawn_error;
    *pid = 123;
    return 0;
}
static void prepare(void) {
    disposition = (struct sigaction){0};
    disposition.sa_handler = SIG_DFL;
    group_error = executions = pipe_calls = 0;
    notifications = false;
    maintenance = false;
    command = HOST_RUN;
    pipe_failure = fcntl_failure = spawn_error = closed = 0;
    memset(parent_fds, 0, sizeof(parent_fds));
    action_count = action_failure = 0;
}
static int launch(void) {
    camctl_host_config cfg = CAMCTL_HOST_CONFIG_INIT;
    cfg.camctl_path = "/camctl";
    host_child child;
    char output[64], stderr_output[64];
    host_child_init(&child, output, sizeof(output), stderr_output, sizeof(stderr_output));
    child.stop_requested = true;
    child.notifications_enabled = notifications;
    int error = host_child_spawn(&child, &cfg, command, NULL);
    assert_false(child.stop_requested);
    if (!error && notifications && command == HOST_RUN) {
        assert_int_equal(child.notification_fd, 10);
        assert_int_equal(parent_fds[child.notification_fd].kind, 10);
        assert_int_equal(parent_fds[child.notification_fd].cloexec, 1);
        assert_int_equal(child.notification_write_fd, -1);
        assert_int_equal(parent_fds[11].kind, 0);
    }
    return error;
}
static void test_group_failure_never_executes_camctl(void **state) {
    (void)state;
    prepare();
    group_error = EPERM;
    assert_int_equal(launch(), EPERM);
    assert_int_equal(executions, 0);
}
static void ignored_sigchld_rejects_launch(void **state) {
    (void)state;
    prepare();
    disposition.sa_handler = SIG_IGN;
    assert_int_equal(launch(), EINVAL);
    assert_int_equal(executions, 0);
    assert_int_equal(pipe_calls, 0);
}
static void no_child_wait_rejects_launch(void **state) {
    (void)state;
    prepare();
    disposition.sa_flags = SA_NOCLDWAIT;
    assert_int_equal(launch(), EINVAL);
    assert_int_equal(executions, 0);
}
static void default_sigchld_launches_separate_group(void **state) {
    (void)state;
    prepare();
    assert_int_equal(launch(), 0);
    assert_int_equal(executions, 1);
}
static void handler(int signal) { (void)signal; }
static void legal_handler_launches_separate_group(void **state) {
    (void)state;
    prepare();
    disposition.sa_handler = handler;
    assert_int_equal(launch(), 0);
    assert_int_equal(executions, 1);
}
static void run_notification_fd(void **state) {
    (void)state;
    prepare();
    notifications = true;
    assert_int_equal(launch(), 0);
    assert_int_equal(pipe_calls, 3);
}
static void submit_without_notification_fd(void **state) {
    (void)state;
    prepare();
    notifications = true;
    command = HOST_SUBMIT;
    assert_int_equal(launch(), 0);
    assert_int_equal(pipe_calls, 2);
}
static void maintenance_forwards_explicit_paths(void **state) {
    (void)state;
    prepare(); maintenance = true;
    camctl_host_update_config config = {"/camctl", "/updater", "/config"};
    camctl_host_update_request request = {CAMCTL_UPDATE_ACTION_APPLY, "/bundle.zip", "/client"};
    host_child child;
    char output[64], errors[64];
    host_child_init(&child, output, sizeof(output), errors, sizeof(errors));
    assert_int_equal(host_maintenance_spawn(&child, &config, &request), 0);
    assert_int_equal(executions, 1);
}
static void notification_resource_failures(void **state) {
    (void)state;
    for (int i = 1; i <= 3; i++) {
        prepare();
        notifications = true;
        pipe_failure = i;
        assert_int_equal(launch(), EMFILE);
        assert_int_equal(closed, (i - 1) * 2);
        assert_int_equal(executions, 0);
    }
    for (int i = 1; i <= 3; i++) {
        prepare();
        notifications = true;
        fcntl_failure = i;
        assert_int_equal(launch(), EIO);
        assert_int_equal(closed, i * 2);
        assert_int_equal(executions, 0);
    }
    prepare();
    notifications = true;
    spawn_error = ENOENT;
    assert_int_equal(launch(), ENOENT);
    assert_int_equal(closed, 6);
    prepare();
    notifications = true;
    action_failure = 3;
    assert_int_equal(launch(), ENOMEM);
    assert_int_equal(closed, 6);
    assert_int_equal(executions, 0);
}
int main(void) {
    const struct CMUnitTest tests[] = {cmocka_unit_test(maintenance_forwards_explicit_paths),
                                       cmocka_unit_test(notification_resource_failures),
                                       cmocka_unit_test(run_notification_fd),
                                       cmocka_unit_test(submit_without_notification_fd),
                                       cmocka_unit_test(test_group_failure_never_executes_camctl),
                                       cmocka_unit_test(ignored_sigchld_rejects_launch),
                                       cmocka_unit_test(no_child_wait_rejects_launch),
                                       cmocka_unit_test(default_sigchld_launches_separate_group),
                                       cmocka_unit_test(legal_handler_launches_separate_group)};
    return cmocka_run_group_tests(tests, NULL, NULL);
}
