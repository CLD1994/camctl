#include "camctl_host.h"
#include "claim.h"
#include "callbacks.h"
#include "callback_queue.h"
#include "notification.h"
#include "config.h"
#include "diagnostic.h"
#include "input.h"
#include "maintenance.h"
#include "host_version.h"
#include "process.h"
#include "scheduler.h"
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <stdio.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

typedef struct host_submission {
    pthread_cond_t ready;
    bool done;
    int error;
} host_submission;

typedef enum { HOST_RUNNING, HOST_STOPPING, HOST_STOPPED } host_lifecycle;

typedef struct {
    camctl_host_config config;
    char paths[7][CAMCTL_HOST_PATH_MAX + 1];
    host_schedule schedule;
    host_inputs inputs;
    host_child children[2];
    host_logger logger;
    host_diagnostic_writer diagnostics;
    pthread_mutex_t inputs_mutex, claim_mutex;
    pthread_t worker;
    host_lifecycle lifecycle; /* instance_mutex 同时保护借用、受理和启动。 */
    size_t api_users;
    bool stop_joining;
    bool worker_joined, callback_joined;
    int wake[2];
    uint64_t next_id, next_attempt_id;
    bool clock_failed;
    host_notification_stream notification;
    host_callback_queue callbacks;
    pthread_mutex_t callback_mutex;
    pthread_cond_t callback_ready;
    pthread_t callback_thread;
    camctl_host_motor_control_callback motor;
    bool callback_stop, callback_active;
    char *notification_bytes;
    size_t notification_offset, notification_length;
    int runtime_error; /* inputs_mutex 保护；无法继续启动时拒绝新路径。 */
    int submit_start_error; /* 工作线程保存当前提交最后一次明确的启动错误。 */
    unsigned construction_resources;
    int construction_error;
    int close_errors[32], close_fds[32];
    const char *cleanup_operations[32];
    size_t close_error_count;
} host;
static pthread_mutex_t instance_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t instance_ready = PTHREAD_COND_INITIALIZER;
static host *instance;
static bool maintenance_active;
static _Thread_local bool maintenance_owner;
static _Atomic size_t stop_participants;
static _Atomic bool restart_active;
static host *construction_pending;
static host_callbacks registrations;
/* 身份在线程局部析构期间仍保留，且不比较已经 join 的线程标识。 */
static _Thread_local bool module_thread;
static const char *command_name(host_command c) { return c == HOST_RUN ? "run" : "submit"; }
static int failure(int e) {
    errno = e;
    return -1;
}
static host *borrow_instance(void) {
    pthread_mutex_lock(&instance_mutex);
    host *h = instance;
    int error = !h ? ENODEV : h->lifecycle != HOST_RUNNING ? ECANCELED : 0;
    if (!error)
        h->api_users++;
    pthread_mutex_unlock(&instance_mutex);
    if (error) {
        errno = error;
        return NULL;
    }
    return h;
}
static void release_instance(host *h) {
    pthread_mutex_lock(&instance_mutex);
    h->api_users--;
    pthread_cond_broadcast(&instance_ready);
    pthread_mutex_unlock(&instance_mutex);
}
static bool stopping(host *h) {
    pthread_mutex_lock(&instance_mutex);
    bool result = h->lifecycle != HOST_RUNNING;
    pthread_mutex_unlock(&instance_mutex);
    return result;
}
int camctl_host_register_motor_control_callback(camctl_host_motor_control_callback callback) {
    pthread_mutex_lock(&instance_mutex);
    int e = host_callbacks_register(&registrations, callback);
    pthread_mutex_unlock(&instance_mutex);
    return e ? failure(e) : 0;
}
static void wake_worker(host *h) {
    ssize_t n = write(h->wake[1], "x", 1);
    if (n < 0 && errno != EAGAIN && errno != EINTR)
        host_log(&h->logger, "worker_wake_failed errno=%d", errno);
}
static void *callback_main(void *p) {
    module_thread = true;
    host *h = p;
    pthread_mutex_lock(&h->callback_mutex);
    for (;;) {
        while (!h->callback_stop && !h->callbacks.length)
            pthread_cond_wait(&h->callback_ready, &h->callback_mutex);
        if (h->callback_stop)
            break;
        host_notification v;
        host_callback_queue_pop(&h->callbacks, &v);
        h->callback_active = true;
        pthread_mutex_unlock(&h->callback_mutex);
        wake_worker(h);
        h->motor(v.position);
        pthread_mutex_lock(&h->callback_mutex);
        h->callback_active = false;
        wake_worker(h);
    }
    pthread_mutex_unlock(&h->callback_mutex);
    return NULL;
}

static bool notification_room(host *h) {
    pthread_mutex_lock(&h->callback_mutex);
    bool room = h->callbacks.length < h->callbacks.capacity;
    pthread_mutex_unlock(&h->callback_mutex);
    return room;
}
static bool notification_sink(void *p, host_notification value) {
    host *h = p;
    pthread_mutex_lock(&h->callback_mutex);
    bool accepted = host_callback_queue_push(&h->callbacks, value);
    if (accepted)
        pthread_cond_signal(&h->callback_ready);
    pthread_mutex_unlock(&h->callback_mutex);
    return accepted;
}
static void notification_diagnostic(void *p, host_notification_error error) {
    host *h = p;
    host_child *c = &h->children[HOST_RUN];
    host_log(&h->logger, "call=%" PRIu64 " command=run pid=%ld notification_rejected reason=%s",
             c->call_id, (long)c->pid, host_notification_error_name(error));
}
static void collect_notifications(host *h, host_child *c) {
    if (c->notification_fd < 0)
        return;
    for (size_t quota = 0; quota < 64 * 1024;) {
        if (!notification_room(h))
            return;
        if (h->notification_offset < h->notification_length) {
            size_t used = host_notification_feed(&h->notification,
                                                 h->notification_bytes + h->notification_offset,
                                                 h->notification_length - h->notification_offset,
                                                 notification_sink, notification_diagnostic, h);
            h->notification_offset += used;
            if (h->notification_offset < h->notification_length)
                return;
        }
        h->notification_offset = h->notification_length = 0;
        ssize_t n = read(c->notification_fd, h->notification_bytes, HOST_NOTIFICATION_READ_BLOCK);
        if (n > 0) {
            h->notification_length = (size_t)n;
            quota += (size_t)n;
            continue;
        }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
            return;
        if (n < 0)
            host_log(&h->logger, "call=%" PRIu64 " notification_read_failed errno=%d", c->call_id,
                     errno);
        host_notification_eof(&h->notification, notification_diagnostic, h);
        close(c->notification_fd);
        c->notification_fd = -1;
        return;
    }
}
static uint64_t now_ms(host *h) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) {
        int e = errno;
        pthread_mutex_lock(&h->inputs_mutex);
        h->runtime_error = e;
        pthread_mutex_unlock(&h->inputs_mutex);
        if (!h->clock_failed)
            host_log(&h->logger, "clock_unknown errno=%d", e);
        h->clock_failed = true;
        return 0;
    }
    return (uint64_t)ts.tv_sec * 1000 + (uint64_t)ts.tv_nsec / 1000000;
}
typedef struct {
    host *h;
    host_child *child;
    host_command command;
} child_log_context;
static void child_log(void *p, const char *stream, const char *data, size_t n) {
    child_log_context *c = p;
    host_log(&c->h->logger, "call=%" PRIu64 " command=%s pid=%ld stream=%s data=%.*s", c->child->call_id,
             command_name(c->command), (long)c->child->pid, stream, (int)n, data);
}
/* inputs_mutex 保护队列和调用线程持有的完成记录。 */
static void finish_submit_locked(host *h, int error) {
    host_input *input = host_inputs_front(&h->inputs);
    if (!input)
        return;
    host_submission *submission = input->submission;
    host_inputs_pop(&h->inputs);
    submission->error = error;
    submission->done = true;
    pthread_cond_signal(&submission->ready);
}
static void finish_submit(host *h, int error) {
    pthread_mutex_lock(&h->inputs_mutex);
    finish_submit_locked(h, error);
    pthread_mutex_unlock(&h->inputs_mutex);
}
static void fail_unstarted_submissions(host *h, int error) {
    pthread_mutex_lock(&h->inputs_mutex);
    host_child *c = &h->children[HOST_SUBMIT];
    bool active = c->pid || c->submit_completion_pending;
    size_t keep = active && h->inputs.count ? 1 : 0;
    for (size_t i = keep; i < h->inputs.count; i++) {
        host_input *input = &h->inputs.items[(h->inputs.head + i) % h->inputs.capacity];
        input->submission->error = error;
        input->submission->done = true;
        pthread_cond_signal(&input->submission->ready);
    }
    h->inputs.count = keep;
    pthread_mutex_unlock(&h->inputs_mutex);
}
static void collect(host *h, host_command command) {
    host_child *c = &h->children[command];
    if (!c->pid)
        return;
    child_log_context ctx = {h, c, command};
    host_child_collect(c, child_log, &ctx);
    if (command == HOST_RUN && h->motor)
        collect_notifications(h, c);
    if (!host_child_done(c))
        return;
    host_result r = c->overflow || c->io_error ? (host_result){HOST_RESULT_ABNORMAL, false}
                                               : host_result_parse(command, c->output, c->length,
                                                                   c->termination, c->exit_code);
    c->result_kind = r.kind;
    c->result_needs_run = r.needs_run;
    c->completed_pid = c->pid;
    c->completed_pgid = c->pgid;
    const char *names[] = {"abnormal", "success", "error"};
    host_log(
        &h->logger,
        "call=%" PRIu64
        " command=%s pid=%ld result=%s exit_kind=%d exit=%d needs_run=%d overflow=%d io_error=%d",
        c->call_id, command_name(command), (long)c->pid, names[r.kind], c->termination,
        c->exit_code, r.needs_run, c->overflow, c->io_error);
    if (r.kind != HOST_RESULT_SUCCESS)
        host_log(&h->logger, "call=%" PRIu64 " stdout=%.*s", c->call_id, (int)c->length,
                 c->output);
    host_schedule_finished(&h->schedule, command, r, now_ms(h));
    c->pid = 0;
    if (r.kind != HOST_RESULT_SUCCESS) {
        if (command == HOST_SUBMIT) {
            c->submit_completion_pending = true;
            c->submit_completion_error = r.kind == HOST_RESULT_ERROR ? EIO : EPROTO;
        }
        host_diagnostic_request(&h->diagnostics, c);
    } else if (command == HOST_SUBMIT) {
        finish_submit(h, 0);
    }
}
static void complete_diagnostic_submits(host *h) {
    host_child *c = &h->children[HOST_SUBMIT];
    if (!c->submit_completion_pending || host_diagnostic_busy(&h->diagnostics, c))
        return;
    int error = c->submit_completion_error;
    c->submit_completion_pending = false;
    finish_submit(h, error);
}
static void launch_locked(host *h, host_command command, uint64_t now) {
    if (h->clock_failed)
        return;
    host_child *c = &h->children[command];
    if (host_diagnostic_busy(&h->diagnostics, c))
        return;
    host_schedule *s = &h->schedule;
    const char *path = NULL;
    uint64_t id = 0;
    if (command == HOST_SUBMIT) {
        pthread_mutex_lock(&h->inputs_mutex);
        host_input *input = host_inputs_front(&h->inputs);
        if (input) {
            path = input->path;
            id = input->id;
        }
        pthread_mutex_unlock(&h->inputs_mutex);
        if (!path)
            return;
        if (s->jobs[command].phase == HOST_IDLE)
            host_schedule_submit(s);
    }
    host_phase before = s->jobs[command].phase;
    uint32_t retries = s->retries_used;
    if (!host_schedule_take(s, command, now)) {
        if (before == HOST_WAIT && s->jobs[command].phase == HOST_IDLE) {
            host_log(&h->logger, "command=%s retry_exhausted used=%u", command_name(command),
                     s->retries_used);
            if (command == HOST_SUBMIT)
                finish_submit(h, h->submit_start_error);
        }
        return;
    }
    if (command == HOST_RUN) {
        path = s->first_plan ? h->paths[5] : NULL;
        pthread_mutex_lock(&h->inputs_mutex);
        id = h->next_id++;
        pthread_mutex_unlock(&h->inputs_mutex);
    }
    c->call_id = id;
    c->attempt_id = h->next_attempt_id++;
    c->command = command;
    c->automatic = s->retries_used != retries;
    c->retries_used = s->retries_used;
    c->plan_path[0] = 0;
    if (path)
        strcpy(c->plan_path, path);
    c->working_directory[0] = 0;
    c->working_directory_errno = 0;
    if (!getcwd(c->working_directory, sizeof(c->working_directory))) {
        c->working_directory_errno = errno;
        c->working_directory[0] = 0;
    }
    c->spawn_errno = 0;
    c->result_kind = HOST_RESULT_ABNORMAL;
    c->submit_completion_pending = false;
    c->submit_completion_error = 0;
    host_log(&h->logger,
             "call=%" PRIu64 " attempt=%" PRIu64
             " command=%s operation=spawn plan=%s automatic=%d retries=%u",
             c->call_id, c->attempt_id, command_name(command), path ? path : "(none)",
             c->automatic, c->retries_used);
    if (command == HOST_RUN && h->motor) {
        host_notification_reset(&h->notification);
        h->notification_length = h->notification_offset = 0;
    }
    int e = host_child_spawn(c, &h->config, command, path);
    if (e) {
        c->spawn_errno = e;
        if (command == HOST_SUBMIT)
            h->submit_start_error = e;
        host_log(&h->logger,
                 "call=%" PRIu64 " attempt=%" PRIu64
                 " command=%s not_started errno=%d detail=%s",
                 c->call_id, c->attempt_id, command_name(command), e, strerror(e));
        host_schedule_spawn_failed(s, command, now_ms(h));
        host_diagnostic_request(&h->diagnostics, c);
    } else {
        host_schedule_spawned(s, command);
        if (command == HOST_RUN)
            h->paths[5][0] = 0;
        host_log(&h->logger,
                 "call=%" PRIu64 " attempt=%" PRIu64
                 " command=%s pid=%ld spawned execution=unknown",
                 c->call_id, c->attempt_id, command_name(command), (long)c->pid);
    }
}
static void launch(host *h, host_command command, uint64_t now) {
    /* 停止状态转换不能插入最终资格检查与实际 spawn 之间。 */
    pthread_mutex_lock(&instance_mutex);
    if (h->lifecycle == HOST_RUNNING && !h->runtime_error)
        launch_locked(h, command, now);
    pthread_mutex_unlock(&instance_mutex);
}
static bool stop_drained(host *h) {
    for (int i = 0; i < 2; i++)
        if (h->children[i].pid || host_diagnostic_busy(&h->diagnostics, &h->children[i]))
            return false;
    pthread_mutex_lock(&h->inputs_mutex);
    bool inputs_done = !h->inputs.count;
    pthread_mutex_unlock(&h->inputs_mutex);
    if (!inputs_done)
        return false;
    if (h->motor) {
        pthread_mutex_lock(&h->callback_mutex);
        bool callbacks_done = !h->callbacks.length && !h->callback_active;
        if (callbacks_done) {
            h->callback_stop = true;
            pthread_cond_signal(&h->callback_ready);
        }
        pthread_mutex_unlock(&h->callback_mutex);
        return callbacks_done;
    }
    return true;
}
static void *worker_main(void *p) {
    module_thread = true;
    host *h = p;
    /* init发布实例后才允许子进程产生回调，重入入口可看见完整实例。 */
    pthread_mutex_lock(&instance_mutex);
    pthread_mutex_unlock(&instance_mutex);
    for (;;) {
        uint64_t now = now_ms(h);
        collect(h, HOST_RUN);
        collect(h, HOST_SUBMIT);
        host_diagnostic_dispatch_pending(&h->diagnostics, h->children);
        complete_diagnostic_submits(h);
        if (stopping(h)) {
            fail_unstarted_submissions(h, ECANCELED);
            for (int i = 0; i < 2; i++) {
                child_log_context ctx = {h, &h->children[i], (host_command)i};
                host_child_request_stop(&h->children[i], child_log, &ctx);
            }
            if (stop_drained(h)) {
                return NULL;
            }
        } else if (!h->clock_failed && !h->runtime_error) {
            launch(h, HOST_RUN, now);
            launch(h, HOST_SUBMIT, now);
        } else
            fail_unstarted_submissions(h, h->runtime_error);
        struct pollfd fds[6];
        nfds_t n = 1;
        fds[0] = (struct pollfd){h->wake[0], POLLIN, 0};
        for (int i = 0; i < 2; i++) {
            if (h->children[i].out_fd >= 0)
                fds[n++] = (struct pollfd){h->children[i].out_fd, POLLIN, 0};
            if (h->children[i].err_fd >= 0)
                fds[n++] = (struct pollfd){h->children[i].err_fd, POLLIN, 0};
        }
        if (h->motor && h->children[HOST_RUN].notification_fd >= 0 && notification_room(h))
            fds[n++] = (struct pollfd){h->children[HOST_RUN].notification_fd, POLLIN, 0};
        if (poll(fds, n, 50) < 0 && errno != EINTR) {
            int e = errno;
            pthread_mutex_lock(&h->inputs_mutex);
            h->runtime_error = e;
            pthread_mutex_unlock(&h->inputs_mutex);
            fail_unstarted_submissions(h, e);
            host_log(&h->logger, "poll_failed errno=%d", e);
            /* 事件等待失效也必须继续收取原调用；不放弃在管进程或真实结果。
             * 空 poll 沿用原轮询预算，避免故障描述符导致立即反复返回。 */
            (void)poll(NULL, 0, 50);
            continue;
        }
        if (fds[0].revents & POLLIN) {
            char bytes[256];
            ssize_t received = read(h->wake[0], bytes, sizeof(bytes));
            if (received < 0 && errno != EAGAIN && errno != EINTR)
                host_log(&h->logger, "notification_read_failed errno=%d", errno);
        }
    }
}
enum {
    BUILD_INPUT_MUTEX = 1u << 0, BUILD_CLAIM_MUTEX = 1u << 1, BUILD_WAKE = 1u << 2,
    BUILD_LOGGER = 1u << 3, BUILD_DIAGNOSTIC = 1u << 4, BUILD_CALLBACK_MUTEX = 1u << 5,
    BUILD_CALLBACK_COND = 1u << 6, BUILD_CALLBACK_THREAD = 1u << 7
};
typedef struct {
    int original_error, cleanup_error;
    int close_errors[32], close_fds[32];
    const char *cleanup_operations[32];
    size_t close_error_count;
} construction_report;

static void candidate_fact(host *h, const char *operation, int fd, int error) {
    if (!error || h->close_error_count >= 32)
        return;
    h->cleanup_operations[h->close_error_count] = operation;
    h->close_fds[h->close_error_count] = fd;
    h->close_errors[h->close_error_count++] = error;
}
static void candidate_close(host *h, int *descriptor) {
    if (*descriptor < 0)
        return;
    int fd = *descriptor;
    *descriptor = -1; /* Linux close 不得对可能已复用的编号重试。 */
    if (close(fd))
        candidate_fact(h, "close", fd, errno);
}
static void candidate_pipe_errors(host *h) {
    host_pipe_close_error errors[4];
    size_t count = host_pipe_take_close_errors(errors, 4);
    for (size_t i = 0; i < count && h->close_error_count < 32; ++i) {
        candidate_fact(h, "close", errors[i].fd, errors[i].error);
    }
}
static int cleanup_candidate(host *h) {
    int error, first_error = 0;
    bool diagnostic_joined = true;
    /* 尽量先结束独立线程；诊断线程未收场时保留其日志依赖。 */
    if (h->construction_resources & BUILD_CALLBACK_THREAD) {
        pthread_mutex_lock(&h->callback_mutex);
        h->callback_stop = true;
        pthread_cond_signal(&h->callback_ready);
        pthread_mutex_unlock(&h->callback_mutex);
        error = pthread_join(h->callback_thread, NULL);
        if (error) {
            candidate_fact(h, "callback_join", -1, error);
            first_error = error;
        } else
            h->construction_resources &= ~BUILD_CALLBACK_THREAD;
    }
    if (h->construction_resources & BUILD_DIAGNOSTIC) {
        error = host_diagnostic_join(&h->diagnostics, true);
        if (error) {
            candidate_fact(h, "diagnostic_join", -1, error);
            diagnostic_joined = false;
            if (!first_error)
                first_error = error;
        }
    }
    if ((h->construction_resources & BUILD_LOGGER) && diagnostic_joined &&
        !(h->construction_resources & BUILD_CALLBACK_THREAD)) {
        error = host_logger_join(&h->logger, true);
        if (error) {
            candidate_fact(h, "logger_join", -1, error);
            if (!first_error)
                first_error = error;
        }
    }
    if (first_error)
        return first_error;
    if (h->construction_resources & BUILD_CALLBACK_COND) {
        error = pthread_cond_destroy(&h->callback_ready);
        candidate_fact(h, "callback_cond_destroy", -1, error);
        h->construction_resources &= ~BUILD_CALLBACK_COND;
    }
    if (h->construction_resources & BUILD_CALLBACK_MUTEX) {
        error = pthread_mutex_destroy(&h->callback_mutex);
        candidate_fact(h, "callback_mutex_destroy", -1, error);
        h->construction_resources &= ~BUILD_CALLBACK_MUTEX;
    }
    if (h->construction_resources & BUILD_DIAGNOSTIC) {
        host_diagnostic_destroy(&h->diagnostics);
        candidate_fact(h, "diagnostic_cond_destroy", -1, h->diagnostics.cond_error);
        candidate_fact(h, "diagnostic_mutex_destroy", -1, h->diagnostics.mutex_error);
        h->construction_resources &= ~BUILD_DIAGNOSTIC;
    }
    if (h->construction_resources & BUILD_LOGGER) {
        host_logger_destroy(&h->logger);
        candidate_fact(h, "close", h->logger.close_error_fd, h->logger.close_error);
        candidate_fact(h, "logger_cond_destroy", -1, h->logger.cond_error);
        candidate_fact(h, "logger_mutex_destroy", -1, h->logger.mutex_error);
        h->construction_resources &= ~BUILD_LOGGER;
    }
    if (h->construction_resources & BUILD_WAKE) {
        candidate_close(h, &h->wake[0]);
        candidate_close(h, &h->wake[1]);
        h->construction_resources &= ~BUILD_WAKE;
    }
    if (h->construction_resources & BUILD_CLAIM_MUTEX) {
        error = pthread_mutex_destroy(&h->claim_mutex);
        candidate_fact(h, "claim_mutex_destroy", -1, error);
        h->construction_resources &= ~BUILD_CLAIM_MUTEX;
    }
    if (h->construction_resources & BUILD_INPUT_MUTEX) {
        error = pthread_mutex_destroy(&h->inputs_mutex);
        candidate_fact(h, "input_mutex_destroy", -1, error);
        h->construction_resources &= ~BUILD_INPUT_MUTEX;
    }
    candidate_close(h, &h->children[HOST_RUN].notification_fd);
    candidate_close(h, &h->children[HOST_RUN].notification_write_fd);
    host_notification_destroy(&h->notification);
    free(h->callbacks.items);
    free(h->notification_bytes);
    for (int i = 0; i < 2; ++i) {
        free(h->children[i].output);
        free(h->children[i].stderr_output);
        host_group_scan_reset(&h->children[i].scan);
    }
    free(h->inputs.items);
    return 0;
}

static construction_report candidate_report(host *h, int original, int cleanup) {
    construction_report report = {0};
    report.original_error = original;
    report.cleanup_error = cleanup;
    report.close_error_count = h->close_error_count;
    for (size_t i = 0; i < h->close_error_count; ++i) {
        report.close_fds[i] = h->close_fds[i];
        report.close_errors[i] = h->close_errors[i];
        report.cleanup_operations[i] = h->cleanup_operations[i];
    }
    h->close_error_count = 0;
    return report;
}
static void deliver_construction_report(const construction_report *report) {
    if (report->original_error)
        fprintf(stderr, "camctl 构造失败 errno=%d\n", report->original_error);
    if (report->cleanup_error && !report->close_error_count)
        fprintf(stderr, "camctl 候选收场未完成 errno=%d\n", report->cleanup_error);
    for (size_t i = 0; i < report->close_error_count; ++i)
        fprintf(stderr, "camctl 候选收场 operation=%s fd=%d errno=%d\n",
                report->cleanup_operations[i], report->close_fds[i], report->close_errors[i]);
    if (report->original_error || report->cleanup_error || report->close_error_count)
        (void)fflush(stderr);
}

static void deliver_maintenance_check(const camctl_host_update_result *result, int error) {
    if (!error && !result->stdout_truncated && !result->stdout_io_error && !result->stderr_length &&
        !result->stderr_truncated && !result->stderr_io_error && !result->process_diagnostics_length &&
        !result->process_diagnostics_truncated && !result->poll_error && !result->parse_error)
        return;
    if (error) {
        fprintf(stderr, "camctl 启动资格检查失败 errno=%d reason=%.*s\n", error,
                (int)(result->reason_length < CAMCTL_HOST_UPDATE_TEXT_MAX ?
                      result->reason_length : CAMCTL_HOST_UPDATE_TEXT_MAX), result->reason);
        if (result->stdout_length)
            (void)fwrite(result->stdout_data, 1, result->stdout_length, stderr);
    }
    if (result->stdout_truncated)
        fputs("camctl 维护 stdout 已截断\n", stderr);
    if (result->stdout_io_error)
        fputs("camctl 维护 stdout 读取失败\n", stderr);
    if (result->stderr_length)
        (void)fwrite(result->stderr_data, 1, result->stderr_length, stderr);
    if (result->stderr_truncated)
        fputs("camctl 维护 stderr 已截断\n", stderr);
    if (result->stderr_io_error)
        fputs("camctl 维护 stderr 读取失败\n", stderr);
    if (result->process_diagnostics_length)
        (void)fwrite(result->process_diagnostics, 1, result->process_diagnostics_length, stderr);
    if (result->process_diagnostics_truncated)
        fputs("camctl 维护进程诊断已截断\n", stderr);
    if (result->poll_error)
        fprintf(stderr, "camctl 维护进程等待错误 errno=%d\n", result->poll_error);
    if (result->parse_error)
        fprintf(stderr, "camctl 维护结果解析错误 errno=%d\n", result->parse_error);
    (void)fflush(stderr);
}

static int host_start(const camctl_host_config *config, const char *initial, host *restarting) {
    int cancel_state;
    int e = pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &cancel_state);
    if (e)
        return failure(e);
    camctl_host_update_result *check_result = NULL;
    bool check_performed = false;
    construction_report report = {0};
    pthread_mutex_lock(&instance_mutex);
    if (maintenance_active) {
        e = EBUSY;
        goto unlock;
    }
    if (restarting) {
        if (instance != restarting || restarting->lifecycle != HOST_STOPPED || construction_pending ||
            atomic_load(&stop_participants)) {
            e = EBUSY;
            goto unlock;
        }
    } else if (instance) {
        e = EALREADY;
        goto unlock;
    } else if (construction_pending) {
        e = EBUSY;
        goto unlock;
    }
    e = host_config_validate(config, initial);
    if (e)
        goto unlock;
    e = host_check_reaping_contract();
    if (e)
        goto unlock;
    if (config->updater_path) {
        camctl_host_update_config maintenance_config = {config->camctl_path, config->updater_path,
                                                        config->config_path};
        camctl_host_update_request request = {CAMCTL_UPDATE_ACTION_CHECK, NULL, NULL};
        check_result = malloc(sizeof(*check_result));
        if (!check_result) {
            e = ENOMEM;
            goto unlock;
        }
        e = host_maintenance_run(&maintenance_config, &request, check_result);
        check_performed = true;
        if (e || check_result->eligibility != CAMCTL_UPDATE_ELIGIBILITY_ALLOWED) {
            if (!e)
                e = check_result->eligibility == CAMCTL_UPDATE_ELIGIBILITY_BLOCKED ? EBUSY : EPROTO;
            goto unlock;
        }
    }
    host *h = calloc(1, sizeof(*h));
    if (!h) {
        e = ENOMEM;
        goto unlock;
    }
    h->config = *config;
    h->wake[0] = h->wake[1] = -1;
    h->motor = registrations.motor;
    const char *paths[] = {config->camctl_path, config->ready_path,  config->processing_path,
                           config->log_path,    config->config_path, initial,
                           config->updater_path};
    const char **owned[] = {&h->config.camctl_path, &h->config.ready_path,
                            &h->config.processing_path, &h->config.log_path,
                            &h->config.config_path};
    for (size_t i = 0; i < 7; i++) {
        if (paths[i])
            strcpy(h->paths[i], paths[i]);
        if (i < 5)
            *owned[i] = paths[i] ? h->paths[i] : NULL;
    }
    h->config.updater_path = paths[6] ? h->paths[6] : NULL;
    h->inputs.capacity = config->plan_capacity;
    h->next_id = 1;
    h->next_attempt_id = 1;
    h->inputs.items = calloc(config->plan_capacity, sizeof(host_input));
    size_t stderr_capacity = config->stdout_capacity < HOST_DIAGNOSTIC_STDERR_CAPTURE_MAX
                                 ? config->stdout_capacity
                                 : HOST_DIAGNOSTIC_STDERR_CAPTURE_MAX;
    for (int i = 0; i < 2; i++) {
        char *output = malloc(config->stdout_capacity);
        char *stderr_output = malloc(stderr_capacity);
        host_child_init(&h->children[i], output, config->stdout_capacity, stderr_output,
                        stderr_capacity);
    }
    if (!h->inputs.items || !h->children[0].output || !h->children[1].output ||
        !h->children[0].stderr_output || !h->children[1].stderr_output) {
        e = ENOMEM;
        goto construction_failed;
    }
    if (h->motor) {
        h->callbacks.capacity = config->notification_queue_capacity;
        h->callbacks.items = calloc(h->callbacks.capacity, sizeof(host_notification));
        if (!h->callbacks.items) {
            e = ENOMEM;
            goto construction_failed;
        }
        h->notification_bytes = malloc(HOST_NOTIFICATION_READ_BLOCK);
        if (!h->notification_bytes) {
            e = ENOMEM;
            goto construction_failed;
        }
        e = host_notification_init(&h->notification, config->notification_line_capacity);
        if (e)
            goto construction_failed;
        h->children[HOST_RUN].notifications_enabled = true;
        e = host_child_prepare_notifications(&h->children[HOST_RUN]);
        candidate_pipe_errors(h);
        if (e)
            goto construction_failed;
    }
    e = pthread_mutex_init(&h->inputs_mutex, NULL);
    if (e)
        goto construction_failed;
    h->construction_resources |= BUILD_INPUT_MUTEX;
    e = pthread_mutex_init(&h->claim_mutex, NULL);
    if (e)
        goto construction_failed;
    h->construction_resources |= BUILD_CLAIM_MUTEX;
    int pipe_result = host_pipe(h->wake);
    int pipe_error = errno;
    candidate_pipe_errors(h);
    if (pipe_result) {
        e = pipe_error;
        goto construction_failed;
    }
    h->construction_resources |= BUILD_WAKE;
    if (fcntl(h->wake[1], F_SETFL, O_NONBLOCK)) {
        e = errno;
        goto construction_failed;
    }
    host_schedule_init(&h->schedule, initial != NULL, config->retry_limit, config->retry_delay_ms);
    h->construction_resources |= BUILD_LOGGER;
    e = host_logger_init(&h->logger, &h->config);
    if (e)
        goto construction_failed;
    host_log(&h->logger, "camctl_host version=%s", CAMCTL_HOST_VERSION);
    h->construction_resources |= BUILD_DIAGNOSTIC;
    e = host_diagnostic_init(&h->diagnostics, &h->config, &h->logger, &h->claim_mutex,
                             h->wake[1], h->children);
    if (e)
        goto construction_failed;
    if (h->motor) {
        e = pthread_mutex_init(&h->callback_mutex, NULL);
        if (e)
            goto construction_failed;
        h->construction_resources |= BUILD_CALLBACK_MUTEX;
        e = pthread_cond_init(&h->callback_ready, NULL);
        if (e)
            goto construction_failed;
        h->construction_resources |= BUILD_CALLBACK_COND;
        e = pthread_create(&h->callback_thread, NULL, callback_main, h);
        if (e)
            goto construction_failed;
    }
    if (h->motor)
        h->construction_resources |= BUILD_CALLBACK_THREAD;
    e = pthread_create(&h->worker, NULL, worker_main, h);
    if (e)
        goto construction_failed;
    report = candidate_report(h, 0, 0);
    instance = h;
    if (restarting)
        free(restarting);
    registrations.initialized = true;
    pthread_mutex_unlock(&instance_mutex);
    deliver_construction_report(&report);
    if (check_performed)
        deliver_maintenance_check(check_result, 0);
    free(check_result);
    pthread_setcancelstate(cancel_state, NULL);
    return 0;
construction_failed: {
    int cleanup_error = cleanup_candidate(h);
    report = candidate_report(h, e, cleanup_error);
    if (cleanup_error) {
        h->construction_error = e;
        construction_pending = h;
    } else {
        free(h);
    }
}
unlock:
    pthread_mutex_unlock(&instance_mutex);
    deliver_construction_report(&report);
    if (check_performed)
        deliver_maintenance_check(check_result, e);
    free(check_result);
    pthread_setcancelstate(cancel_state, NULL);
    return failure(e);
}
int camctl_host_init(const camctl_host_config *config, const char *initial) {
    return host_start(config, initial, NULL);
}
int camctl_host_submit(const char *path) {
    host *h = borrow_instance();
    if (!h)
        return -1;
    host_submission submission = {0};
    int e = pthread_cond_init(&submission.ready, NULL);
    if (e) {
        release_instance(h);
        return failure(e);
    }
    /* 入队后，完成记录必须保持有效；取消在工作线程释放该记录后生效。 */
    int cancel_state;
    e = pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &cancel_state);
    if (e) {
        pthread_cond_destroy(&submission.ready);
        release_instance(h);
        return failure(e);
    }
    pthread_mutex_lock(&instance_mutex);
    pthread_mutex_lock(&h->inputs_mutex);
    uint64_t id = h->next_id;
    e = h->lifecycle != HOST_RUNNING ? ECANCELED : h->runtime_error ? h->runtime_error
        : host_inputs_push(&h->inputs, path, id, &submission);
    if (!e)
        h->next_id++;
    pthread_mutex_unlock(&h->inputs_mutex);
    pthread_mutex_unlock(&instance_mutex);
    if (e) {
        host_log(&h->logger, "submit rejected errno=%d", e);
        pthread_cond_destroy(&submission.ready);
        release_instance(h);
        pthread_setcancelstate(cancel_state, NULL);
        pthread_testcancel();
        return failure(e);
    }
    host_log(&h->logger, "call=%" PRIu64 " submit accepted path=%s", id, path);
    /* 即使通知被信号中断，最长 50 ms 的轮询仍会发现已接收输入。 */
    ssize_t sent = write(h->wake[1], "x", 1);
    if (sent < 0 && errno != EAGAIN && errno != EINTR)
        host_log(&h->logger, "notification_write_failed errno=%d", errno);
    pthread_mutex_lock(&h->inputs_mutex);
    while (!submission.done)
        pthread_cond_wait(&submission.ready, &h->inputs_mutex);
    e = submission.error;
    pthread_mutex_unlock(&h->inputs_mutex);
    pthread_cond_destroy(&submission.ready);
    release_instance(h);
    pthread_setcancelstate(cancel_state, NULL);
    pthread_testcancel();
    return e ? failure(e) : 0;
}
void camctl_host_claim(void) {
    host *h = borrow_instance();
    if (!h)
        return;
    int cancel_state;
    int error = pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &cancel_state);
    if (error) {
        release_instance(h);
        errno = error;
        return;
    }
    pthread_mutex_lock(&h->claim_mutex);
    host_claim_posix(&h->config, &h->logger);
    pthread_mutex_unlock(&h->claim_mutex);
    release_instance(h);
    pthread_setcancelstate(cancel_state, NULL);
    pthread_testcancel();
}

static int stop_result(int error, int cancel_state) {
    pthread_setcancelstate(cancel_state, NULL);
    pthread_testcancel();
    return error ? failure(error) : 0;
}
static void stop_participant_leave(void *unused) {
    (void)unused;
    atomic_fetch_sub(&stop_participants, 1);
}
static int stop_participant_result(volatile int error, int cancel_state) {
    pthread_cleanup_push(stop_participant_leave, NULL);
    pthread_setcancelstate(cancel_state, NULL);
    pthread_testcancel();
    pthread_cleanup_pop(1);
    return error ? failure(error) : 0;
}
static void release_storage(host *h) {
    if (h->motor) {
        pthread_cond_destroy(&h->callback_ready);
        pthread_mutex_destroy(&h->callback_mutex);
        host_notification_destroy(&h->notification);
        free(h->callbacks.items);
        h->callbacks.items = NULL;
        free(h->notification_bytes);
        h->notification_bytes = NULL;
    }
    for (int i = 0; i < 2; i++) {
        host_child_close_notifications(&h->children[i]);
        free(h->children[i].output);
        h->children[i].output = NULL;
        free(h->children[i].stderr_output);
        h->children[i].stderr_output = NULL;
        host_group_scan_reset(&h->children[i].scan);
    }
    close(h->wake[0]);
    close(h->wake[1]);
    h->wake[0] = h->wake[1] = -1;
    free(h->inputs.items);
    h->inputs.items = NULL;
    pthread_mutex_destroy(&h->inputs_mutex);
    pthread_mutex_destroy(&h->claim_mutex);
}
int camctl_host_stop(void) {
    int cancel_state;
    construction_report report = {0};
    int error = pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &cancel_state);
    if (error)
        return failure(error);
    atomic_fetch_add(&stop_participants, 1);
    pthread_mutex_lock(&instance_mutex);
    if ((maintenance_active || construction_pending) && module_thread) {
        pthread_mutex_unlock(&instance_mutex);
        return stop_participant_result(EDEADLK, cancel_state);
    }
    while (maintenance_active && !maintenance_owner) {
        error = pthread_cond_wait(&instance_ready, &instance_mutex);
        if (error) {
            pthread_mutex_unlock(&instance_mutex);
            return stop_participant_result(error, cancel_state);
        }
    }
    if (construction_pending) {
        host *candidate = construction_pending;
        int cleanup_error = cleanup_candidate(candidate);
        construction_report report = candidate_report(candidate, 0, cleanup_error);
        if (!cleanup_error) {
            construction_pending = NULL;
            free(candidate);
        }
        pthread_mutex_unlock(&instance_mutex);
        deliver_construction_report(&report);
        if (cleanup_error)
            return stop_participant_result(cleanup_error, cancel_state);
        pthread_mutex_lock(&instance_mutex);
        while (maintenance_active && !maintenance_owner) {
            error = pthread_cond_wait(&instance_ready, &instance_mutex);
            if (error) {
                pthread_mutex_unlock(&instance_mutex);
                return stop_participant_result(error, cancel_state);
            }
        }
    }
    host *h = instance;
    if (!h) {
        pthread_mutex_unlock(&instance_mutex);
        return stop_participant_result(ENODEV, cancel_state);
    }
    for (;;) {
        if (h->lifecycle == HOST_STOPPED) {
            while (maintenance_active && !maintenance_owner) {
                error = pthread_cond_wait(&instance_ready, &instance_mutex);
                if (error) {
                    pthread_mutex_unlock(&instance_mutex);
                    return stop_participant_result(error, cancel_state);
                }
            }
            pthread_mutex_unlock(&instance_mutex);
            return stop_participant_result(0, cancel_state);
        }
        if (module_thread) {
            pthread_mutex_unlock(&instance_mutex);
            return stop_participant_result(EDEADLK, cancel_state);
        }
        if (!h->stop_joining)
            break;
        error = pthread_cond_wait(&instance_ready, &instance_mutex);
        if (error) {
            pthread_mutex_unlock(&instance_mutex);
            return stop_participant_result(error, cancel_state);
        }
    }
    h->lifecycle = HOST_STOPPING;
    h->stop_joining = true;
    pthread_mutex_unlock(&instance_mutex);
    wake_worker(h);

    if (!h->worker_joined) {
        error = pthread_join(h->worker, NULL);
        if (error)
            goto failed;
        pthread_mutex_lock(&instance_mutex);
        h->worker_joined = true;
        pthread_mutex_unlock(&instance_mutex);
    }
    if (h->motor && !h->callback_joined) {
        error = pthread_join(h->callback_thread, NULL);
        if (error)
            goto failed;
        pthread_mutex_lock(&instance_mutex);
        h->callback_joined = true;
        pthread_mutex_unlock(&instance_mutex);
    }
    pthread_mutex_lock(&instance_mutex);
    while (h->api_users && !error)
        error = pthread_cond_wait(&instance_ready, &instance_mutex);
    pthread_mutex_unlock(&instance_mutex);
    if (error)
        goto failed;
    error = host_diagnostic_close(&h->diagnostics);
    if (error)
        goto failed;
    error = host_logger_close(&h->logger);
    if (error)
        goto failed;
    candidate_fact(h, "diagnostic_cond_destroy", -1, h->diagnostics.cond_error);
    candidate_fact(h, "diagnostic_mutex_destroy", -1, h->diagnostics.mutex_error);
    candidate_fact(h, "logger_cond_destroy", -1, h->logger.cond_error);
    candidate_fact(h, "logger_mutex_destroy", -1, h->logger.mutex_error);
    candidate_fact(h, "close", h->logger.close_error_fd, h->logger.close_error);
    report = candidate_report(h, 0, 0);
    release_storage(h);
    pthread_mutex_lock(&instance_mutex);
    h->lifecycle = HOST_STOPPED;
    h->stop_joining = false;
    pthread_cond_broadcast(&instance_ready);
    while (maintenance_active && !maintenance_owner) {
        error = pthread_cond_wait(&instance_ready, &instance_mutex);
        if (error)
            break;
    }
    pthread_mutex_unlock(&instance_mutex);
    deliver_construction_report(&report);
    return stop_participant_result(error, cancel_state);

failed:
    pthread_mutex_lock(&instance_mutex);
    h->stop_joining = false;
    pthread_cond_broadcast(&instance_ready);
    pthread_mutex_unlock(&instance_mutex);
    return stop_participant_result(error, cancel_state);
}

static bool same_optional_path(const char *a, const char *b) {
    return (!a && !b) || (a && b && !strcmp(a, b));
}

int camctl_host_update(const camctl_host_update_config *config,
                       const camctl_host_update_request *request, camctl_host_update_result *result) {
    if (!result)
        return failure(EINVAL);
    host_maintenance_result_init(result, request ? request->action : CAMCTL_UPDATE_ACTION_INVALID);
    int error = host_maintenance_validate(config, request, false);
    if (error)
        return failure(error);
    int cancel_state;
    error = pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &cancel_state);
    if (error)
        return failure(error);
    pthread_mutex_lock(&instance_mutex);
    if (module_thread)
        error = EDEADLK;
    else if (maintenance_active || atomic_load(&restart_active) || construction_pending)
        error = EBUSY;
    else if (instance && (!same_optional_path(instance->config.camctl_path, config->camctl_path) ||
                          !same_optional_path(instance->config.updater_path, config->updater_path) ||
                          !same_optional_path(instance->config.config_path, config->config_path)))
        error = EINVAL;
    else {
        maintenance_active = true;
        maintenance_owner = true;
    }
    host *h = instance;
    pthread_mutex_unlock(&instance_mutex);
    if (error)
        return stop_result(error, cancel_state);
    if (h && camctl_host_stop())
        error = errno;
    if (!error)
        error = host_maintenance_run(config, request, result);
    pthread_mutex_lock(&instance_mutex);
    maintenance_owner = false;
    maintenance_active = false;
    pthread_cond_broadcast(&instance_ready);
    pthread_mutex_unlock(&instance_mutex);
    return stop_result(error, cancel_state);
}

int camctl_host_restart(void) {
    if (module_thread)
        return failure(EDEADLK);
    int cancel_state;
    int error = pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &cancel_state);
    if (error)
        return failure(error);
    if (atomic_exchange(&restart_active, true))
        return stop_result(EBUSY, cancel_state);
    pthread_mutex_lock(&instance_mutex);
    host *previous = instance;
    error = 0;
    if (maintenance_active || construction_pending || atomic_load(&stop_participants))
        error = EBUSY;
    else if (!previous)
        error = ENODEV;
    else if (previous->lifecycle == HOST_RUNNING)
        error = EALREADY;
    else if (previous->lifecycle != HOST_STOPPED)
        error = EBUSY;
    pthread_mutex_unlock(&instance_mutex);
    if (!error && host_start(&previous->config, NULL, previous))
        error = errno;
    atomic_store(&restart_active, false);
    return stop_result(error, cancel_state);
}
