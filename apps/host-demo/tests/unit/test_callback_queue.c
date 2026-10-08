#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include "callback_queue.h"
static void fifo(void **state) {
    (void)state;
    host_notification items[2], v;
    host_callback_queue q = {items, 2, 0, 0};
    assert_false(host_callback_queue_pop(&q, &v));
    assert_true(host_callback_queue_push(&q, (host_notification){-1}));
    assert_true(host_callback_queue_push(&q, (host_notification){0}));
    assert_false(host_callback_queue_push(&q, (host_notification){99}));
    assert_true(host_callback_queue_pop(&q, &v));
    assert_int_equal(v.position, -1);
    assert_true(host_callback_queue_push(&q, (host_notification){1}));
    assert_true(host_callback_queue_pop(&q, &v));
    assert_int_equal(v.position, 0);
    assert_true(host_callback_queue_pop(&q, &v));
    assert_int_equal(v.position, 1);
    assert_false(host_callback_queue_pop(&q, &v));
}
int main(void) {
    const struct CMUnitTest t[] = {cmocka_unit_test(fifo)};
    return cmocka_run_group_tests(t, NULL, NULL);
}
