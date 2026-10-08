#define _GNU_SOURCE
#include "logger.h"
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static pthread_mutex_t gate = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static unsigned written;
static char denied[256];
ssize_t __real_write(int, const void *, size_t);
ssize_t __wrap_write(int fd, const void *data, size_t length) {
    ssize_t n = __real_write(fd, data, length);
    if (n > 0) {
        pthread_mutex_lock(&gate);
        written++;
        pthread_cond_signal(&changed);
        pthread_mutex_unlock(&gate);
    }
    return n;
}
int __real_unlink(const char *);
int __wrap_unlink(const char *path) {
    if (*denied && !strcmp(path, denied)) {
        errno = EACCES;
        return -1;
    }
    return __real_unlink(path);
}
static void records(host_logger *logger, unsigned base, unsigned count) {
    char payload[111];
    memset(payload, 'x', sizeof(payload) - 1);
    payload[sizeof(payload) - 1] = 0;
    for (unsigned i = 0; i < count; i++) {
        pthread_mutex_lock(&gate);
        unsigned target = written + 1;
        host_log(logger, "record-%03u-%s", base + i, payload);
        struct timespec deadline;
        assert(!clock_gettime(CLOCK_REALTIME, &deadline));
        deadline.tv_sec += 5;
        while (written < target)
            assert(!pthread_cond_timedwait(&changed, &gate, &deadline));
        pthread_mutex_unlock(&gate);
    }
}
static void write_text(const char *path, const char *text) {
    FILE *file = fopen(path, "w");
    assert(file);
    assert(fputs(text, file) >= 0);
    assert(!fclose(file));
}
static void assert_record(const char *path, unsigned number) {
    char text[256], expected[32];
    FILE *file = fopen(path, "r");
    assert(file);
    assert(fgets(text, sizeof(text), file));
    assert(!fclose(file));
    snprintf(expected, sizeof(expected), "record-%03u-", number);
    assert(!strncmp(text, expected, strlen(expected)));
}
static void transition(uint32_t old_count, uint32_t new_count, bool deny, bool missing) {
    char root[] = "/tmp/camctl-log-XXXXXX", path[128], archive[256];
    assert(mkdtemp(root));
    snprintf(path, sizeof(path), "%s/module.log", root);
    camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
    config.log_path = path;
    config.log_file_count = old_count;
    config.log_file_size = config.log_record_capacity = 128;
    host_logger logger;
    assert(!host_logger_init(&logger, &config));
    records(&logger, 0, 9);
    host_logger_abort(&logger);
    const char *unrelated[] = {".notes", ".01", ".64"};
    for (size_t i = 0; i < sizeof(unrelated) / sizeof(unrelated[0]); i++) {
        snprintf(archive, sizeof(archive), "%s%s", path, unrelated[i]);
        write_text(archive, "unrelated");
    }
    if (missing) {
        snprintf(archive, sizeof(archive), "%s.2", path);
        assert(!unlink(archive));
    }
    if (deny)
        snprintf(denied, sizeof(denied), "%s.1", path);
    config.log_file_count = new_count;
    assert(!host_logger_init(&logger, &config));
    if (deny) {
        host_log(&logger, "record-denied");
        int error = 0;
        for (unsigned i = 0; i < 5000 && !error; i++) {
            pthread_mutex_lock(&logger.mutex);
            error = logger.queue.file_error;
            pthread_mutex_unlock(&logger.mutex);
            if (!error)
                usleep(1000);
        }
        assert(error == EACCES);
        assert(logger.file_operation == HOST_LOG_FILE_CLEAN_ARCHIVES);
        assert(logger.failed_archive == 1);
    } else {
        records(&logger, 100, 1);
        /* 一次轮换后，仍在保留范围内的旧记录继续按新旧顺序存在。 */
        if (!missing) {
            for (uint32_t i = 1; i < new_count && i <= old_count; i++) {
                snprintf(archive, sizeof(archive), "%s.%u", path, i);
                assert_record(archive, 9 - i);
            }
        }
        records(&logger, 101, 8);
    }
    host_logger_abort(&logger);
    *denied = 0;
    if (!deny) {
        assert_record(path, 108);
        for (uint32_t i = 1; i < 64; i++) {
            snprintf(archive, sizeof(archive), "%s.%u", path, i);
            if (i < new_count)
                assert_record(archive, 108 - i);
            else
                assert(access(archive, F_OK) < 0 && errno == ENOENT);
        }
    }
    for (size_t i = 0; i < sizeof(unrelated) / sizeof(unrelated[0]); i++) {
        snprintf(archive, sizeof(archive), "%s%s", path, unrelated[i]);
        char text[32];
        FILE *file = fopen(archive, "r");
        assert(file && fgets(text, sizeof(text), file));
        assert(!strcmp(text, "unrelated"));
        assert(!fclose(file));
        assert(!unlink(archive));
    }
    for (uint32_t i = 1; i < 64; i++) {
        snprintf(archive, sizeof(archive), "%s.%u", path, i);
        assert(!unlink(archive) || errno == ENOENT);
    }
    assert(!unlink(path));
    assert(!rmdir(root));
}
int main(void) {
    transition(3, 1, false, false);
    transition(5, 3, false, false);
    transition(3, 3, false, false);
    transition(3, 5, false, false);
    transition(3, 1, false, true);
    transition(3, 1, true, false);
    return 0;
}
