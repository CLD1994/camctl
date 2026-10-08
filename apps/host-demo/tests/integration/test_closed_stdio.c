#define _GNU_SOURCE
#include "logger.h"
#include "process_group.h"
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
int main(void) {
    char path[] = "/tmp/camctl-host-log-XXXXXX";
    int tmp = mkstemp(path);
    assert(tmp >= 0);
    close(tmp);
    camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
    config.log_path = path;
    close(0);
    close(1);
    close(2);
    host_logger logger;
    assert(host_logger_init(&logger, &config) == 0);
    host_log(&logger, "closed stdio test");
    struct stat st;
    for (int i = 0; i < 5000; i++) {
        assert(stat(path, &st) == 0);
        if (st.st_size)
            break;
        struct timespec t = {0, 1000000};
        nanosleep(&t, NULL);
    }
    assert(st.st_size > 0);
    for (int fd = 0; fd < 3; fd++) {
        assert(fcntl(fd, F_GETFD) == -1);
        assert(errno == EBADF);
    }
    host_logger_abort(&logger);
    pid_t child = fork();
    assert(child >= 0);
    if (!child) {
        assert(!setpgid(0, 0));
        _exit(0);
    }
    siginfo_t info = {0};
    assert(!waitid(P_PID, child, &info, WEXITED | WNOWAIT));
    host_group_scan scan;
    host_group_scan_init(&scan);
    assert(host_group_scan_batch(&scan, child, 1).status == HOST_GROUP_PENDING);
    for (int fd = 0; fd < 3; fd++) {
        assert(fcntl(fd, F_GETFD) == -1 && errno == EBADF);
    }
    host_group_result result = {HOST_GROUP_PENDING, 0, 0, 0};
    for (int i = 0; i < 5000 && result.status == HOST_GROUP_PENDING; i++)
        result = host_group_scan_batch(&scan, child, 64);
    assert(result.status == HOST_GROUP_COMPLETE);
    int status;
    assert(waitpid(child, &status, 0) == child);
    unlink(path);
    return 0;
}
