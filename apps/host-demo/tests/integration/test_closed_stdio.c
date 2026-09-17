#define _GNU_SOURCE
#include "logger.h"
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <sys/stat.h>
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
    unlink(path);
    return 0;
}
