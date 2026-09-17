#define _GNU_SOURCE
#include "claim.h"
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
static int mode, moves;
static char removed[256], ready[4096], old_ready[4096];
int __real_renameat(int, const char *, int, const char *);
int __wrap_renameat(int from, const char *source, int to, const char *target) {
    moves++;
    if (moves == 1 && mode == 1) {
        /* 明确在完整遍历之后、首项移动之前发布新文件并撤下当前文件。 */
        int fd = openat(from, "later", O_WRONLY | O_CREAT | O_EXCL, 0600);
        assert(fd >= 0);
        assert(write(fd, "new", 3) == 3);
        close(fd);
        assert(unlinkat(from, source, 0) == 0);
        strcpy(removed, source);
    }
    int result = __real_renameat(from, source, to, target);
    if (moves == 1 && mode == 2) {
        assert(result == 0);
        errno = EIO;
        return -1;
    }
    if (moves == 1 && mode == 3) {
        assert(result == 0);
        assert(rename(ready, old_ready) == 0);
    }
    return result;
}
static int exists(const char *directory, const char *name) {
    char path[8192];
    snprintf(path, sizeof(path), "%s/%s", directory, name);
    return access(path, F_OK) == 0;
}
int main(int argc, char **argv) {
    assert(argc == 2);
    mode = atoi(argv[1]);
    char root[] = "/tmp/camctl-claim-XXXXXX";
    assert(mkdtemp(root));
    char processing[4096], log[4096];
    snprintf(ready, sizeof(ready), "%s/ready", root);
    snprintf(old_ready, sizeof(old_ready), "%s/old-ready", root);
    snprintf(processing, sizeof(processing), "%s/processing", root);
    snprintf(log, sizeof(log), "%s/log", root);
    assert(mkdir(ready, 0700) == 0);
    assert(mkdir(processing, 0700) == 0);
    for (int i = 0; i < 3; i++) {
        char path[8192];
        snprintf(path, sizeof(path), "%s/%c", ready, 'a' + i);
        int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
        assert(fd >= 0);
        close(fd);
    }
    camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
    config.ready_path = ready;
    config.processing_path = processing;
    config.log_path = log;
    host_logger logger;
    assert(host_logger_init(&logger, &config) == 0);
    host_claim_posix(&config, &logger);
    int delivered = 0;
    for (int i = 0; i < 3; i++) {
        char name[2] = {(char)('a' + i), 0};
        delivered += exists(processing, name);
    }
    if (mode == 1) {
        assert(moves == 3);
        assert(delivered == 2);
        assert(exists(ready, "later"));
        assert(!exists(processing, removed));
        assert(!exists(processing, "later"));
    } else if (mode == 2) {
        assert(moves == 3);
        assert(delivered == 3);
    } else {
        assert(moves == 1);
        assert(delivered == 1);
        assert(access(ready, F_OK) < 0);
    }
    host_logger_abort(&logger);
    const char *directories[] = {ready, processing, old_ready};
    for (size_t i = 0; i < 3; i++) {
        for (int n = 0; n < 4; n++) {
            char path[8192], name[2] = {(char)('a' + n), 0};
            snprintf(path, sizeof(path), "%s/%s", directories[i], n == 3 ? "later" : name);
            unlink(path);
        }
        rmdir(directories[i]);
    }
    unlink(log);
    assert(rmdir(root) == 0);
    return 0;
}
