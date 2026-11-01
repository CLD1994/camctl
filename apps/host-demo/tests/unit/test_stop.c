#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <stdio.h>
static int test_host_fprintf(FILE *stream, const char *format, ...);
static int test_host_fflush(FILE *stream);
#define fprintf test_host_fprintf
#define fflush test_host_fflush
/* 执行公开入口和生产 worker/callback；所有进程、等待和文件接口均为替身。 */
#include "../../src/host.c"
#undef fprintf
#undef fflush

static host owned;
static int spawns, requests[2], collects, polls, joins, diag_closes, log_closes;
static int callbacks, positions[4], waits, join_error;
static int maintenance_wait_mode, maintenance_wait_step;
static bool maintenance_stub_enabled;
static int maintenance_calls;
static bool restart_test, fail_worker_create;
static int restart_creates, restart_join_error, restart_join_count, restart_close_error_fd;
static int restart_closed_fd_count, construction_messages;
static camctl_update_eligibility restart_eligibility;
static int restart_check_error;
static int test_host_fprintf(FILE *stream, const char *format, ...) {
    (void)stream; (void)format; construction_messages++; return 0;
}
static int test_host_fflush(FILE *stream) { (void)stream; return 0; }
int __wrap_host_check_reaping_contract(void) { return 0; }
int __wrap_host_pipe(int fds[2]) {
    if (!restart_test) { fail_msg("%s", "不得创建真实管道"); return -1; }
    fds[0] = 30; fds[1] = 31; return 0;
}
size_t __wrap_host_pipe_take_close_errors(host_pipe_close_error *errors, size_t capacity) {
    (void)errors; (void)capacity; return 0;
}
int __wrap_host_child_prepare_notifications(host_child *child) {
    if (!restart_test) { fail_msg("%s", "不得创建真实通知管道"); return EINVAL; }
    child->notification_fd = 20; child->notification_write_fd = 21; return 0;
}
int __wrap_fcntl(int fd, int operation, ...) { (void)fd; (void)operation; return 0; }
static bool poll_error, callback_work, start_submit, borrower_done, other_stop_done;
static bool notification_eof, clock_error, stop_before_admission;
static host_result results[2];
int __wrap_host_maintenance_run(const camctl_host_update_config *config,
                                const camctl_host_update_request *request, camctl_host_update_result *result) {
    (void)config; (void)request;
    if (!maintenance_stub_enabled) {
        fail_msg("%s", "不得启动真实维护");
        return EINVAL;
    }
    if (restart_test) {
        assert_true(atomic_load(&restart_active));
        assert_int_equal(request->action, CAMCTL_UPDATE_ACTION_CHECK);
        memset(result, 0, sizeof(*result));
        result->eligibility = restart_eligibility;
        maintenance_calls++;
        return restart_check_error;
    }
    assert_true(maintenance_active); assert_true(maintenance_owner);
    if (instance) {
        assert_int_equal(instance->lifecycle, HOST_STOPPED);
        assert_int_equal(instance->api_users, 0);
    }
    maintenance_calls++;
    result->outcome = CAMCTL_UPDATE_OUTCOME_APPLIED;
    result->effect = CAMCTL_UPDATE_EFFECT_COMMITTED;
    result->eligibility = CAMCTL_UPDATE_ELIGIBILITY_ALLOWED;
    return 0;
}
int __wrap_host_maintenance_spawn(host_child *child, const camctl_host_update_config *config,
                                  const camctl_host_update_request *request) {
    (void)child; (void)config; (void)request; fail_msg("%s", "不得启动真实进程"); return EINVAL;
}
int __wrap_pthread_create(pthread_t *thread, const pthread_attr_t *attr,
                          void *(*start)(void *), void *argument) {
    (void)attr; (void)start; (void)argument;
    if (!restart_test) { fail_msg("%s", "不得创建线程"); return EINVAL; }
    restart_creates++;
    if (fail_worker_create && restart_creates == 4)
        return EAGAIN;
    *thread = (pthread_t)(size_t)restart_creates;
    return 0;
}
int __wrap_pipe2(int fds[2], int flags) {
    (void)fds; (void)flags; fail_msg("%s", "不得创建管道"); return -1;
}
void __wrap_host_log(host_logger *l, const char *f, ...) { (void)l; (void)f; }
ssize_t __wrap_write(int fd, const void *p, size_t n) {
    (void)p; assert_int_equal(fd, owned.wake[1]); return (ssize_t)n;
}
ssize_t __wrap_read(int fd, void *p, size_t n) {
    if (notification_eof && fd == 20) return 0;
    (void)fd; (void)p; (void)n; errno = EAGAIN; return -1;
}
int __wrap_close(int fd) {
    assert_true(fd >= 0);
    if (restart_test && fd == 30) {
        restart_closed_fd_count++;
        if (restart_close_error_fd) { restart_close_error_fd = 0; errno = EIO; return -1; }
    }
    return 0;
}
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
    if (restart_test) {
        (void)thread; restart_join_count++;
        if (restart_join_error) { int error = restart_join_error; restart_join_error = 0; return error; }
        return 0;
    }
    assert_int_equal(pthread_mutex_trylock(&instance_mutex), 0);
    pthread_mutex_unlock(&instance_mutex);
    assert_int_equal(pthread_mutex_trylock(&owned.inputs_mutex), 0);
    pthread_mutex_unlock(&owned.inputs_mutex);
    joins++;
    if (join_error) { int e = join_error; join_error = 0; return e; }
    if (thread == owned.worker) {
        worker_main(&owned); module_thread = false;
        if (maintenance_wait_mode == 3)
            maintenance_active = true;
    }
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
        if (maintenance_wait_mode) {
            maintenance_wait_step++;
            if (maintenance_wait_mode == 2 && maintenance_wait_step == 1) {
                owned.lifecycle = HOST_STOPPED;
                owned.stop_joining = false;
                maintenance_active = true;
            } else {
                assert_true(maintenance_active);
                maintenance_active = false;
            }
        } else if (borrower_done) { borrower_done = false; release_instance(&owned); }
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
    maintenance_active = maintenance_owner = false;
    maintenance_wait_mode = maintenance_wait_step = maintenance_calls = 0;
    maintenance_stub_enabled = false;
    restart_test = fail_worker_create = false;
    restart_creates = restart_join_error = restart_join_count = restart_close_error_fd = 0;
    restart_closed_fd_count = construction_messages = 0;
    restart_eligibility = CAMCTL_UPDATE_ELIGIBILITY_ALLOWED; restart_check_error = 0;
    atomic_store(&stop_participants, 0); atomic_store(&restart_active, false);
    construction_pending = NULL;
    spawns = collects = polls = joins = diag_closes = log_closes = callbacks = waits = join_error = 0;
    requests[0] = requests[1] = 0;
    poll_error = callback_work = start_submit = borrower_done = other_stop_done = false;
    notification_eof = clock_error = stop_before_admission = false;
    return 0;
}
static int teardown(void **state) {
    (void)state; if (owned.inputs.items) release_storage(&owned);
    instance = NULL; module_thread = false; maintenance_active = maintenance_owner = false; return 0;
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
static void maintenance_before_stop(void **state) {
    (void)state; owned.lifecycle = HOST_STOPPED; maintenance_active = true; maintenance_wait_mode = 1;
    assert_int_equal(camctl_host_stop(), 0);
    assert_int_equal(maintenance_wait_step, 1); assert_false(maintenance_active);
}
static void maintenance_while_waiting_for_other_stop(void **state) {
    (void)state; owned.lifecycle = HOST_STOPPING; owned.stop_joining = true; maintenance_wait_mode = 2;
    assert_int_equal(camctl_host_stop(), 0);
    assert_int_equal(maintenance_wait_step, 2); assert_false(maintenance_active);
}
static void maintenance_accepted_during_stop(void **state) {
    (void)state; maintenance_wait_mode = 3;
    assert_int_equal(camctl_host_stop(), 0);
    assert_int_equal(maintenance_wait_step, 1); assert_false(maintenance_active);
}
static void callback_cannot_wait_for_maintenance(void **state) {
    (void)state; module_thread = maintenance_active = true;
    assert_int_equal(camctl_host_stop(), -1); assert_int_equal(errno, EDEADLK); assert_int_equal(waits, 0);
}
static void update_uninitialized(void **state) {
    (void)state; instance = NULL; maintenance_stub_enabled = true;
    camctl_host_update_config config = {"/camctl", "/updater", NULL};
    camctl_host_update_request request = {CAMCTL_UPDATE_ACTION_APPLY, "/package.zip", NULL};
    camctl_host_update_result result;
    assert_int_equal(camctl_host_update(&config, &request, &result), 0);
    assert_int_equal(maintenance_calls, 1); assert_false(maintenance_active);
    assert_int_equal(result.effect, CAMCTL_UPDATE_EFFECT_COMMITTED);
}
static void update_stops_and_drains_borrower(void **state) {
    (void)state; maintenance_stub_enabled = true;
    owned.config.camctl_path = "/camctl"; owned.config.updater_path = "/updater";
    owned.config.config_path = "/config"; owned.api_users = 1; borrower_done = true;
    camctl_host_update_config config = {"/camctl", "/updater", "/config"};
    camctl_host_update_request request = {CAMCTL_UPDATE_ACTION_APPLY, "/package.zip", NULL};
    camctl_host_update_result result;
    assert_int_equal(camctl_host_update(&config, &request, &result), 0);
    assert_int_equal(maintenance_calls, 1); assert_int_equal(owned.lifecycle, HOST_STOPPED);
    assert_false(maintenance_active); assert_false(maintenance_owner);
}
static void update_gates_and_checks_active_configuration(void **state) {
    (void)state; camctl_host_update_config config = {"/camctl", "/updater", NULL};
    camctl_host_update_request request = {CAMCTL_UPDATE_ACTION_RECOVER, NULL, NULL};
    camctl_host_update_result result;
    maintenance_active = true;
    assert_int_equal(camctl_host_update(&config, &request, &result), -1); assert_int_equal(errno, EBUSY);
    assert_int_equal(camctl_host_init(NULL, NULL), -1); assert_int_equal(errno, EBUSY);
    maintenance_active = false; module_thread = true;
    assert_int_equal(camctl_host_update(&config, &request, &result), -1); assert_int_equal(errno, EDEADLK);
    module_thread = false; owned.config.camctl_path = "/other"; owned.config.updater_path = "/updater";
    assert_int_equal(camctl_host_update(&config, &request, &result), -1); assert_int_equal(errno, EINVAL);
    assert_int_equal(maintenance_calls, 0); assert_int_equal(owned.lifecycle, HOST_RUNNING);
}
static host *stopped_heap_instance(void) {
    host *previous = calloc(1, sizeof(*previous));
    assert_non_null(previous);
    previous->config = (camctl_host_config)CAMCTL_HOST_CONFIG_INIT;
    previous->config.camctl_path = "/camctl"; previous->config.updater_path = "/updater";
    previous->config.config_path = "/config"; previous->config.ready_path = "/ready";
    previous->config.processing_path = "/processing"; previous->config.log_path = "/log";
    previous->lifecycle = HOST_STOPPED;
    instance = previous;
    registrations.motor = motor;
    restart_test = maintenance_stub_enabled = true;
    return previous;
}
static void dispose_fake_fresh_instance(void) {
    host *fresh = instance;
    assert_non_null(fresh);
    host_diagnostic_destroy(&fresh->diagnostics);
    host_logger_destroy(&fresh->logger);
    release_storage(fresh);
    free(fresh);
    instance = &owned;
    registrations.motor = NULL;
}
static void restart_state_gates(void **state) {
    (void)state;
    instance = NULL; assert_int_equal(camctl_host_restart(), -1); assert_int_equal(errno, ENODEV);
    maintenance_active = true;
    assert_int_equal(camctl_host_restart(), -1); assert_int_equal(errno, EBUSY);
    maintenance_active = false; construction_pending = &owned;
    assert_int_equal(camctl_host_restart(), -1); assert_int_equal(errno, EBUSY);
    construction_pending = NULL;
    instance = &owned; assert_int_equal(camctl_host_restart(), -1); assert_int_equal(errno, EALREADY);
    owned.lifecycle = HOST_STOPPING;
    assert_int_equal(camctl_host_restart(), -1); assert_int_equal(errno, EBUSY);
    owned.lifecycle = HOST_STOPPED; maintenance_active = true;
    assert_int_equal(camctl_host_restart(), -1); assert_int_equal(errno, EBUSY);
    maintenance_active = false; atomic_store(&stop_participants, 1);
    assert_int_equal(camctl_host_restart(), -1); assert_int_equal(errno, EBUSY);
    atomic_store(&stop_participants, 0); atomic_store(&restart_active, true);
    assert_int_equal(camctl_host_restart(), -1); assert_int_equal(errno, EBUSY);
    atomic_store(&restart_active, false); module_thread = true;
    assert_int_equal(camctl_host_restart(), -1); assert_int_equal(errno, EDEADLK);
    module_thread = false;
}
static void restart_reuses_config_and_callback_without_first_plan(void **state) {
    (void)state; host *previous = stopped_heap_instance();
    assert_int_equal(camctl_host_restart(), 0);
    assert_ptr_not_equal(instance, previous); assert_int_equal(instance->lifecycle, HOST_RUNNING);
    assert_string_equal(instance->config.camctl_path, "/camctl");
    assert_string_equal(instance->config.config_path, "/config");
    assert_ptr_equal(instance->motor, motor); assert_false(instance->schedule.first_plan);
    assert_int_equal(instance->schedule.retries_used, 0); assert_int_equal(maintenance_calls, 1);
    assert_int_equal(camctl_host_init(NULL, NULL), -1); assert_int_equal(errno, EALREADY);
    dispose_fake_fresh_instance();
}
static void restart_join_failure_keeps_candidate_until_stop_finishes(void **state) {
    (void)state; host *previous = stopped_heap_instance();
    fail_worker_create = true; restart_join_error = ESRCH;
    assert_int_equal(camctl_host_restart(), -1); assert_int_equal(errno, EAGAIN);
    assert_ptr_equal(instance, previous); assert_non_null(construction_pending);
    assert_int_equal(restart_join_count, 2); /* 回调未确认结束时保留其日志依赖。 */
    assert_int_equal(camctl_host_restart(), -1); assert_int_equal(errno, EBUSY);
    camctl_host_update_config config = {"/camctl", "/updater", "/config"};
    camctl_host_update_request request = {CAMCTL_UPDATE_ACTION_RECOVER, NULL, NULL};
    camctl_host_update_result result;
    assert_int_equal(camctl_host_update(&config, &request, &result), -1); assert_int_equal(errno, EBUSY);
    restart_close_error_fd = 1;
    assert_int_equal(camctl_host_stop(), 0); assert_null(construction_pending);
    assert_int_equal(restart_join_count, 4); assert_int_equal(restart_closed_fd_count, 1);
    assert_true(construction_messages >= 3); /* 原构造、join 和 close 错误均交付。 */
    fail_worker_create = false; restart_creates = 0;
    assert_int_equal(camctl_host_restart(), 0);
    dispose_fake_fresh_instance();
}
static void restart_check_failure_preserves_stopped_instance(void **state) {
    (void)state; host *previous = stopped_heap_instance();
    restart_eligibility = CAMCTL_UPDATE_ELIGIBILITY_BLOCKED;
    assert_int_equal(camctl_host_restart(), -1); assert_int_equal(errno, EBUSY);
    assert_ptr_equal(instance, previous); assert_int_equal(restart_creates, 0);
    restart_eligibility = CAMCTL_UPDATE_ELIGIBILITY_UNKNOWN;
    assert_int_equal(camctl_host_restart(), -1); assert_int_equal(errno, EPROTO);
    assert_ptr_equal(instance, previous); assert_int_equal(restart_creates, 0);
    restart_check_error = EIO;
    assert_int_equal(camctl_host_restart(), -1); assert_int_equal(errno, EIO);
    assert_ptr_equal(instance, previous); assert_int_equal(restart_creates, 0);
    restart_check_error = 0; restart_eligibility = CAMCTL_UPDATE_ELIGIBILITY_ALLOWED;
    assert_int_equal(camctl_host_restart(), 0);
    dispose_fake_fresh_instance();
}
static void restart_creation_failure_can_retry_after_threads_join(void **state) {
    (void)state; host *previous = stopped_heap_instance();
    fail_worker_create = true;
    assert_int_equal(camctl_host_restart(), -1); assert_int_equal(errno, EAGAIN);
    assert_ptr_equal(instance, previous); assert_null(construction_pending);
    assert_int_equal(restart_join_count, 3);
    fail_worker_create = false; restart_creates = 0;
    assert_int_equal(camctl_host_restart(), 0);
    dispose_fake_fresh_instance();
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
#ifdef HOST_RESTART_ONLY
        CASE(restart_state_gates), CASE(restart_reuses_config_and_callback_without_first_plan),
        CASE(restart_join_failure_keeps_candidate_until_stop_finishes),
        CASE(restart_check_failure_preserves_stopped_instance), CASE(restart_creation_failure_can_retry_after_threads_join),
#elif defined(HOST_UPDATE_STOP_ONLY)
        CASE(maintenance_before_stop), CASE(maintenance_while_waiting_for_other_stop),
        CASE(maintenance_accepted_during_stop), CASE(callback_cannot_wait_for_maintenance),
        CASE(update_uninitialized), CASE(update_stops_and_drains_borrower),
        CASE(update_gates_and_checks_active_configuration),
#else
        CASE(uninitialized), CASE(terminal_idempotent), CASE(stops_both_no_restart),
        CASE(stopped_launch), CASE(queued_submit), CASE(borrowed_submit_rechecks_stop_before_enqueue),
        CASE(active_submit_success), CASE(active_submit_error),
        CASE(infrastructure_error_preserves_active), CASE(clock_failure_preserves_active), CASE(waits_borrower), CASE(claim_reference),
        CASE(concurrent_stop), CASE(maintenance_before_stop), CASE(maintenance_while_waiting_for_other_stop),
        CASE(maintenance_accepted_during_stop), CASE(callback_cannot_wait_for_maintenance),
        CASE(update_uninitialized), CASE(update_stops_and_drains_borrower),
        CASE(update_gates_and_checks_active_configuration),
        CASE(restart_state_gates), CASE(restart_reuses_config_and_callback_without_first_plan),
        CASE(restart_join_failure_keeps_candidate_until_stop_finishes),
        CASE(restart_check_failure_preserves_stopped_instance), CASE(restart_creation_failure_can_retry_after_threads_join),
        CASE(self_stop), CASE(retry_join_failure), CASE(callback_drain_reentry),
        CASE(notification_residue_and_active_callback_delay_stop), CASE(diagnostics_delay_close),
#endif
    };
    return cmocka_run_group_tests(tests, NULL, NULL);
}
