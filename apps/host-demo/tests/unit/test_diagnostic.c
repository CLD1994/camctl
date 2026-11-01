#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <string.h>
#include <errno.h>
#include "diagnostic.h"

static int sync_calls, fail_sync, renames, unlinks;
int __wrap_open(const char *path, int flags, ...) { (void)path; (void)flags; return 10; }
int __wrap_openat(int fd, const char *path, int flags, ...) {
    (void)fd; (void)flags; return !strcmp(path, "record.tmp") ? 12 : 11;
}
char *__wrap_mkdtemp(char *path) {
    size_t n = strlen(path); assert_true(n >= 6); memcpy(path + n - 6, "abcdef", 6); return path;
}
int __wrap_fsync(int fd) {
    (void)fd; ++sync_calls;
    if (sync_calls == fail_sync) { errno = EIO; return -1; }
    return 0;
}
int __wrap_host_diagnostic_rename_noreplace(int oldfd, const char *oldpath, int newfd, const char *newpath) {
    (void)oldfd; (void)oldpath; (void)newfd; (void)newpath; renames++; return 0;
}
int __wrap_unlinkat(int fd, const char *path, int flags) {
    (void)fd; (void)path; (void)flags; unlinks++; return 0;
}
int __wrap_close(int fd) { (void)fd; return 0; }
static int write_payload(int fd, void *context) {
    (void)context; assert_int_equal(fd, 12); return 0;
}

static void update_publication_preserves_preparation_and_never_repeats_rename(void **state) {
    (void)state; sync_calls = renames = unlinks = 0; fail_sync = 2;
    host_diagnostic_publication result = host_diagnostic_publish_update("/processing", write_payload, NULL);
    assert_int_equal(result.error, EIO); assert_true(result.complete); assert_false(result.published);
    assert_non_null(strstr(result.preparation_name, "abcdef"));
    assert_int_equal(renames, 0); assert_int_equal(unlinks, 0);
    sync_calls = renames = unlinks = 0; fail_sync = 3;
    result = host_diagnostic_publish_update("/processing", write_payload, NULL);
    assert_int_equal(result.error, EIO); assert_true(result.complete); assert_true(result.published);
    assert_non_null(strstr(result.file_name, "abcdef"));
    assert_int_equal(renames, 1); assert_int_equal(unlinks, 1);
}

static void format_contains_context_and_bounded_lengths(void **state) {
    (void)state;
    camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
    config.camctl_path = "/opt/camctl";
    config.config_path = "/tmp/config.toml";
    host_child child = {0};
    child.call_id = 17;
    child.attempt_id = 23;
    child.command = HOST_RUN;
    child.automatic = true;
    child.retries_used = 2;
    strcpy(child.plan_path, "/tmp/plan.json");
    strcpy(child.working_directory, "/work/run");
    child.started = true;
    child.completed_pid = child.completed_pgid = 123;
    child.result_kind = HOST_RESULT_ERROR;
    child.termination = HOST_EXIT_NORMAL;
    child.exit_code = 1;
    child.output_bytes_seen = 42;
    child.length = 4;
    child.overflow = true;
    child.stderr_bytes_seen = 9;
    child.stderr_length = 3;
    child.stderr_overflow = true;
    char header[40 * 1024];
    size_t length = 0;
    assert_int_equal(host_diagnostic_format_header(&child, &config, header, sizeof(header),
                                                   &length), 0);
    assert_true(length > 0 && length < sizeof(header));
    assert_non_null(strstr(header, "call_id=17\n"));
    assert_non_null(strstr(header, "attempt_id=23\n"));
    assert_non_null(strstr(header, "classification=error\n"));
    assert_non_null(strstr(header, "stdout_bytes_seen=42\n"));
    assert_non_null(strstr(header, "stdout_bytes_saved=4\n"));
    assert_non_null(strstr(header, "stdout_truncated=1\n"));
    assert_non_null(strstr(header, "stderr_bytes_seen=9\n"));
    assert_non_null(strstr(header, "stderr_bytes_saved=3\n"));
    assert_non_null(strstr(header, "stderr_truncated=1\n"));
    assert_non_null(strstr(header, "plan_path_hex=2f746d702f706c616e2e6a736f6e\n"));
    assert_non_null(strstr(header, "working_directory_hex=2f776f726b2f72756e\n"));
}

int main(void) {
    const struct CMUnitTest tests[] = {
        cmocka_unit_test(format_contains_context_and_bounded_lengths),
        cmocka_unit_test(update_publication_preserves_preparation_and_never_repeats_rename),
    };
    return cmocka_run_group_tests(tests, NULL, NULL);
}
