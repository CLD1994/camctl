#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <string.h>
#include <sys/stat.h>
#include "diagnostic.h"

/* 运行真实写入循环；线程入口由 join 在本线程执行，文件接口只接受夹具路径。 */
static struct { void *(*entry)(void *); void *argument; bool joined; } threads[3];
static int created, joins, join_error, publications, wakes, closes;
static char written[8192];
static size_t written_length;
static host_logger logger;
static host_diagnostic_writer writer;
static host_child children[2];
static pthread_mutex_t publish_mutex;
static camctl_host_config config;
int __wrap_pthread_create(pthread_t *thread, const pthread_attr_t *attr,
                          void *(*entry)(void *), void *argument) {
    assert_null(attr); assert_true(++created < 3);
    *thread = (pthread_t)created;
    threads[created].entry = entry; threads[created].argument = argument;
    return 0;
}
int __wrap_pthread_join(pthread_t thread, void **result) {
    (void)result; assert_true(thread > 0 && thread <= (pthread_t)created); joins++;
    pthread_mutex_t *mutex = thread == logger.thread ? &logger.mutex : &writer.mutex;
    assert_int_equal(pthread_mutex_trylock(mutex), 0); pthread_mutex_unlock(mutex);
    if (join_error) { int error = join_error; join_error = 0; return error; }
    assert_false(threads[thread].joined);
    threads[thread].entry(threads[thread].argument); threads[thread].joined = true;
    return 0;
}
int __wrap_open(const char *path, int flags, ...) {
    if (!strcmp(path, "/processing")) {
        assert_true(flags & O_DIRECTORY); publications++; errno = EACCES; return -1;
    }
    assert_string_equal(path, "/log"); assert_true(flags & O_APPEND); return 10;
}
int __wrap_fstat(int fd, struct stat *st) {
    assert_int_equal(fd, 10); memset(st, 0, sizeof(*st)); st->st_mode = S_IFREG; return 0;
}
int __wrap_unlink(const char *path) { (void)path; errno = ENOENT; return -1; }
int __wrap_close(int fd) { assert_int_equal(fd, 10); closes++; return 0; }
ssize_t __wrap_write(int fd, const void *data, size_t length) {
    if (fd == 42) { assert_int_equal(length, 1); wakes++; return 1; }
    assert_int_equal(fd, 10); assert_true(written_length + length < sizeof(written));
    memcpy(written + written_length, data, length); written_length += length;
    written[written_length] = 0; return (ssize_t)length;
}
static int setup(void **state) {
    (void)state; memset(threads, 0, sizeof(threads)); memset(children, 0, sizeof(children));
    created = joins = join_error = publications = wakes = closes = 0;
    written_length = 0; written[0] = 0;
    config = (camctl_host_config)CAMCTL_HOST_CONFIG_INIT;
    config.log_path = "/log"; config.processing_path = "/processing";
    assert_int_equal(pthread_mutex_init(&publish_mutex, NULL), 0);
    assert_int_equal(host_logger_init(&logger, &config), 0);
    assert_int_equal(host_diagnostic_init(&writer, &config, &logger, &publish_mutex, 42, children), 0);
    return 0;
}
static int teardown(void **state) {
    (void)state;
    assert_int_equal(host_diagnostic_close(&writer), 0);
    assert_int_equal(host_logger_close(&logger), 0);
    pthread_mutex_destroy(&publish_mutex); return 0;
}
static void logger_drains_queued_records(void **state) {
    (void)state; host_log(&logger, "first"); host_log(&logger, "second");
    assert_non_null(logger.queue.head);
    assert_int_equal(host_logger_close(&logger), 0);
    assert_null(logger.queue.head); assert_non_null(strstr(written, "first"));
    assert_non_null(strstr(written, "second")); assert_int_equal(closes, 1);
    int before = joins; assert_int_equal(host_logger_close(&logger), 0); assert_int_equal(joins, before);
}
static void logger_join_failure_keeps_queue_for_retry(void **state) {
    (void)state; host_log(&logger, "retained"); join_error = EDEADLK;
    assert_int_equal(host_logger_close(&logger), EDEADLK); assert_false(logger.closed);
    assert_non_null(logger.queue.head); assert_int_equal(written_length, 0);
    assert_int_equal(host_logger_close(&logger), 0); assert_non_null(strstr(written, "retained"));
}
static void diagnostic_drains_both_attempts_and_their_logs(void **state) {
    (void)state;
    for (int i = 0; i < 2; i++) { children[i].call_id = (uint64_t)(31 + i); host_diagnostic_request(&writer, &children[i]); }
    assert_int_equal(writer.count, 2);
    assert_int_equal(host_diagnostic_close(&writer), 0);
    assert_int_equal(publications, 2); assert_int_equal(wakes, 2); assert_int_equal(writer.count, 0);
    assert_int_equal(writer.outstanding, 0);
    for (int i = 0; i < 2; i++) assert_int_equal(children[i].diagnostic_state, HOST_DIAGNOSTIC_IDLE);
    assert_int_equal(host_logger_close(&logger), 0);
    assert_non_null(strstr(written, "call=31")); assert_non_null(strstr(written, "call=32"));
    assert_non_null(strstr(written, "diagnostic_write_failed step=open_processing"));
}
static void pending_diagnostic_is_enqueued_before_close(void **state) {
    (void)state; children[0].diagnostic_state = HOST_DIAGNOSTIC_PENDING;
    host_diagnostic_dispatch_pending(&writer, children);
    assert_int_equal(children[0].diagnostic_state, HOST_DIAGNOSTIC_QUEUED);
    assert_int_equal(host_diagnostic_close(&writer), 0); assert_int_equal(publications, 1);
    assert_int_equal(children[0].diagnostic_state, HOST_DIAGNOSTIC_IDLE);
}
static void diagnostic_join_failure_keeps_owned_attempt(void **state) {
    (void)state; host_diagnostic_request(&writer, &children[0]); join_error = ESRCH;
    assert_int_equal(host_diagnostic_close(&writer), ESRCH); assert_false(writer.closed);
    assert_int_equal(children[0].diagnostic_state, HOST_DIAGNOSTIC_QUEUED);
    assert_int_equal(publications, 0);
    assert_int_equal(host_diagnostic_close(&writer), 0); assert_int_equal(publications, 1);
}
int main(void) {
    const struct CMUnitTest tests[] = {
        cmocka_unit_test_setup_teardown(logger_drains_queued_records, setup, teardown),
        cmocka_unit_test_setup_teardown(logger_join_failure_keeps_queue_for_retry, setup, teardown),
        cmocka_unit_test_setup_teardown(diagnostic_drains_both_attempts_and_their_logs, setup, teardown),
        cmocka_unit_test_setup_teardown(pending_diagnostic_is_enqueued_before_close, setup, teardown),
        cmocka_unit_test_setup_teardown(diagnostic_join_failure_keeps_owned_attempt, setup, teardown),
    };
    return cmocka_run_group_tests(tests, NULL, NULL);
}
