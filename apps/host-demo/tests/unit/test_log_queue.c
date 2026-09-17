#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include "log_queue.h"
static void fifo(void **p) {
    (void)p;
    host_log_queue q;
    host_log_queue_init(&q, 4096, 128);
    assert_true(host_log_queue_push(&q, "one", 3));
    assert_true(host_log_queue_push(&q, "two", 3));
    host_log_record *r = host_log_queue_pop(&q);
    assert_string_equal(r->text, "one\n");
    free(r);
    r = host_log_queue_pop(&q);
    assert_string_equal(r->text, "two\n");
    free(r);
    assert_null(host_log_queue_pop(&q));
    assert_int_equal(q.used, 0);
}
static void bounded_drop_and_recovery(void **p) {
    (void)p;
    host_log_queue q;
    host_log_queue_init(&q, 256, 128);
    char text[200];
    memset(text, 'x', sizeof(text));
    assert_true(host_log_queue_push(&q, text, sizeof(text)));
    assert_false(host_log_queue_push(&q, text, sizeof(text)));
    assert_int_equal(q.dropped, 1);
    host_log_record *r = host_log_queue_pop(&q);
    assert_non_null(r);
    free(r);
    r = host_log_queue_pop(&q);
    assert_non_null(r);
    assert_non_null(strstr(r->text, "dropped=1"));
    free(r);
    assert_int_equal(q.dropped, 0);
    assert_int_equal(q.used, 0);
}
static void truncation(void **p) {
    (void)p;
    host_log_queue q;
    host_log_queue_init(&q, 4096, 128);
    char text[200];
    memset(text, 'x', sizeof(text));
    assert_true(host_log_queue_push(&q, text, sizeof(text)));
    host_log_record *r = host_log_queue_pop(&q);
    assert_int_equal(r->length, 128);
    assert_non_null(strstr(r->text, "[truncated]"));
    free(r);
}
static void disable_discards_pending(void **p) {
    (void)p;
    host_log_queue q;
    host_log_queue_init(&q, 4096, 128);
    assert_true(host_log_queue_push(&q, "one", 3));
    host_log_queue_disable(&q, ENOSPC);
    assert_int_equal(q.file_error, ENOSPC);
    assert_int_equal(q.used, 0);
    assert_null(host_log_queue_pop(&q));
    assert_false(host_log_queue_push(&q, "two", 3));
    host_log_queue_disable(&q, EIO);
    assert_int_equal(q.file_error, ENOSPC);
}
int main(void) {
    const struct CMUnitTest t[] = {
        cmocka_unit_test(fifo), cmocka_unit_test(bounded_drop_and_recovery),
        cmocka_unit_test(truncation), cmocka_unit_test(disable_discards_pending)};
    return cmocka_run_group_tests(t, NULL, NULL);
}
