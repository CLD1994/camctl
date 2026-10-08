#define _GNU_SOURCE
#include "process.h"
#include <assert.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <sys/wait.h>
#include <sys/prctl.h>
#include <signal.h>
#include <time.h>
#include <unistd.h>
static size_t err_bytes;
static void handle(int signal) { (void)signal; }
static void finish(host_child *c) {
    for (int i = 0; i < 5000 && !host_child_done(c); i++) {
        host_child_collect(c, NULL, NULL);
        struct timespec delay = {0, 1000000};
        nanosleep(&delay, NULL);
    }
    assert(host_child_done(c));
}
static void leftover_contract(camctl_host_config *cfg, const char *mode, bool threads) {
    char root[] = "/tmp/host-group-XXXXXX";
    assert(mkdtemp(root));
    char record[128];
    snprintf(record, sizeof(record), "%s/pid", root);
    cfg->config_path = record;
    char run_buffer[1024], submit_buffer[1024];
    host_child run, submit;
    host_child_init(&run, run_buffer, sizeof(run_buffer));
    host_child_init(&submit, submit_buffer, sizeof(submit_buffer));
    assert(!host_child_spawn(&submit, cfg, HOST_SUBMIT, "/hold"));
    assert(!host_child_spawn(&run, cfg, HOST_RUN, mode));
    assert(run.pgid == run.pid && getpgid(run.pid) == run.pgid);
    assert(submit.pgid == submit.pid && getpgid(submit.pid) == submit.pgid);
    assert(run.pgid != submit.pgid && run.pgid != getpgrp());
    siginfo_t info = {0};
    assert(!waitid(P_PID, run.pid, &info, WEXITED | WNOWAIT));
    assert(info.si_pid == run.pid);
    FILE *file = fopen(record, "r");
    assert(file);
    long value;
    assert(fscanf(file, "%ld", &value) == 1);
    fclose(file);
    pid_t tool = (pid_t)value;
    assert(getpgid(tool) == run.pgid);
    if (threads) {
        char path[128], stat[4096];
        snprintf(path, sizeof(path), "/proc/%ld/stat", value);
        bool zombie = false;
        for (int i = 0; i < 5000 && !zombie; i++) {
            FILE *stream = fopen(path, "r");
            assert(stream && fgets(stat, sizeof(stat), stream));
            fclose(stream);
            zombie = strrchr(stat, ')')[2] == 'Z';
            struct timespec delay = {0, 1000000};
            nanosleep(&delay, NULL);
        }
        assert(zombie);
    }
    finish(&run);
    assert(!run.io_error && !run.overflow);
    assert(run.exit_code == (!strcmp(mode, "/leftover-error") ? 7 : 0));
    /* 回收旧组不能终止并行 submit 或取走主程序其他子进程的结果。 */
    assert(!kill(submit.pid, 0));
    pid_t other = fork();
    assert(other >= 0);
    if (!other) _exit(23);
    int status;
    assert(waitpid(other, &status, 0) == other && WEXITSTATUS(status) == 23);
    assert(waitpid(tool, &status, 0) == tool && WIFSIGNALED(status));
    assert(WTERMSIG(status) == SIGKILL);
    assert(!kill(submit.pid, SIGTERM));
    finish(&submit);
    assert(submit.termination == HOST_EXIT_SIGNAL && submit.exit_code == SIGTERM);
    assert(!unlink(record) && !rmdir(root));
    cfg->config_path = NULL;
}
static void log_data(void *p, const char *stream, const char *data, size_t len) {
    (void)p;
    (void)data;
    if (!strcmp(stream, "stderr"))
        err_bytes += len;
}
int main(int argc, char **argv) {
    assert(argc == 2);
    /* 测试父进程接管自己的孤儿后代，仅用于测试资源回收。 */
    assert(!prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0));
    camctl_host_config cfg = CAMCTL_HOST_CONFIG_INIT;
    char buffer[1024];
    host_child c;
    host_child_init(&c, buffer, sizeof(buffer));
    cfg.camctl_path = "/definitely-missing-camctl";
    assert(host_child_spawn(&c, &cfg, HOST_RUN, NULL) == ENOENT);
    assert(c.pid == 0);
    cfg.camctl_path = argv[1];
    assert(host_child_spawn(&c, &cfg, HOST_SUBMIT, "/中文 path.json") == 0);
    for (int i = 0; i < 500 && !host_child_done(&c); i++) {
        host_child_collect(&c, log_data, NULL);
        struct timespec t = {0, 1000000};
        nanosleep(&t, NULL);
    }
    assert(host_child_done(&c));
    assert(!c.overflow);
    assert(c.exit_code == 0);
    assert(
        host_result_parse(HOST_SUBMIT, c.output, c.length, c.termination, c.exit_code).needs_run);
    assert(err_bytes == 1024 * 1024);
    /* 真正执行后退出 127，不能归入“确定未启动”。 */
    assert(host_child_spawn(&c, &cfg, HOST_RUN, "/exit127") == 0);
    for (int i = 0; i < 500 && !host_child_done(&c); i++) {
        host_child_collect(&c, log_data, NULL);
        struct timespec t = {0, 1000000};
        nanosleep(&t, NULL);
    }
    assert(host_child_done(&c));
    assert(c.exit_code == 127);
    assert(host_child_spawn(&c, &cfg, HOST_RUN, "/overflow") == 0);
    for (int i = 0; i < 500 && !host_child_done(&c); i++) {
        host_child_collect(&c, log_data, NULL);
        struct timespec t = {0, 1000000};
        nanosleep(&t, NULL);
    }
    assert(host_child_done(&c));
    assert(c.overflow);
    assert(c.length == sizeof(buffer));
    struct sigaction action = {0}, after;
    action.sa_handler = handle;
    sigemptyset(&action.sa_mask);
    assert(!sigaction(SIGCHLD, &action, NULL));
    leftover_contract(&cfg, "/leftover-success", false);
    leftover_contract(&cfg, "/leftover-error", false);
    leftover_contract(&cfg, "/leftover-thread", true);
    assert(!sigaction(SIGCHLD, NULL, &after) && after.sa_handler == handle);
    assert(!host_child_spawn(&c, &cfg, HOST_RUN, "/hold"));
    assert(!kill(c.pid, SIGKILL));
    int status;
    assert(waitpid(c.pid, &status, 0) == c.pid);
    host_child_collect(&c, NULL, NULL);
    assert(c.wait_fault && !host_child_done(&c));
    if (c.out_fd >= 0) close(c.out_fd);
    if (c.err_fd >= 0) close(c.err_fd);
    return 0;
}
