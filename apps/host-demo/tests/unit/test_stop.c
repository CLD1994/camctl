#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
/* 执行公开入口和生产 worker/callback；所有进程、等待和文件接口均为替身。 */
#include "../../src/host.c"

static host owned;
static int spawns, requests[2], collects, polls, joins, diag_closes, log_closes;
static int callbacks, positions[4], waits, join_error;
static bool poll_error, callback_work, start_submit, borrower_done, other_stop_done;
static bool notification_eof, clock_error, stop_before_admission;
static host_result results[2];
void __wrap_host_log(host_logger *l, const char *f, ...) { (void)l; (void)f; }
ssize_t __wrap_write(int fd, const void *p, size_t n) {
    (void)p; assert_int_equal(fd, owned.wake[1]); return (ssize_t)n;
}
ssize_t __wrap_read(int fd, void *p, size_t n) {
    if (notification_eof && fd == 20) return 0;
    (void)fd; (void)p; (void)n; errno = EAGAIN; return -1;
}
int __wrap_close(int fd) { assert_true(fd >= 0); return 0; }
int __wrap_clock_gettime(clockid_t id, struct timespec *t) {
    if (clock_error) { clock_error = false; errno = EIO; return -1; }
    assert_int_equal(id, CLOCK_MONOTONIC); *t = (struct timespec){1, 0}; return 0;
}
int __real_pthread_cond_init(pthread_cond_t *, const pthread_condattr_t *);
int __wrap_pthread_cond_init(pthread_cond_t *condition, const pthread_condattr_t *attr) {
    if (stop_before_admission) {
        assert_int_equal(owned.api_users, 1);
        owned.lifecycle = HOST_STOPPING; stop_before_admission = false;
    }
    return __real_pthread_cond_init(condition, attr);
}
char *__wrap_getcwd(char *p, size_t n) { assert_true(n > 1); strcpy(p, "/"); return p; }
void __wrap_host_group_scan_reset(host_group_scan *s) { (void)s; }
void __wrap_host_child_close_notifications(host_child *c) {
    c->notification_fd = c->notification_write_fd = -1;
}
void __wrap_host_child_collect(host_child *c, host_child_log log, void *p) {
    (void)log; (void)p; collects++;
    if (c->stop_requested) {
        c->ended = c->reaped = true;
        c->out_fd = c->err_fd = c->notification_fd = -1;
    }
}
bool __wrap_host_child_done(const host_child *c) {
    return c->reaped && c->out_fd < 0 && c->err_fd < 0 && c->notification_fd < 0;
}
void __wrap_host_child_request_stop(host_child *c, host_child_log log, void *p) {
    (void)log; (void)p; assert_int_equal(owned.lifecycle, HOST_STOPPING);
    if (c->pid && !c->ended && !c->stop_requested) {
        size_t i = (size_t)(c - owned.children); assert_true(i < 2);
        requests[i]++; c->stop_requested = true;
    }
}
int __wrap_host_child_spawn(host_child *c, const camctl_host_config *cfg,
                            host_command command, const char *p) {
    (void)c; (void)cfg; (void)command; (void)p; spawns++; return EAGAIN;
}
host_result __wrap_host_result_parse(host_command command, const char *p, size_t n,
                                     host_exit exit, int code) {
    (void)p; (void)n; (void)exit; (void)code; return results[command];
}
bool __wrap_host_diagnostic_busy(host_diagnostic_writer *w, const host_child *c) {
    (void)w; return c->diagnostic_state != HOST_DIAGNOSTIC_IDLE;
}
void __wrap_host_diagnostic_request(host_diagnostic_writer *w, host_child *c) {
    (void)w; c->diagnostic_state = HOST_DIAGNOSTIC_PENDING;
}
void __wrap_host_diagnostic_dispatch_pending(host_diagnostic_writer *w, host_child c[2]) {
    (void)w;
    for (int i = 0; i < 2; i++) {
        switch (c[i].diagnostic_state) {
        case HOST_DIAGNOSTIC_PENDING: c[i].diagnostic_state = HOST_DIAGNOSTIC_QUEUED; break;
        case HOST_DIAGNOSTIC_QUEUED: c[i].diagnostic_state = HOST_DIAGNOSTIC_WRITING; break;
        case HOST_DIAGNOSTIC_WRITING: c[i].diagnostic_state = HOST_DIAGNOSTIC_IDLE; break;
        default: break;
        }
    }
}
int __wrap_host_diagnostic_close(host_diagnostic_writer *w) {
    assert_int_equal(owned.api_users, 0);
    for (int i = 0; i < 2; i++) assert_int_equal(owned.children[i].diagnostic_state, HOST_DIAGNOSTIC_IDLE);
    w->closed = true; diag_closes++; return 0;
}
int __wrap_host_logger_close(host_logger *l) {
    assert_true(owned.diagnostics.closed); l->closed = true; log_closes++; return 0;
}
void __wrap_host_claim_posix(const camctl_host_config *cfg, host_logger *l) {
    (void)cfg; (void)l; assert_int_equal(owned.api_users, 1);
}
static void motor(int position) {
    positions[callbacks++] = position;
    assert_true(owned.callback_active);
    assert_int_equal(camctl_host_submit("/callback.json"), -1);
    assert_int_equal(errno, ECANCELED);
    assert_int_equal(camctl_host_stop(), -1);
    assert_int_equal(errno, EDEADLK);
}
int __wrap_poll(struct pollfd *fds, nfds_t n, int timeout) {
    assert_int_equal(timeout, 50); assert_true(++polls < 20);
    if (!fds) { assert_int_equal(n, 0); return 0; }
    if (poll_error) {
        poll_error = false; owned.lifecycle = HOST_STOPPING; errno = EBADF; return -1;
    }
    if (callback_work) { callback_work = false; callback_main(&owned); module_thread = true; }
    return 0;
}
int __wrap_pthread_join(pthread_t thread, void **p) {
    (void)p;
    assert_int_equal(pthread_mutex_trylock(&instance_mutex), 0);
    pthread_mutex_unlock(&instance_mutex);
    assert_int_equal(pthread_mutex_trylock(&owned.inputs_mutex), 0);
    pthread_mutex_unlock(&owned.inputs_mutex);
    joins++;
    if (join_error) { int e = join_error; join_error = 0; return e; }
    if (thread == owned.worker) { worker_main(&owned); module_thread = false; }
    else {
        assert_true(owned.motor && thread == owned.callback_thread);
        assert_true(owned.callback_stop); assert_false(owned.callback_active);
        assert_int_equal(owned.callbacks.length, 0);
    }
    return 0;
}
int __wrap_pthread_cond_wait(pthread_cond_t *condition, pthread_mutex_t *mutex) {
    assert_true(++waits < 8); pthread_mutex_unlock(mutex);
    if (condition == &owned.callback_ready) assert_true(stop_drained(&owned));
    else if (condition == &instance_ready) {
        if (borrower_done) { borrower_done = false; release_instance(&owned); }
        else {
            assert_true(other_stop_done); other_stop_done = false;
            owned.lifecycle = HOST_STOPPED; owned.stop_joining = false;
        }
    } else {
        owned.lifecycle = HOST_STOPPING;
        if (start_submit) {
            owned.children[HOST_SUBMIT].pid = owned.children[HOST_SUBMIT].pgid = 102;
            owned.schedule.jobs[HOST_SUBMIT].phase = HOST_ACTIVE;
        }
        worker_main(&owned); module_thread = false;
    }
    pthread_mutex_lock(mutex); return 0;
}
static int setup(void **state) {
    (void)state; memset(&owned, 0, sizeof(owned));
    owned.config = (camctl_host_config)CAMCTL_HOST_CONFIG_INIT;
    owned.worker = (pthread_t)1; owned.callback_thread = (pthread_t)2;
    owned.wake[0] = 10; owned.wake[1] = 11;
    owned.inputs.capacity = 4; owned.inputs.items = calloc(4, sizeof(host_input));
    pthread_mutex_init(&owned.inputs_mutex, NULL); pthread_mutex_init(&owned.claim_mutex, NULL);
    for (int i = 0; i < 2; i++) {
        host_child_init(&owned.children[i], malloc(64), 64, malloc(64), 64);
        results[i] = (host_result){HOST_RESULT_SUCCESS, true};
    }
    instance = &owned; module_thread = false; registrations.initialized = true;
    spawns = collects = polls = joins = diag_closes = log_closes = callbacks = waits = join_error = 0;
    requests[0] = requests[1] = 0;
    poll_error = callback_work = start_submit = borrower_done = other_stop_done = false;
    notification_eof = clock_error = stop_before_admission = false;
    return 0;
}
static int teardown(void **state) {
    (void)state; if (owned.inputs.items) release_storage(&owned);
    instance = NULL; module_thread = false; return 0;
}
static void uninitialized(void **state) {
    (void)state; instance = NULL;
    assert_int_equal(camctl_host_stop(), -1); assert_int_equal(errno, ENODEV); assert_int_equal(joins, 0);
}
static void terminal_idempotent(void **state) {
    (void)state; assert_int_equal(camctl_host_stop(), 0);
    assert_int_equal(owned.lifecycle, HOST_STOPPED); assert_int_equal(camctl_host_stop(), 0);
    assert_int_equal(joins, 1); assert_int_equal(log_closes, 1);
    assert_int_equal(camctl_host_init(NULL, NULL), -1); assert_int_equal(errno, EALREADY);
    assert_int_equal(camctl_host_submit("/later.json"), -1); assert_int_equal(errno, ECANCELED);
    camctl_host_claim(); assert_int_equal(errno, ECANCELED);
}
static void stops_both_no_restart(void **state) {
    (void)state;
    for (int i = 0; i < 2; i++) {
        owned.children[i].pid = owned.children[i].pgid = 101 + i;
        owned.schedule.jobs[i].phase = HOST_WAIT;
    }
    assert_int_equal(camctl_host_stop(), 0);
    assert_int_equal(requests[0], 1); assert_int_equal(requests[1], 1);
    assert_int_equal(spawns, 0); assert_true(collects >= 2);
}
static void stopped_launch(void **state) {
    (void)state; owned.lifecycle = HOST_STOPPING; host_schedule_init(&owned.schedule, false, 3, 0);
    launch(&owned, HOST_RUN, 0); assert_int_equal(spawns, 0);
}
static void queued_submit(void **state) {
    (void)state; assert_int_equal(camctl_host_submit("/queued.json"), -1);
    assert_int_equal(errno, ECANCELED); assert_int_equal(owned.inputs.count, 0);
    assert_int_equal(owned.api_users, 0); assert_int_equal(spawns, 0);
}
static void borrowed_submit_rechecks_stop_before_enqueue(void **state) {
    (void)state; stop_before_admission = true;
    assert_int_equal(camctl_host_submit("/racing.json"), -1);
    assert_int_equal(errno, ECANCELED); assert_int_equal(owned.api_users, 0);
    assert_int_equal(owned.inputs.count, 0); assert_int_equal(spawns, 0);
}
static void active_submit_success(void **state) {
    (void)state; start_submit = true; assert_int_equal(camctl_host_submit("/active.json"), 0);
    assert_int_equal(requests[HOST_SUBMIT], 1); assert_int_equal(owned.api_users, 0);
}
static void active_submit_error(void **state) {
    (void)state; start_submit = true; results[HOST_SUBMIT] = (host_result){HOST_RESULT_ERROR, false};
    assert_int_equal(camctl_host_submit("/active.json"), -1); assert_int_equal(errno, EIO);
    assert_int_equal(owned.children[HOST_SUBMIT].diagnostic_state, HOST_DIAGNOSTIC_IDLE);
    assert_true(polls >= 3); assert_int_equal(owned.api_users, 0);
}
static void infrastructure_error_preserves_active(void **state) {
    (void)state; host_submission active = {0}, queued = {0};
    pthread_cond_init(&active.ready, NULL); pthread_cond_init(&queued.ready, NULL);
    host_inputs_push(&owned.inputs, "/active.json", 1, &active);
    host_inputs_push(&owned.inputs, "/queued.json", 2, &queued);
    owned.children[HOST_SUBMIT].pid = owned.children[HOST_SUBMIT].pgid = 102;
    owned.schedule.jobs[HOST_SUBMIT].phase = HOST_ACTIVE; owned.runtime_error = EIO;
    fail_unstarted_submissions(&owned, EIO);
    assert_false(active.done); assert_true(queued.done); assert_int_equal(queued.error, EIO);
    poll_error = true; worker_main(&owned); module_thread = false;
    assert_true(active.done); assert_int_equal(active.error, 0);
    assert_true(collects >= 2); assert_int_equal(spawns, 0);
    pthread_cond_destroy(&active.ready); pthread_cond_destroy(&queued.ready);
}
static void clock_failure_preserves_active(void **state) {
    (void)state; host_submission active = {0}; pthread_cond_init(&active.ready, NULL);
    host_inputs_push(&owned.inputs, "/active.json", 1, &active);
    owned.children[HOST_SUBMIT].pid = owned.children[HOST_SUBMIT].pgid = 102;
    owned.schedule.jobs[HOST_SUBMIT].phase = HOST_ACTIVE;
    clock_error = poll_error = true; worker_main(&owned); module_thread = false;
    assert_true(owned.clock_failed); assert_true(active.done); assert_int_equal(active.error, 0);
    assert_int_equal(spawns, 0); pthread_cond_destroy(&active.ready);
}
static void waits_borrower(void **state) {
    (void)state; assert_ptr_equal(borrow_instance(), &owned); borrower_done = true;
    assert_int_equal(camctl_host_stop(), 0); assert_int_equal(waits, 1);
    assert_int_equal(owned.api_users, 0);
}
static void claim_reference(void **state) { (void)state; camctl_host_claim(); assert_int_equal(owned.api_users, 0); }
static void concurrent_stop(void **state) {
    (void)state; owned.lifecycle = HOST_STOPPING; owned.stop_joining = other_stop_done = true;
    assert_int_equal(camctl_host_stop(), 0); assert_int_equal(waits, 1); assert_int_equal(joins, 0);
}
static void self_stop(void **state) {
    (void)state; owned.lifecycle = HOST_STOPPING; owned.stop_joining = module_thread = true;
    assert_int_equal(camctl_host_stop(), -1); assert_int_equal(errno, EDEADLK); assert_int_equal(waits, 0);
}
static void retry_join_failure(void **state) {
    (void)state; join_error = ESRCH; assert_int_equal(camctl_host_stop(), -1);
    assert_int_equal(errno, ESRCH); assert_int_equal(owned.lifecycle, HOST_STOPPING);
    assert_non_null(owned.inputs.items); assert_int_equal(log_closes, 0);
    assert_int_equal(camctl_host_stop(), 0);
}
static void callback_drain_reentry(void **state) {
    (void)state; owned.motor = motor;
    pthread_mutex_init(&owned.callback_mutex, NULL); pthread_cond_init(&owned.callback_ready, NULL);
    owned.callbacks.capacity = 2; owned.callbacks.items = calloc(2, sizeof(host_notification));
    host_callback_queue_push(&owned.callbacks, (host_notification){.position = 4});
    host_callback_queue_push(&owned.callbacks, (host_notification){.position = 5});
    callback_work = true; assert_int_equal(camctl_host_stop(), 0);
    assert_int_equal(callbacks, 2); assert_int_equal(positions[0], 4); assert_int_equal(positions[1], 5);
    assert_int_equal(joins, 2);
}
static void notification_residue_and_active_callback_delay_stop(void **state) {
    (void)state; owned.motor = motor;
    pthread_mutex_init(&owned.callback_mutex, NULL); pthread_cond_init(&owned.callback_ready, NULL);
    owned.callbacks.capacity = 1; owned.callbacks.items = calloc(1, sizeof(host_notification));
    assert_int_equal(host_notification_init(&owned.notification, 512), 0);
    owned.notification_bytes = malloc(HOST_NOTIFICATION_READ_BLOCK);
    const char *bytes = "{\"type\":\"motor_control\",\"action_instance_id\":\"1\",\"params\":{\"position\":4}}\n"
                        "{\"type\":\"motor_control\",\"action_instance_id\":\"2\",\"params\":{\"position\":5}}\n";
    memcpy(owned.notification_bytes, bytes, strlen(bytes)); owned.notification_length = strlen(bytes);
    host_child *child = &owned.children[HOST_RUN]; child->pid = 101; child->notification_fd = 20;
    notification_eof = true;
    host_callback_queue_push(&owned.callbacks, (host_notification){3});
    collect_notifications(&owned, child); assert_int_equal(owned.notification_offset, 0);
    host_notification value; host_callback_queue_pop(&owned.callbacks, &value); assert_int_equal(value.position, 3);
    collect_notifications(&owned, child);
    assert_true(owned.notification_offset < owned.notification_length); assert_int_equal(child->notification_fd, 20);
    host_callback_queue_pop(&owned.callbacks, &value); assert_int_equal(value.position, 4);
    collect_notifications(&owned, child); assert_int_equal(child->notification_fd, -1);
    child->pid = 0; assert_false(stop_drained(&owned));
    host_callback_queue_pop(&owned.callbacks, &value); assert_int_equal(value.position, 5);
    owned.callback_active = true; assert_false(stop_drained(&owned));
    owned.callback_active = false; assert_true(stop_drained(&owned));
}
static void diagnostics_delay_close(void **state) {
    (void)state;
    const int phases[] = {HOST_DIAGNOSTIC_PENDING, HOST_DIAGNOSTIC_QUEUED, HOST_DIAGNOSTIC_WRITING};
    for (size_t i = 0; i < sizeof(phases) / sizeof(phases[0]); i++) {
        owned.children[0].diagnostic_state = phases[i]; assert_false(stop_drained(&owned));
    }
    owned.children[0].diagnostic_state = HOST_DIAGNOSTIC_PENDING;
    assert_int_equal(camctl_host_stop(), 0); assert_true(polls >= 2); assert_int_equal(diag_closes, 1);
}
#define CASE(name) cmocka_unit_test_setup_teardown(name, setup, teardown)
int main(void) {
    const struct CMUnitTest tests[] = {
        CASE(uninitialized), CASE(terminal_idempotent), CASE(stops_both_no_restart),
        CASE(stopped_launch), CASE(queued_submit), CASE(borrowed_submit_rechecks_stop_before_enqueue),
        CASE(active_submit_success), CASE(active_submit_error),
        CASE(infrastructure_error_preserves_active), CASE(clock_failure_preserves_active), CASE(waits_borrower), CASE(claim_reference),
        CASE(concurrent_stop), CASE(self_stop), CASE(retry_join_failure), CASE(callback_drain_reentry),
        CASE(notification_residue_and_active_callback_delay_stop), CASE(diagnostics_delay_close),
    };
    return cmocka_run_group_tests(tests, NULL, NULL);
}
