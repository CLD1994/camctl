#define _GNU_SOURCE
#include "camctl_host.h"
#include "callback_queue.h"
#include "process.h"
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t ready = PTHREAD_COND_INITIALIZER;
static int received[128], count, permits;
static const char *callback_root;
static atomic_bool full_observed, reaped_observed, output_observed, channel_retained;
static atomic_int eof_count, first_result = -1, first_exit;
static atomic_long first_pid;
static bool pause_full_management;
static void mark(const char *root, const char *name);
static void delay(void) {
    struct timespec t = {0, 1000000};
    nanosleep(&t, NULL);
}
static void check(bool condition, const char *expression, int line) {
    if (condition)
        return;
    fprintf(stderr, "notification check line=%d expression=%s\n", line, expression);
    pid_t pid = (pid_t)atomic_load(&first_pid);
    if (pid > 1)
        kill(-pid, SIGKILL);
    abort();
}
#define CHECK(c) check((c), #c, __LINE__)
/* 包装只观察真实队列状态，不等待、不修改队列，也不增加生产接口。 */
bool __real_host_callback_queue_push(host_callback_queue *, host_notification);
bool __wrap_host_callback_queue_push(host_callback_queue *q, host_notification n) {
    bool accepted = __real_host_callback_queue_push(q, n);
    if (accepted && q->length == q->capacity)
        atomic_store(&full_observed, true);
    return accepted;
}
void __real_host_child_collect(host_child *, host_child_log, void *);
void __wrap_host_child_collect(host_child *c, host_child_log log, void *context) {
    if (c->notifications_enabled && !atomic_load(&first_pid))
        atomic_store(&first_pid, c->pid);
    /* 负向核验模拟满队列错误地停止全部进程输出管理。 */
    if (pause_full_management && atomic_load(&full_observed) && c->notifications_enabled)
        return;
    __real_host_child_collect(c, log, context);
    if (c->notifications_enabled && c->pid == atomic_load(&first_pid) && c->reaped) {
        atomic_store(&reaped_observed, true);
        if (c->notification_fd >= 0)
            atomic_store(&channel_retained, true);
        if (c->out_fd < 0 && c->length)
            atomic_store(&output_observed, true);
    }
}
void __real_host_notification_eof(host_notification_stream *, host_notification_diagnostic, void *);
void __wrap_host_notification_eof(host_notification_stream *s, host_notification_diagnostic d,
                                  void *p) {
    __real_host_notification_eof(s, d, p);
    atomic_fetch_add(&eof_count, 1);
}
host_result __real_host_result_parse(host_command, const char *, size_t, host_exit, int);
host_result __wrap_host_result_parse(host_command c, const char *data, size_t len, host_exit exit,
                                     int code) {
    host_result result = __real_host_result_parse(c, data, len, exit, code);
    if (c == HOST_RUN && atomic_load(&first_result) == -1) {
        atomic_store(&first_exit, code);
        atomic_store(&first_result, result.kind);
    }
    return result;
}
static void motor(int p) {
    pthread_mutex_lock(&mutex);
    CHECK(count < (int)(sizeof(received) / sizeof(received[0])));
    received[count++] = p;
    if (p == 0)
        mark(callback_root, "callback-started");
    pthread_cond_broadcast(&ready);
    while (!permits)
        pthread_cond_wait(&ready, &mutex);
    permits--;
    pthread_mutex_unlock(&mutex);
    if (p == 0)
        CHECK(!camctl_host_submit("/from-callback"));
}
static int callback_count(void) {
    pthread_mutex_lock(&mutex);
    int n = count;
    pthread_mutex_unlock(&mutex);
    return n;
}
static void wait_callback(int n) {
    for (int i = 0; i < 5000 && callback_count() < n; i++)
        delay();
    CHECK(callback_count() == n);
}
static void release_one(void) {
    pthread_mutex_lock(&mutex);
    permits++;
    pthread_cond_signal(&ready);
    pthread_mutex_unlock(&mutex);
}
static void file(char *out, size_t n, const char *root, const char *suffix) {
    snprintf(out, n, "%s/%s", root, suffix);
}
static bool exists(const char *root, const char *name) {
    char path[512];
    file(path, sizeof(path), root, name);
    return !access(path, F_OK);
}
static void wait_file(const char *root, const char *name) {
    for (int i = 0; i < 5000 && !exists(root, name); i++)
        delay();
    CHECK(exists(root, name));
}
static void mark(const char *root, const char *name) {
    char path[512];
    file(path, sizeof(path), root, name);
    int fd = open(path, O_CREAT | O_WRONLY, 0600);
    CHECK(fd >= 0);
    close(fd);
}
static void allow_stderr(const char *root) {
    CHECK(atomic_load(&full_observed));
    CHECK(callback_count() == 1);
    mark(root, "allow-stderr");
}
static int cli(int argc, char **argv) {
    const char *root = NULL;
    int notify = -1;
    for (int i = 2; i < argc; i++) {
        if (!strcmp(argv[i], "--config"))
            root = argv[++i];
        else if (!strcmp(argv[i], "--host-notification-fd"))
            notify = atoi(argv[++i]);
    }
    CHECK(root);
    if (!strcmp(argv[1], "submit")) {
        CHECK(notify == -1);
        mark(root, "submitted");
        puts("{\"kind\":\"succeeded\",\"body\":{\"needs_run\":true}}");
        return 0;
    }
    CHECK(notify >= 3);
    bool initial = !exists(root, "first");
    mark(root, initial ? "first" : "second");
    int n = initial ? 100 : 1;
    for (int i = 0; i < n; i++) {
        char line[256];
        int len = snprintf(line, sizeof(line),
                           "{\"type\":\"motor_control\",\"action_instance_id\":\"1\",\"params\":{"
                           "\"position\":%d}}\n",
                           initial ? i : -7);
        CHECK(write(notify, line, (size_t)len) == len);
        if (initial && i == 0) {
            wait_file(root, "callback-started");
            wait_file(root, "allow-fill");
        }
        if (initial && i == 2) {
            wait_file(root, "allow-stderr");
            int capacity = fcntl(2, F_GETPIPE_SZ);
            CHECK(capacity > 0);
            char noise[4096];
            memset(noise, 'x', sizeof(noise));
            size_t remaining = (size_t)capacity + sizeof(noise);
            while (remaining) {
                size_t bytes = remaining < sizeof(noise) ? remaining : sizeof(noise);
                CHECK(write(2, noise, bytes) == (ssize_t)bytes);
                remaining -= bytes;
            }
            mark(root, "stderr-complete");
        }
    }
    if (initial)
        CHECK(write(notify, "{\"type\":", 8) == 8);
    close(notify);
    puts("{\"kind\":\"succeeded\"}");
    CHECK(!fflush(stdout));
    if (initial)
        mark(root, "stdout-complete");
    return argc > 2 && !strcmp(argv[2], "/exit7") ? 7 : 0;
}
int main(int argc, char **argv) {
    struct rlimit no_core = {0, 0};
    CHECK(!setrlimit(RLIMIT_CORE, &no_core));
    if (argc > 1 && (!strcmp(argv[1], "run") || !strcmp(argv[1], "submit")))
        return cli(argc, argv);
    bool abnormal = argc > 1 && !strcmp(argv[1], "abnormal");
    bool early_signal = argc > 1 && !strcmp(argv[1], "early-signal");
    pause_full_management = argc > 1 && !strcmp(argv[1], "pause-management");
    char root[] = "/tmp/host-notify-XXXXXX";
    CHECK(mkdtemp(root));
    callback_root = root;
    char executable[4096];
    ssize_t n = readlink("/proc/self/exe", executable, sizeof(executable) - 1);
    CHECK(n > 0);
    executable[n] = 0;
    camctl_host_config c = CAMCTL_HOST_CONFIG_INIT;
    c.camctl_path = executable;
    c.ready_path = root;
    c.processing_path = root;
    c.config_path = root;
    char log[512];
    file(log, sizeof(log), root, "log");
    c.log_path = log;
    c.notification_queue_capacity = 2;
    c.retry_limit = 0;
    CHECK(camctl_host_register_motor_control_callback(NULL) == -1 && errno == EINVAL);
    CHECK(!camctl_host_register_motor_control_callback(motor));
    CHECK(camctl_host_register_motor_control_callback(motor) == -1 && errno == EALREADY);
    CHECK(!camctl_host_init(&c, abnormal ? "/exit7" : NULL));
    CHECK(camctl_host_register_motor_control_callback(NULL) == -1 && errno == EINVAL);
    CHECK(camctl_host_register_motor_control_callback(motor) == -1 && errno == EBUSY);
    wait_callback(1);
    CHECK(received[0] == 0);
    CHECK(!atomic_load(&full_observed));
    if (early_signal)
        allow_stderr(root); /* 实际Q<N，继续信号必须被拒绝。 */
    mark(root, "allow-fill");
    for (int i = 0; i < 5000 && !atomic_load(&full_observed); i++)
        delay();
    CHECK(atomic_load(&full_observed));
    allow_stderr(root);
    CHECK(!camctl_host_submit("/second-run"));
    wait_file(root, "stderr-complete");
    wait_file(root, "stdout-complete");
    wait_file(root, "submitted");
    for (int i = 0; i < 5000 && !(atomic_load(&reaped_observed) && atomic_load(&output_observed));
         i++)
        delay();
    CHECK(atomic_load(&reaped_observed));
    CHECK(atomic_load(&output_observed));
    CHECK(atomic_load(&channel_retained));
    CHECK(!atomic_load(&eof_count));
    CHECK(atomic_load(&first_result) == -1);
    CHECK(callback_count() == 1);
    CHECK(!exists(root, "second"));
    /* 每次仅放行一个返回；98号阻塞时99号仍排队，旧EOF和下一run必须推进。 */
    for (int target = 2; target <= 99; target++) {
        release_one();
        wait_callback(target);
    }
    for (int i = 0; i < 5000 && (!atomic_load(&eof_count) || atomic_load(&first_result) == -1 ||
                                 !exists(root, "second"));
         i++)
        delay();
    CHECK(atomic_load(&eof_count) >= 1);
    CHECK(atomic_load(&first_exit) == (abnormal ? 7 : 0));
    CHECK(atomic_load(&first_result) == (abnormal ? HOST_RESULT_ABNORMAL : HOST_RESULT_SUCCESS));
    CHECK(exists(root, "second"));
    CHECK(callback_count() == 99);
    for (int i = 0; i < 99; i++)
        CHECK(received[i] == i);
    release_one();
    wait_callback(100);
    CHECK(received[99] == 99);
    release_one();
    wait_callback(101);
    CHECK(received[100] == -7);
    release_one();
    return 0;
}
