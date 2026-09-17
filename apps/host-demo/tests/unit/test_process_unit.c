#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <errno.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
#include "process.h"
static const char *stdout_data;
static size_t position;
static bool exited, lost, eof;
static bool stderr_stays_open;
static int close_count;
static int wait_status;
pid_t __wrap_waitpid(pid_t pid, int *status, int options) {
    assert_int_equal(pid, 123);
    assert_int_equal(options, WNOHANG);
    if (lost) {
        errno = ECHILD;
        return -1;
    }
    *status = wait_status;
    return exited ? pid : 0;
}
ssize_t __wrap_read(int fd, void *buffer, size_t length) {
    assert_true(fd == 10 || fd == 11);
    if (fd == 10 && position < strlen(stdout_data)) {
        size_t n = strlen(stdout_data) - position;
        if (n > 3)
            n = 3;
        if (n > length)
            n = length;
        memcpy(buffer, stdout_data + position, n);
        position += n;
        return (ssize_t)n;
    }
    if (eof && !(fd == 11 && stderr_stays_open))
        return 0;
    errno = EAGAIN;
    return -1;
}
int __wrap_close(int fd) {
    assert_true(fd == 10 || fd == 11);
    close_count++;
    return 0;
}
static void prepare(host_child *c, char *buffer, size_t length) {
    host_child_init(c, buffer, length);
    c->pid = 123;
    c->out_fd = 10;
    c->err_fd = 11;
    stdout_data = "{\"kind\":\"succeeded\"}\n";
    position = 0;
    exited = false;
    lost = false;
    eof = false;
    close_count = 0;
    stderr_stays_open = false;
    wait_status = 0;
}
static void final_message_before_exit(void **p) {
    (void)p;
    char buffer[128];
    host_child c;
    prepare(&c, buffer, sizeof(buffer));
    host_child_collect(&c, NULL, NULL);
    assert_int_equal(c.length, strlen(stdout_data));
    assert_false(host_child_done(&c));
    exited = true;
    eof = true;
    host_child_collect(&c, NULL, NULL);
    assert_true(host_child_done(&c));
    assert_int_equal(close_count, 2);
}
static void overflow_drains(void **p) {
    (void)p;
    char buffer[4];
    host_child c;
    prepare(&c, buffer, sizeof(buffer));
    exited = true;
    eof = true;
    host_child_collect(&c, NULL, NULL);
    assert_true(c.overflow);
    assert_int_equal(c.length, 4);
    assert_int_equal(position, strlen(stdout_data));
    assert_true(host_child_done(&c));
}
static void lost_wait_is_not_confirmed_exit(void **p) {
    (void)p;
    char buffer[128];
    host_child c;
    prepare(&c, buffer, sizeof(buffer));
    lost = true;
    eof = true;
    host_child_collect(&c, NULL, NULL);
    assert_true(c.wait_fault);
    assert_false(c.ended);
    assert_false(host_child_done(&c));
    assert_int_equal(c.termination, HOST_EXIT_UNKNOWN);
}
static void inherited_stderr_does_not_invalidate_stdout(void **p) {
    (void)p;
    char buffer[128];
    host_child c;
    prepare(&c, buffer, sizeof(buffer));
    exited = true;
    eof = true;
    stderr_stays_open = true;
    host_child_collect(&c, NULL, NULL);
    assert_true(host_child_done(&c));
    assert_false(c.io_error);
}
static void stopped_child_is_not_terminated(void **p) {
    (void)p;
    char buffer[128];
    host_child c;
    prepare(&c, buffer, sizeof(buffer));
    exited = true;                  /* 替身让 waitpid 返回指定 PID 的状态事件。 */
    wait_status = (19 << 8) | 0x7f; /* Linux SIGSTOP 的停止状态编码。 */
    host_child_collect(&c, NULL, NULL);
    assert_false(c.ended);
    assert_false(host_child_done(&c));
    assert_int_equal(close_count, 0);
}
int main(void) {
    const struct CMUnitTest t[] = {cmocka_unit_test(final_message_before_exit),
                                   cmocka_unit_test(overflow_drains),
                                   cmocka_unit_test(lost_wait_is_not_confirmed_exit),
                                   cmocka_unit_test(inherited_stderr_does_not_invalidate_stdout),
                                   cmocka_unit_test(stopped_child_is_not_terminated)};
    return cmocka_run_group_tests(t, NULL, NULL);
}
