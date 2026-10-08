#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include "scheduler.h"
static const host_result ok = {HOST_RESULT_SUCCESS, false};
static const host_result bad = {HOST_RESULT_ABNORMAL, false};
static const host_result error = {HOST_RESULT_ERROR, false};
static void initial(void **p) {
    (void)p;
    host_schedule s;
    host_schedule_init(&s, true, 3, 5000);
    assert_true(host_schedule_take(&s, HOST_RUN, 0));
    assert_true(s.first_plan);
    assert_int_equal(s.retries_used, 0);
    assert_false(host_schedule_take(&s, HOST_RUN, 0));
}
static void bounded_failures(void **p) {
    (void)p;
    host_schedule s;
    host_schedule_init(&s, true, 3, 5000);
    assert_true(host_schedule_take(&s, HOST_RUN, 0));
    for (unsigned i = 0; i < 3; i++) {
        host_schedule_spawn_failed(&s, HOST_RUN, i * 5000);
        assert_false(host_schedule_take(&s, HOST_RUN, (i + 1) * 5000 - 1));
        assert_true(host_schedule_take(&s, HOST_RUN, (i + 1) * 5000));
        assert_int_equal(s.retries_used, i + 1);
        assert_true(s.first_plan);
    }
    host_schedule_spawn_failed(&s, HOST_RUN, 15000);
    assert_false(host_schedule_take(&s, HOST_RUN, 90000));
    host_schedule_need_run(&s);
    assert_true(host_schedule_take(&s, HOST_RUN, 90000));
    assert_true(s.first_plan);
    assert_int_equal(s.retries_used, 3);
    host_schedule_spawn_failed(&s, HOST_RUN, 90000);
    assert_false(host_schedule_take(&s, HOST_RUN, 100000));
    assert_true(s.pending_run);
}
static void merge_preserves_unstarted_A(void **p) {
    (void)p;
    host_schedule s;
    host_schedule_init(&s, true, 3, 5000);
    assert_true(host_schedule_take(&s, HOST_RUN, 0));
    host_schedule_spawn_failed(&s, HOST_RUN, 0);
    host_schedule_need_run(&s);
    host_schedule_need_run(&s);
    assert_true(host_schedule_take(&s, HOST_RUN, 10));
    assert_true(s.first_plan);
    assert_int_equal(s.retries_used, 0);
    host_schedule_spawned(&s, HOST_RUN);
    assert_false(s.pending_run);
    assert_false(s.first_plan);
}
static void later_requirement_survives_start(void **p) {
    (void)p;
    host_schedule s;
    host_schedule_init(&s, false, 3, 5000);
    assert_true(host_schedule_take(&s, HOST_RUN, 0));
    host_schedule_need_run(&s);
    host_schedule_spawned(&s, HOST_RUN);
    assert_true(s.pending_run);
    assert_false(host_schedule_take(&s, HOST_RUN, 0));
    host_schedule_finished(&s, HOST_RUN, error, 0);
    assert_true(host_schedule_take(&s, HOST_RUN, 0));
}
static void active_pending_survives_false_and_error(void **p) {
    (void)p;
    host_schedule s;
    host_schedule_init(&s, true, 3, 5000);
    assert_true(host_schedule_take(&s, HOST_RUN, 0));
    host_schedule_spawned(&s, HOST_RUN);
    host_schedule_need_run(&s);
    host_schedule_finished(&s, HOST_SUBMIT, ok, 0);
    host_schedule_finished(&s, HOST_SUBMIT, error, 0);
    assert_true(s.pending_run);
    host_schedule_finished(&s, HOST_RUN, bad, 0);
    assert_true(host_schedule_take(&s, HOST_RUN, 0));
    assert_int_equal(s.retries_used, 0);
    assert_false(s.first_plan);
}
static void success_does_not_reset_budget(void **p) {
    (void)p;
    host_schedule s;
    host_schedule_init(&s, true, 3, 5000);
    assert_true(host_schedule_take(&s, HOST_RUN, 0));
    host_schedule_spawned(&s, HOST_RUN);
    host_schedule_finished(&s, HOST_RUN, bad, 0);
    assert_true(host_schedule_take(&s, HOST_RUN, 5000));
    host_schedule_spawned(&s, HOST_RUN);
    host_schedule_finished(&s, HOST_RUN, ok, 5000);
    assert_int_equal(s.retries_used, 1);
    assert_false(host_schedule_take(&s, HOST_RUN, 99999));
}
static void shared_budget_rechecked(void **p) {
    (void)p;
    host_schedule s;
    host_schedule_init(&s, false, 1, 5000);
    assert_true(host_schedule_take(&s, HOST_RUN, 0));
    host_schedule_spawn_failed(&s, HOST_RUN, 0);
    host_schedule_submit(&s);
    assert_true(host_schedule_take(&s, HOST_SUBMIT, 0));
    host_schedule_spawn_failed(&s, HOST_SUBMIT, 0);
    assert_true(host_schedule_take(&s, HOST_RUN, 5000));
    assert_false(host_schedule_take(&s, HOST_SUBMIT, 5000));
    assert_int_equal(s.jobs[HOST_SUBMIT].phase, HOST_IDLE);
    host_schedule_submit(&s);
    assert_true(host_schedule_take(&s, HOST_SUBMIT, 5000));
    assert_int_equal(s.retries_used, 1);
}
static void started_submit_never_retried(void **p) {
    (void)p;
    host_schedule s;
    host_schedule_init(&s, false, 3, 1);
    host_schedule_submit(&s);
    assert_true(host_schedule_take(&s, HOST_SUBMIT, 0));
    host_schedule_spawned(&s, HOST_SUBMIT);
    host_schedule_finished(&s, HOST_SUBMIT, bad, 0);
    assert_false(host_schedule_take(&s, HOST_SUBMIT, 100));
    assert_int_equal(s.retries_used, 0);
}
static void legal_error_not_retried(void **p) {
    (void)p;
    host_schedule s;
    host_schedule_init(&s, false, 3, 1);
    assert_true(host_schedule_take(&s, HOST_RUN, 0));
    host_schedule_spawned(&s, HOST_RUN);
    host_schedule_finished(&s, HOST_RUN, error, 0);
    assert_false(host_schedule_take(&s, HOST_RUN, 100));
}
static void abnormal_retry_uses_completion_deadline(void **p) {
    (void)p;
    host_schedule s;
    host_schedule_init(&s, false, 1, 200);
    assert_true(host_schedule_take(&s, HOST_RUN, 1000));
    host_schedule_spawned(&s, HOST_RUN);
    host_schedule_finished(&s, HOST_RUN, bad, 1300);
    assert_false(host_schedule_take(&s, HOST_RUN, 1499));
    assert_int_equal(s.retries_used, 0);
    assert_true(host_schedule_take(&s, HOST_RUN, 1500));
    assert_int_equal(s.retries_used, 1);
}
int main(void) {
    const struct CMUnitTest tests[] = {cmocka_unit_test(initial),
                                       cmocka_unit_test(bounded_failures),
                                       cmocka_unit_test(merge_preserves_unstarted_A),
                                       cmocka_unit_test(later_requirement_survives_start),
                                       cmocka_unit_test(active_pending_survives_false_and_error),
                                       cmocka_unit_test(success_does_not_reset_budget),
                                       cmocka_unit_test(shared_budget_rechecked),
                                       cmocka_unit_test(started_submit_never_retried),
                                       cmocka_unit_test(legal_error_not_retried),
                                       cmocka_unit_test(abnormal_retry_uses_completion_deadline)};
    return cmocka_run_group_tests(tests, NULL, NULL);
}
