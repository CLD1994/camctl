#define _GNU_SOURCE
#include "camctl_host.h"
#include "process_group.h"
#include "result.h"
#include <assert.h>
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <time.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
static void handle(int sig) { (void)sig; }
static atomic_bool break_clock;
static atomic_bool suppress_kill, hold_after_kill;
static atomic_int scan_mode, group_signals;
static atomic_bool final_scan_delayed;
static atomic_uint_fast64_t parsed_at, retry_gap;
static atomic_int run_attempts;
static uint32_t final_scan_delay_ms;
static bool fail_completion_clock;
int __real_clock_gettime(clockid_t, struct timespec *);
static uint64_t observation_ns(void) {
    struct timespec value;
    assert(!__real_clock_gettime(CLOCK_MONOTONIC, &value));
    return (uint64_t)value.tv_sec * 1000000000 + (uint64_t)value.tv_nsec;
}
host_result __real_host_result_parse(host_command, const char *, size_t, host_exit, int);
host_result __wrap_host_result_parse(host_command command, const char *text, size_t length,
                                    host_exit termination, int code) {
    host_result result = __real_host_result_parse(command, text, length, termination, code);
    if (command == HOST_RUN && result.kind == HOST_RESULT_ABNORMAL && !atomic_load(&parsed_at)) {
        atomic_store(&parsed_at, observation_ns());
        if (fail_completion_clock)
            atomic_store(&break_clock, true);
    }
    return result;
}
int __real_posix_spawn(pid_t *, const char *, const posix_spawn_file_actions_t *,
                       const posix_spawnattr_t *, char *const[], char *const[]);
int __wrap_posix_spawn(pid_t *pid, const char *path, const posix_spawn_file_actions_t *actions,
                       const posix_spawnattr_t *attributes, char *const args[], char *const env[]) {
    if (!strcmp(args[1], "run") && atomic_fetch_add(&run_attempts, 1) == 1) {
        uint64_t parsed = atomic_load(&parsed_at);
        if (parsed)
            atomic_store(&retry_gap, observation_ns() - parsed);
    }
    return __real_posix_spawn(pid, path, actions, attributes, args, env);
}
host_group_result __real_host_group_scan_batch(host_group_scan *, pid_t, size_t);
int __real_kill(pid_t, int);
host_group_result __wrap_host_group_scan_batch(host_group_scan *scan, pid_t pgid, size_t limit) {
    int mode = atomic_load(&scan_mode);
    if (mode)
        return (host_group_result){mode == 1 ? HOST_GROUP_PENDING : HOST_GROUP_UNKNOWN,
                                   HOST_GROUP_READ_THREAD, mode == 2 ? EACCES : 0, pgid};
    host_group_result result = __real_host_group_scan_batch(scan, pgid, limit);
    if (result.status == HOST_GROUP_COMPLETE && final_scan_delay_ms &&
        !atomic_exchange(&final_scan_delayed, true)) {
        struct timespec delay = {.tv_sec = final_scan_delay_ms / 1000,
                                 .tv_nsec = (long)(final_scan_delay_ms % 1000) * 1000000};
        while (nanosleep(&delay, &delay))
            assert(errno == EINTR);
    }
    return result;
}
int __wrap_kill(pid_t pid, int signal) {
    if (pid < 0 && signal == SIGKILL) {
        atomic_fetch_add(&group_signals, 1);
        if (atomic_load(&suppress_kill)) return 0;
        int result = __real_kill(pid, signal);
        if (!result && atomic_load(&hold_after_kill)) atomic_store(&scan_mode, 1);
        return result;
    }
    return __real_kill(pid, signal);
}
static pthread_mutex_t log_gate = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t log_condition = PTHREAD_COND_INITIALIZER;
static int slow_log, log_entered, slow_cleanup, cleanup_entered;
static bool deny_cleanup;
static char cleanup_path[CAMCTL_HOST_PATH_MAX + 32];
int __real_unlink(const char *);
int __wrap_unlink(const char *path) {
    if (!strcmp(path, cleanup_path)) {
        pthread_mutex_lock(&log_gate);
        cleanup_entered = 1;
        while (slow_cleanup)
            pthread_cond_wait(&log_condition, &log_gate);
        pthread_mutex_unlock(&log_gate);
        if (deny_cleanup) {
            errno = EACCES;
            return -1;
        }
    }
    return __real_unlink(path);
}
ssize_t __real_write(int, const void *, size_t);
int __wrap_clock_gettime(clockid_t clock, struct timespec *time) {
    if (clock == CLOCK_MONOTONIC && atomic_load(&break_clock)) {
        errno = EIO;
        return -1;
    }
    return __real_clock_gettime(clock, time);
}
ssize_t __wrap_write(int fd, const void *data, size_t length) {
    /* 驱动中只有模块日志向非标准描述符写多字节记录；通知只写一个字节。 */
    if (fd > 2 && length > 1) {
        pthread_mutex_lock(&log_gate);
        log_entered = 1;
        while (slow_log)
            pthread_cond_wait(&log_condition, &log_gate);
        pthread_mutex_unlock(&log_gate);
    }
    return __real_write(fd, data, length);
}
int main(int argc, char **argv) {
    assert(argc == 8);
    struct sigaction action = {0}, after;
    action.sa_handler = handle;
    sigemptyset(&action.sa_mask);
    assert(!sigaction(SIGUSR1, &action, NULL));
    char cwd[4096], later[4096];
    assert(getcwd(cwd, sizeof(cwd)));
    camctl_host_config cfg = CAMCTL_HOST_CONFIG_INIT;
    cfg.camctl_path = argv[1];
    cfg.ready_path = argv[2];
    cfg.processing_path = argv[3];
    cfg.log_path = argv[4];
    snprintf(cleanup_path, sizeof(cleanup_path), "%s.1", cfg.log_path);
    cfg.config_path = argv[5];
    cfg.retry_delay_ms = 20;
    if (getenv("HOST_TEST_RETRY_DELAY_MS"))
        cfg.retry_delay_ms = (uint32_t)atoi(getenv("HOST_TEST_RETRY_DELAY_MS"));
    if (getenv("HOST_TEST_FINAL_SCAN_DELAY_MS"))
        final_scan_delay_ms = (uint32_t)atoi(getenv("HOST_TEST_FINAL_SCAN_DELAY_MS"));
    fail_completion_clock = getenv("HOST_TEST_FAIL_COMPLETION_CLOCK") != NULL;
    cfg.plan_capacity = 2;
    cfg.stdout_capacity = 1024;
    cfg.log_queue_capacity = 4096;
    cfg.log_record_capacity = 512;
    cfg.log_file_size = 2048;
    if (getenv("HOST_TEST_LOG_FILE_COUNT"))
        cfg.log_file_count = (uint32_t)atoi(getenv("HOST_TEST_LOG_FILE_COUNT"));
    if (getenv("HOST_TEST_LARGE_LOG")) cfg.log_file_size = 256 * 1024;
    cfg.retry_limit = (uint32_t)atoi(argv[7]);
    char *initial = strcmp(argv[6], "-") ? argv[6] : NULL;
    setvbuf(stdout, NULL, _IOLBF, 0);
    slow_log = getenv("HOST_TEST_SLOW_LOG") != NULL;
    slow_cleanup = getenv("HOST_TEST_SLOW_ARCHIVE_CLEANUP") != NULL;
    deny_cleanup = getenv("HOST_TEST_DENY_ARCHIVE_CLEANUP") != NULL;
    assert(camctl_host_init(&cfg, initial) == 0);
    /* 调用返回后复用参数缓冲区，后台必须持有自己的副本。 */
    for (int i = 1; i <= 5; i++)
        memset(argv[i], 'x', strlen(argv[i]));
    memset(&cfg, 0, sizeof(cfg));
    puts("initialized");
    char line[8192];
    while (fgets(line, sizeof(line), stdin)) {
        line[strcspn(line, "\n")] = 0;
        if (!strncmp(line, "submit ", 7)) {
            int r = camctl_host_submit(line + 7);
            printf("submit %d\n", r);
            memset(line, 'x', strlen(line));
        } else if (!strcmp(line, "claim")) {
            camctl_host_claim();
            puts("claimed");
        } else if (!strcmp(line, "coexist")) {
            assert(getcwd(later, sizeof(later)));
            assert(!strcmp(cwd, later));
            assert(!sigaction(SIGUSR1, NULL, &after));
            assert(after.sa_handler == handle);
            pid_t pid = fork();
            assert(pid >= 0);
            if (!pid)
                _exit(23);
            int status;
            assert(waitpid(pid, &status, 0) == pid);
            assert(WEXITSTATUS(status) == 23);
            puts("coexists");
        } else if (!strcmp(line, "reinit")) {
            assert(camctl_host_init(NULL, NULL) == -1);
            puts("rejected");
        } else if (!strcmp(line, "break-clock")) {
            atomic_store(&break_clock, true);
            puts("clock-failed");
        } else if (!strcmp(line, "log-state")) {
            pthread_mutex_lock(&log_gate);
            printf("log-entered %d\n", log_entered);
            pthread_mutex_unlock(&log_gate);
        } else if (!strcmp(line, "unblock-log")) {
            pthread_mutex_lock(&log_gate);
            slow_log = 0;
            slow_cleanup = 0;
            pthread_cond_signal(&log_condition);
            pthread_mutex_unlock(&log_gate);
            puts("log-resumed");
        } else if (!strcmp(line, "cleanup-state")) {
            pthread_mutex_lock(&log_gate);
            printf("cleanup-entered %d\n", cleanup_entered);
            pthread_mutex_unlock(&log_gate);
        } else if (!strcmp(line, "block-kill")) {
            atomic_store(&suppress_kill, true);
            puts("kill-blocked");
        } else if (!strcmp(line, "kill-and-hold")) {
            atomic_store(&hold_after_kill, true);
            atomic_store(&suppress_kill, false);
            puts("kill-enabled");
        } else if (!strcmp(line, "release-kill")) {
            atomic_store(&suppress_kill, false);
            puts("kill-enabled");
        } else if (!strcmp(line, "pause-scan") || !strcmp(line, "deny-scan")) {
            atomic_store(&scan_mode, !strcmp(line, "pause-scan") ? 1 : 2);
            puts("scan-held");
        } else if (!strcmp(line, "resume-scan")) {
            atomic_store(&hold_after_kill, false);
            atomic_store(&scan_mode, 0);
            puts("scan-enabled");
        } else if (!strcmp(line, "group-signals")) {
            printf("group-signals %d\n", atomic_load(&group_signals));
        } else if (!strcmp(line, "retry-gap")) {
            printf("retry-gap %.3f\n", (double)atomic_load(&retry_gap) / 1000000);
        } else if (!strncmp(line, "steal-exit ", 11)) {
            int status;
            pid_t child = (pid_t)strtol(line + 11, NULL, 10);
            printf("stolen %ld\n", (long)waitpid(child, &status, WNOHANG));
        } else if (!strcmp(line, "ignore-children")) {
            action.sa_handler = SIG_IGN;
            assert(!sigaction(SIGCHLD, &action, NULL));
            puts("children-ignored");
        }
    }
    for (;;)
        pause();
}
