#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <errno.h>
#include <string.h>
#include "input.h"
#include "config.h"
static void capacity_includes_active(void **p) {
    (void)p;
    host_input items[2];
    host_inputs q = {.items = items, .capacity = 2};
    char path[] = "/A";
    assert_int_equal(host_inputs_push(&q, path, 1), 0);
    path[1] = 'X';
    assert_string_equal(host_inputs_front(&q)->path, "/A");
    assert_int_equal(host_inputs_push(&q, "/B", 2), 0);
    assert_int_equal(host_inputs_push(&q, "/C", 3), EAGAIN);
    assert_int_equal(q.count, 2);
    assert_string_equal(host_inputs_front(&q)->path, "/A");
    host_inputs_pop(&q);
    assert_string_equal(host_inputs_front(&q)->path, "/B");
    assert_int_equal(host_inputs_push(&q, "/C", 3), 0);
    host_inputs_pop(&q);
    assert_string_equal(host_inputs_front(&q)->path, "/C");
    host_inputs_pop(&q);
    assert_null(host_inputs_front(&q));
}
static camctl_host_config config(void) {
    camctl_host_config c = CAMCTL_HOST_CONFIG_INIT;
    c.camctl_path = "/bin/camctl";
    c.ready_path = "/ready";
    c.processing_path = "/processing";
    c.log_path = "/log";
    return c;
}
static void invalid_config(void **p) {
    (void)p;
    camctl_host_config c = config();
    assert_int_equal(host_config_validate(NULL, NULL), EINVAL);
    c.plan_capacity = 0;
    assert_int_equal(host_config_validate(&c, NULL), EINVAL);
    c = config();
    c.stdout_capacity = (size_t)-1;
    assert_int_equal(host_config_validate(&c, NULL), EINVAL);
    c = config();
    c.log_queue_capacity = 1;
    assert_int_equal(host_config_validate(&c, NULL), EINVAL);
    c = config();
    c.log_record_capacity = 1;
    assert_int_equal(host_config_validate(&c, NULL), EINVAL);
    c = config();
    c.log_file_size = 1;
    assert_int_equal(host_config_validate(&c, NULL), EINVAL);
    c = config();
    c.log_file_count = 0;
    assert_int_equal(host_config_validate(&c, NULL), EINVAL);
}
static void invalid_paths(void **p) {
    (void)p;
    camctl_host_config c = config();
    assert_int_equal(host_path_validate(NULL), EINVAL);
    assert_int_equal(host_path_validate("relative"), EINVAL);
    assert_int_equal(host_config_validate(&c, ""), EINVAL);
    char long_path[CAMCTL_HOST_PATH_MAX + 2];
    memset(long_path, 'x', sizeof(long_path));
    long_path[0] = '/';
    long_path[sizeof(long_path) - 1] = 0;
    assert_int_equal(host_path_validate(long_path), ENAMETOOLONG);
    long_path[sizeof(long_path) - 2] = 0;
    assert_int_equal(host_path_validate(long_path), 0);
}
static void valid_overrides(void **p) {
    (void)p;
    camctl_host_config c = config();
    assert_int_equal(host_config_validate(&c, NULL), 0);
    c.retry_limit = 0;
    c.retry_delay_ms = 0;
    c.config_path = "/配置/有 空格.toml";
    assert_int_equal(host_config_validate(&c, "/A"), 0);
}
int main(void) {
    const struct CMUnitTest t[] = {
        cmocka_unit_test(capacity_includes_active), cmocka_unit_test(invalid_config),
        cmocka_unit_test(invalid_paths), cmocka_unit_test(valid_overrides)};
    return cmocka_run_group_tests(t, NULL, NULL);
}
