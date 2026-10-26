#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "demo_stop.h"

static jmp_buf finished, duplicate_finished;
static int mask_error, create_error, wait_error, stop_error;
static int mask_calls, create_calls, stop_calls, exit_status, wait_signal;
static void *(*waiting_thread)(void *);
static void *waiting_argument;
static char events[32], diagnostic[512];
static size_t event_count;
static bool duplicate_request, in_duplicate;
static demo_stop *active_stop;

static void event(char value) {
    assert_true(event_count + 1 < sizeof(events));
    events[event_count++] = value;
    events[event_count] = 0;
}
static void assert_stop_signals(const sigset_t *signals) {
    for (int number = 1; number < NSIG; number++)
        assert_int_equal(sigismember(signals, number), number == SIGINT || number == SIGTERM);
}
int __wrap_pthread_sigmask(int how, const sigset_t *signals, sigset_t *old) {
    assert_int_equal(how, SIG_BLOCK);
    assert_stop_signals(signals);
    assert_null(old);
    mask_calls++;
    event('M');
    return mask_error;
}
int __wrap_pthread_create(pthread_t *thread, const pthread_attr_t *attributes,
                          void *(*entry)(void *), void *argument) {
    assert_non_null(thread);
    assert_null(attributes);
    assert_non_null(entry);
    assert_non_null(argument);
    create_calls++;
    waiting_thread = entry;
    waiting_argument = argument;
    event('C');
    return create_error;
}
int __wrap_sigwait(const sigset_t *signals, int *number) {
    assert_stop_signals(signals);
    assert_non_null(number);
    *number = wait_signal;
    event('W');
    return wait_error;
}
int __wrap_camctl_host_stop(void) {
    stop_calls++;
    event('S');
    if (duplicate_request) {
        in_duplicate = true;
        if (!setjmp(duplicate_finished)) {
            demo_stop_exit(active_stop, EXIT_SUCCESS);
        }
        in_duplicate = false;
    }
    errno = stop_error;
    return stop_error ? -1 : 0;
}
int __wrap_fprintf(FILE *stream, const char *format, ...) {
    assert_ptr_equal(stream, stderr);
    va_list arguments;
    va_start(arguments, format);
    int length = vsnprintf(diagnostic, sizeof(diagnostic), format, arguments);
    va_end(arguments);
    event('D');
    return length;
}
_Noreturn void __wrap_exit(int status) {
    assert_false(in_duplicate);
    exit_status = status;
    event('E');
    longjmp(finished, 1);
}
_Noreturn void __wrap_pthread_exit(void *result) {
    assert_null(result);
    event('T');
    if (in_duplicate)
        longjmp(duplicate_finished, 1);
    longjmp(finished, 1);
}
static void reset(void) {
    mask_error = create_error = wait_error = stop_error = 0;
    mask_calls = create_calls = stop_calls = 0;
    exit_status = -1;
    wait_signal = SIGINT;
    waiting_thread = NULL;
    waiting_argument = NULL;
    event_count = 0;
    events[0] = diagnostic[0] = 0;
    duplicate_request = in_duplicate = false;
    active_stop = NULL;
}
static void prepare(demo_stop *stop) {
    reset();
    assert_int_equal(demo_stop_prepare(stop), 0);
    event_count = 0;
    events[0] = 0;
}
static void blocks_signals_before_threads(void **state) {
    (void)state;
    demo_stop stop;
    reset();
    assert_int_equal(demo_stop_prepare(&stop), 0);
    assert_int_equal(mask_calls, 1);
    assert_int_equal(create_calls, 0);
    assert_int_equal(stop_calls, 0);
}
static void mask_failure_preserves_error(void **state) {
    (void)state;
    demo_stop stop;
    reset();
    mask_error = EINVAL;
    errno = EIO;
    assert_int_equal(demo_stop_prepare(&stop), -1);
    assert_int_equal(errno, EINVAL);
    assert_int_equal(create_calls, 0);
    assert_int_equal(stop_calls, 0);
}
static void starts_waiting_thread_without_stopping(void **state) {
    (void)state;
    demo_stop stop;
    prepare(&stop);
    demo_stop_start(&stop);
    assert_int_equal(create_calls, 1);
    assert_non_null(waiting_thread);
    assert_ptr_equal(waiting_argument, &stop);
    assert_string_equal(events, "C");
}
static void thread_failure_stops_before_failure_exit(void **state) {
    (void)state;
    demo_stop stop;
    prepare(&stop);
    create_error = EAGAIN;
    errno = EIO;
    if (!setjmp(finished)) {
        demo_stop_start(&stop);
        fail_msg("%s", "信号等待线程创建失败后仍然返回主流程");
    }
    assert_string_equal(events, "CDSE");
    assert_int_equal(stop_calls, 1);
    assert_int_equal(exit_status, EXIT_FAILURE);
    assert_non_null(strstr(diagnostic, strerror(EAGAIN)));
}
static void commands_stop_before_success_exit(void **state) {
    (void)state;
    const char *commands[] = {"quit", "exit"};
    for (size_t i = 0; i < sizeof(commands) / sizeof(commands[0]); i++) {
        demo_stop stop;
        prepare(&stop);
        if (!setjmp(finished)) {
            demo_stop_command(&stop, commands[i]);
            fail_msg("%s", "退出命令未完成停止和退出");
        }
        assert_string_equal(events, "SE");
        assert_int_equal(stop_calls, 1);
        assert_int_equal(exit_status, EXIT_SUCCESS);
    }
}
static void other_commands_continue_without_stopping(void **state) {
    (void)state;
    demo_stop stop;
    prepare(&stop);
    assert_false(demo_stop_command(&stop, "help"));
    assert_false(demo_stop_command(&stop, "quit extra"));
    assert_false(demo_stop_command(&stop, "exit "));
    assert_false(demo_stop_command(&stop, ""));
    assert_int_equal(stop_calls, 0);
    assert_string_equal(events, "");
}
static void signals_stop_before_success_exit(void **state) {
    (void)state;
    const int signals[] = {SIGINT, SIGTERM};
    for (size_t i = 0; i < sizeof(signals) / sizeof(signals[0]); i++) {
        demo_stop stop;
        prepare(&stop);
        demo_stop_start(&stop);
        assert_non_null(waiting_thread);
        event_count = 0;
        events[0] = 0;
        wait_signal = signals[i];
        if (!setjmp(finished)) {
            waiting_thread(waiting_argument);
            fail_msg("%s", "收到停止信号后等待线程返回");
        }
        assert_string_equal(events, "WSE");
        assert_int_equal(stop_calls, 1);
        assert_int_equal(exit_status, EXIT_SUCCESS);
    }
}
static void wait_failure_stops_before_failure_exit(void **state) {
    (void)state;
    demo_stop stop;
    prepare(&stop);
    demo_stop_start(&stop);
    assert_non_null(waiting_thread);
    event_count = 0;
    events[0] = 0;
    wait_error = EINVAL;
    errno = EIO;
    if (!setjmp(finished)) {
        waiting_thread(waiting_argument);
        fail_msg("%s", "信号等待失败后线程返回");
    }
    assert_string_equal(events, "WDSE");
    assert_int_equal(stop_calls, 1);
    assert_int_equal(exit_status, EXIT_FAILURE);
    assert_non_null(strstr(diagnostic, strerror(EINVAL)));
}
static void stop_failure_cannot_report_success_exit(void **state) {
    (void)state;
    demo_stop stop;
    prepare(&stop);
    stop_error = EIO;
    if (!setjmp(finished))
        demo_stop_exit(&stop, EXIT_SUCCESS);
    assert_string_equal(events, "SDE");
    assert_int_equal(stop_calls, 1);
    assert_int_equal(exit_status, EXIT_FAILURE);
    assert_non_null(strstr(diagnostic, strerror(EIO)));
}
static void requested_failure_remains_failure_after_stop(void **state) {
    (void)state;
    demo_stop stop;
    prepare(&stop);
    if (!setjmp(finished))
        demo_stop_exit(&stop, EXIT_FAILURE);
    assert_string_equal(events, "SE");
    assert_int_equal(exit_status, EXIT_FAILURE);
}
static void concurrent_request_has_one_exit_owner(void **state) {
    (void)state;
    demo_stop stop;
    prepare(&stop);
    active_stop = &stop;
    duplicate_request = true;
    if (!setjmp(finished))
        demo_stop_exit(&stop, EXIT_SUCCESS);
    assert_string_equal(events, "STE");
    assert_int_equal(stop_calls, 1);
    assert_int_equal(exit_status, EXIT_SUCCESS);
}
int main(void) {
    const struct CMUnitTest tests[] = {
        cmocka_unit_test(blocks_signals_before_threads),
        cmocka_unit_test(mask_failure_preserves_error),
        cmocka_unit_test(starts_waiting_thread_without_stopping),
        cmocka_unit_test(thread_failure_stops_before_failure_exit),
        cmocka_unit_test(commands_stop_before_success_exit),
        cmocka_unit_test(other_commands_continue_without_stopping),
        cmocka_unit_test(signals_stop_before_success_exit),
        cmocka_unit_test(wait_failure_stops_before_failure_exit),
        cmocka_unit_test(stop_failure_cannot_report_success_exit),
        cmocka_unit_test(requested_failure_remains_failure_after_stop),
        cmocka_unit_test(concurrent_request_has_one_exit_owner)};
    return cmocka_run_group_tests(tests, NULL, NULL);
}
