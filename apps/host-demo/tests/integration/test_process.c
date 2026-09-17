#define _GNU_SOURCE
#include "process.h"
#include <assert.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
static size_t err_bytes;
static void log_data(void *p, const char *stream, const char *data, size_t len) {
    (void)p;
    (void)data;
    if (!strcmp(stream, "stderr"))
        err_bytes += len;
}
int main(int argc, char **argv) {
    assert(argc == 2);
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
    return 0;
}
