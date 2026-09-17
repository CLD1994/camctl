#include "camctl_host.h"
#include "claim.h"
#include "config.h"
#include "input.h"
#include "process.h"
#include "scheduler.h"
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

typedef struct {
    camctl_host_config config;
    char paths[6][CAMCTL_HOST_PATH_MAX + 1];
    host_schedule schedule;
    host_inputs inputs;
    host_child children[2];
    host_logger logger;
    pthread_mutex_t inputs_mutex, claim_mutex;
    pthread_t worker;
    int wake[2];
    uint64_t next_id;
    bool clock_failed;
    int runtime_error; /* inputs_mutex 保护；无法继续启动时拒绝新路径。 */
} host;
static pthread_mutex_t instance_mutex = PTHREAD_MUTEX_INITIALIZER;
static host *instance;
static const char *command_name(host_command c) { return c == HOST_RUN ? "run" : "submit"; }
static int failure(int e) {
    errno = e;
    return -1;
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
    host_log(&c->h->logger, "call=%" PRIu64 " command=%s pid=%ld stream=%s data=%.*s", c->child->id,
             command_name(c->command), (long)c->child->pid, stream, (int)n, data);
}
static void finish_submit(host *h) {
    pthread_mutex_lock(&h->inputs_mutex);
    host_inputs_pop(&h->inputs);
    pthread_mutex_unlock(&h->inputs_mutex);
}
static void collect(host *h, host_command command, uint64_t now) {
    host_child *c = &h->children[command];
    if (!c->pid)
        return;
    child_log_context ctx = {h, c, command};
    host_child_collect(c, child_log, &ctx);
    if (!host_child_done(c))
        return;
    host_result r = c->overflow || c->io_error ? (host_result){HOST_RESULT_ABNORMAL, false}
                                               : host_result_parse(command, c->output, c->length,
                                                                   c->termination, c->exit_code);
    const char *names[] = {"abnormal", "success", "error"};
    host_log(
        &h->logger,
        "call=%" PRIu64
        " command=%s pid=%ld result=%s exit_kind=%d exit=%d needs_run=%d overflow=%d io_error=%d",
        c->id, command_name(command), (long)c->pid, names[r.kind], c->termination, c->exit_code,
        r.needs_run, c->overflow, c->io_error);
    if (r.kind != HOST_RESULT_SUCCESS)
        host_log(&h->logger, "call=%" PRIu64 " stdout=%.*s", c->id, (int)c->length, c->output);
    host_schedule_finished(&h->schedule, command, r, now);
    c->pid = 0;
    if (command == HOST_SUBMIT)
        finish_submit(h);
}
static void launch(host *h, host_command command, uint64_t now) {
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
                finish_submit(h);
        }
        return;
    }
    if (command == HOST_RUN) {
        path = s->first_plan ? h->paths[5] : NULL;
        pthread_mutex_lock(&h->inputs_mutex);
        id = h->next_id++;
        pthread_mutex_unlock(&h->inputs_mutex);
    }
    host_child *c = &h->children[command];
    c->id = id;
    host_log(&h->logger,
             "call=%" PRIu64 " command=%s operation=spawn plan=%s automatic=%d retries=%u", id,
             command_name(command), path ? path : "(none)", s->retries_used != retries,
             s->retries_used);
    int e = host_child_spawn(c, &h->config, command, path);
    if (e) {
        host_log(&h->logger, "call=%" PRIu64 " command=%s not_started errno=%d detail=%s", id,
                 command_name(command), e, strerror(e));
        host_schedule_spawn_failed(s, command, now_ms(h));
    } else {
        host_schedule_spawned(s, command);
        if (command == HOST_RUN)
            h->paths[5][0] = 0;
        host_log(&h->logger, "call=%" PRIu64 " command=%s pid=%ld spawned execution=unknown", id,
                 command_name(command), (long)c->pid);
    }
}
static void *worker_main(void *p) {
    host *h = p;
    for (;;) {
        uint64_t now = now_ms(h);
        collect(h, HOST_RUN, now);
        collect(h, HOST_SUBMIT, now);
        if (!h->clock_failed) {
            launch(h, HOST_RUN, now);
            launch(h, HOST_SUBMIT, now);
        }
        struct pollfd fds[5];
        nfds_t n = 1;
        fds[0] = (struct pollfd){h->wake[0], POLLIN, 0};
        for (int i = 0; i < 2; i++) {
            if (h->children[i].out_fd >= 0)
                fds[n++] = (struct pollfd){h->children[i].out_fd, POLLIN, 0};
            if (h->children[i].err_fd >= 0)
                fds[n++] = (struct pollfd){h->children[i].err_fd, POLLIN, 0};
        }
        if (poll(fds, n, 50) < 0 && errno != EINTR) {
            int e = errno;
            pthread_mutex_lock(&h->inputs_mutex);
            h->runtime_error = e;
            pthread_mutex_unlock(&h->inputs_mutex);
            host_log(&h->logger, "poll_failed errno=%d", e);
            /* 保留进程占位，避免错误循环；该线程停止，公开领取仍可工作。 */
            return NULL;
        }
        if (fds[0].revents & POLLIN) {
            char bytes[256];
            ssize_t received = read(h->wake[0], bytes, sizeof(bytes));
            if (received < 0 && errno != EAGAIN && errno != EINTR)
                host_log(&h->logger, "notification_read_failed errno=%d", errno);
        }
    }
}
int camctl_host_init(const camctl_host_config *config, const char *initial) {
    pthread_mutex_lock(&instance_mutex);
    int e;
    if (instance) {
        e = EALREADY;
        goto unlock;
    }
    e = host_config_validate(config, initial);
    if (e)
        goto unlock;
    host *h = calloc(1, sizeof(*h));
    if (!h) {
        e = ENOMEM;
        goto unlock;
    }
    h->config = *config;
    const char *paths[] = {config->camctl_path, config->ready_path,  config->processing_path,
                           config->log_path,    config->config_path, initial};
    const char **owned[] = {&h->config.camctl_path, &h->config.ready_path,
                            &h->config.processing_path, &h->config.log_path,
                            &h->config.config_path};
    for (size_t i = 0; i < 6; i++) {
        if (paths[i])
            strcpy(h->paths[i], paths[i]);
        if (i < 5)
            *owned[i] = paths[i] ? h->paths[i] : NULL;
    }
    h->inputs.capacity = config->plan_capacity;
    h->next_id = 1;
    h->inputs.items = calloc(config->plan_capacity, sizeof(host_input));
    for (int i = 0; i < 2; i++)
        host_child_init(&h->children[i], malloc(config->stdout_capacity), config->stdout_capacity);
    if (!h->inputs.items || !h->children[0].output || !h->children[1].output) {
        e = ENOMEM;
        goto memory;
    }
    e = pthread_mutex_init(&h->inputs_mutex, NULL);
    if (e)
        goto memory;
    e = pthread_mutex_init(&h->claim_mutex, NULL);
    if (e)
        goto input_mutex;
    if (host_pipe(h->wake)) {
        e = errno;
        goto claim_mutex;
    }
    if (fcntl(h->wake[1], F_SETFL, O_NONBLOCK)) {
        e = errno;
        goto pipe;
    }
    host_schedule_init(&h->schedule, initial != NULL, config->retry_limit, config->retry_delay_ms);
    e = host_logger_init(&h->logger, &h->config);
    if (e)
        goto pipe;
    e = pthread_create(&h->worker, NULL, worker_main, h);
    if (e) {
        host_logger_abort(&h->logger);
        goto pipe;
    }
    instance = h;
    pthread_mutex_unlock(&instance_mutex);
    return 0;
pipe:
    close(h->wake[0]);
    close(h->wake[1]);
claim_mutex:
    pthread_mutex_destroy(&h->claim_mutex);
input_mutex:
    pthread_mutex_destroy(&h->inputs_mutex);
memory:
    free(h->children[0].output);
    free(h->children[1].output);
    free(h->inputs.items);
    free(h);
unlock:
    pthread_mutex_unlock(&instance_mutex);
    return failure(e);
}
int camctl_host_submit(const char *path) {
    pthread_mutex_lock(&instance_mutex);
    host *h = instance;
    pthread_mutex_unlock(&instance_mutex);
    if (!h)
        return failure(ENODEV);
    pthread_mutex_lock(&h->inputs_mutex);
    uint64_t id = h->next_id;
    int e = h->runtime_error ? h->runtime_error : host_inputs_push(&h->inputs, path, id);
    if (!e)
        h->next_id++;
    pthread_mutex_unlock(&h->inputs_mutex);
    if (e) {
        host_log(&h->logger, "submit rejected errno=%d", e);
        return failure(e);
    }
    host_log(&h->logger, "call=%" PRIu64 " submit accepted path=%s", id, path);
    /* 即使通知被信号中断，最长 50 ms 的轮询仍会发现已接收输入。 */
    ssize_t sent = write(h->wake[1], "x", 1);
    if (sent < 0 && errno != EAGAIN && errno != EINTR)
        host_log(&h->logger, "notification_write_failed errno=%d", errno);
    return 0;
}
void camctl_host_claim(void) {
    pthread_mutex_lock(&instance_mutex);
    host *h = instance;
    pthread_mutex_unlock(&instance_mutex);
    if (!h) {
        errno = ENODEV;
        return;
    }
    pthread_mutex_lock(&h->claim_mutex);
    host_claim_posix(&h->config, &h->logger);
    pthread_mutex_unlock(&h->claim_mutex);
}
