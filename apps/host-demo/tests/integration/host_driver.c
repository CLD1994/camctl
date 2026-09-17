#define _GNU_SOURCE
#include "camctl_host.h"
#include <assert.h>
#include <errno.h>
#include <signal.h>
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
static pthread_mutex_t log_gate = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t log_condition = PTHREAD_COND_INITIALIZER;
static int slow_log, log_entered;
int __real_clock_gettime(clockid_t, struct timespec *);
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
    cfg.config_path = argv[5];
    cfg.retry_delay_ms = 20;
    cfg.plan_capacity = 2;
    cfg.stdout_capacity = 1024;
    cfg.log_queue_capacity = 4096;
    cfg.log_record_capacity = 512;
    cfg.log_file_size = 2048;
    cfg.retry_limit = (uint32_t)atoi(argv[7]);
    char *initial = strcmp(argv[6], "-") ? argv[6] : NULL;
    setvbuf(stdout, NULL, _IOLBF, 0);
    slow_log = getenv("HOST_TEST_SLOW_LOG") != NULL;
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
            pthread_cond_signal(&log_condition);
            pthread_mutex_unlock(&log_gate);
            puts("log-resumed");
        }
    }
    for (;;)
        pause();
}
