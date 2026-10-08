#define _GNU_SOURCE
#include "logger.h"
#include "process.h"
#include <string.h>
#include "process_group.h"
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc > 1) {
        assert(argc == 4 && !strcmp(argv[1], "run") && !strcmp(argv[2], "--host-notification-fd"));
        int fd = atoi(argv[3]);
        assert(fd >= 3 && fcntl(fd, F_GETFD) >= 0);
        const char line[] = "{\"type\":\"motor_control\",\"action_instance_id\":\"1\",\"params\":{"
                            "\"position\":-7}}\n";
        assert(write(fd, line, sizeof(line) - 1) == sizeof(line) - 1);
        const char result[] = "{\"kind\":\"succeeded\"}\n";
        assert(write(1, result, sizeof(result) - 1) == sizeof(result) - 1);
        return 0;
    }
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
    char executable[4096], output[1024], notifications[1024];
    ssize_t length = readlink("/proc/self/exe", executable, sizeof(executable) - 1);
    assert(length > 0);
    executable[length] = 0;
    config.camctl_path = executable;
    host_child invocation;
    host_child_init(&invocation, output, sizeof(output));
    invocation.notifications_enabled = true;
    assert(!host_child_spawn(&invocation, &config, HOST_RUN, NULL));
    size_t bytes = 0;
    for (int i = 0; i < 5000 && !host_child_done(&invocation); i++) {
        host_child_collect(&invocation, NULL, NULL);
        if (invocation.notification_fd >= 0) {
            ssize_t n = read(invocation.notification_fd, notifications + bytes,
                             sizeof(notifications) - bytes);
            if (n > 0)
                bytes += (size_t)n;
            else if (n == 0) {
                close(invocation.notification_fd);
                invocation.notification_fd = -1;
            } else
                assert(errno == EAGAIN || errno == EINTR);
        }
        struct timespec t = {0, 1000000};
        nanosleep(&t, NULL);
    }
    assert(host_child_done(&invocation));
    assert(bytes > 0 && notifications[bytes - 1] == '\n');
    assert(host_result_parse(HOST_RUN, output, invocation.length, invocation.termination,
                             invocation.exit_code)
               .kind == HOST_RESULT_SUCCESS);
    for (int fd = 0; fd < 3; fd++)
        assert(fcntl(fd, F_GETFD) == -1 && errno == EBADF);
    unlink(path);
    return 0;
}
