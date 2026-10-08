#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <errno.h>
#include "callbacks.h"
#include "config.h"
#include "notification.h"
static void motor(int p) { (void)p; }
static void registration(void **state) {
    (void)state;
    host_callbacks c = {0};
    assert_int_equal(host_callbacks_register(&c, NULL), EINVAL);
    assert_int_equal(host_callbacks_register(&c, motor), 0);
    assert_ptr_equal(c.motor, motor);
    assert_int_equal(host_callbacks_register(&c, NULL), EINVAL);
    assert_int_equal(host_callbacks_register(&c, motor), EALREADY);
    /* 失败的初始化不修改独立注册状态。 */
    assert_int_equal(host_callbacks_register(&c, motor), EALREADY);
    c.initialized = true;
    assert_int_equal(host_callbacks_register(&c, NULL), EINVAL);
    assert_int_equal(host_callbacks_register(&c, motor), EBUSY);
}
static void config_bounds(void **state) {
    (void)state;
    camctl_host_config c = CAMCTL_HOST_CONFIG_INIT;
    c.camctl_path = "/cli";
    c.ready_path = "/ready";
    c.processing_path = "/processing";
    c.log_path = "/log";
    assert_int_equal(host_config_validate(&c, NULL), 0);
    assert_int_equal(
        host_notification_buffer_bound(c.notification_line_capacity, c.notification_queue_capacity),
        61956);
    assert_int_equal(host_notification_buffer_bound(SIZE_MAX, SIZE_MAX), 0);
    c.notification_line_capacity = CAMCTL_HOST_NOTIFICATION_LINE_MAX;
    c.notification_queue_capacity = CAMCTL_HOST_NOTIFICATION_QUEUE_MAX;
    assert_int_equal(host_config_validate(&c, NULL), 0);
    assert_int_equal(
        host_notification_buffer_bound(c.notification_line_capacity, c.notification_queue_capacity),
        938244);
    c.notification_line_capacity++;
    assert_int_equal(host_config_validate(&c, NULL), EINVAL);
    c.notification_line_capacity = CAMCTL_HOST_NOTIFICATION_LINE_MAX;
    c.notification_queue_capacity++;
    assert_int_equal(host_config_validate(&c, NULL), EINVAL);
    c.notification_line_capacity = 1;
    c.notification_queue_capacity = 1;
    assert_int_equal(host_config_validate(&c, NULL), 0);
    c.notification_line_capacity = 0;
    assert_int_equal(host_config_validate(&c, NULL), EINVAL);
    c.notification_line_capacity = SIZE_MAX;
    assert_int_equal(host_config_validate(&c, NULL), EINVAL);
    c.notification_line_capacity = CAMCTL_HOST_NOTIFICATION_LINE_DEFAULT;
    c.notification_queue_capacity = 0;
    assert_int_equal(host_config_validate(&c, NULL), EINVAL);
    c.notification_queue_capacity = SIZE_MAX;
    assert_int_equal(host_config_validate(&c, NULL), EINVAL);
}
int main(void) {
    const struct CMUnitTest t[] = {cmocka_unit_test(registration), cmocka_unit_test(config_bounds)};
    return cmocka_run_group_tests(t, NULL, NULL);
}
