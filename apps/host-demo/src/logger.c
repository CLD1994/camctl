#include "logger.h"
#include "config.h"
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int file_failure(host_logger *l, host_log_file_operation operation, int e) {
    l->file_operation = operation;
    return e;
}
static int clean_archives(host_logger *l) {
    char path[CAMCTL_HOST_PATH_MAX + 32];
    for (uint32_t i = l->config->log_file_count; i < HOST_LOG_FILE_COUNT_MAX; i++) {
        snprintf(path, sizeof(path), "%s.%u", l->config->log_path, i);
        if (unlink(path) && errno != ENOENT) {
            l->failed_archive = i;
            return file_failure(l, HOST_LOG_FILE_CLEAN_ARCHIVES, errno);
        }
    }
    l->archives_checked = true;
    return 0;
}
static int open_log(host_logger *l) {
    int e;
    if (!l->archives_checked && (e = clean_archives(l)))
        return e;
    l->fd = open(l->config->log_path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC | O_NONBLOCK, 0600);
    if (l->fd < 0)
        return file_failure(l, HOST_LOG_FILE_OPEN, errno);
    /* 主程序可能主动关闭标准流，日志不能永久占用其 0/1/2 描述符。 */
    if (l->fd < 3) {
        int fd = fcntl(l->fd, F_DUPFD_CLOEXEC, 3);
        int e = errno;
        close(l->fd);
        l->fd = fd;
        if (fd < 0)
            return file_failure(l, HOST_LOG_FILE_DUPLICATE_FD, e);
    }
    struct stat st;
    if (fstat(l->fd, &st))
        return file_failure(l, HOST_LOG_FILE_STAT, errno);
    if (!S_ISREG(st.st_mode))
        return file_failure(l, HOST_LOG_FILE_TYPE, EINVAL);
    l->file_size = (size_t)st.st_size;
    return 0;
}
static int rotate(host_logger *l) {
    int fd = l->fd;
    l->fd = -1;
    if (close(fd))
        return file_failure(l, HOST_LOG_FILE_CLOSE, errno);
    char old[CAMCTL_HOST_PATH_MAX + 32], next[CAMCTL_HOST_PATH_MAX + 32];
    const char *path = l->config->log_path;
    for (uint32_t i = l->config->log_file_count; i > 1; i--) {
        snprintf(next, sizeof(next), "%s.%u", path, i - 1);
        if (i == l->config->log_file_count && unlink(next) && errno != ENOENT)
            return file_failure(l, HOST_LOG_FILE_REMOVE, errno);
        if (i == 2)
            snprintf(old, sizeof(old), "%s", path);
        else
            snprintf(old, sizeof(old), "%s.%u", path, i - 2);
        if (rename(old, next) && errno != ENOENT)
            return file_failure(l, HOST_LOG_FILE_RENAME, errno);
    }
    if (l->config->log_file_count == 1 && unlink(path) && errno != ENOENT)
        return file_failure(l, HOST_LOG_FILE_REMOVE, errno);
    return open_log(l);
}
static int write_record(host_logger *l, host_log_record *r) {
    int e;
    if (l->fd < 0 && (e = open_log(l)))
        return e;
    if (l->file_size > l->config->log_file_size - r->length && (e = rotate(l)))
        return e;
    size_t done = 0;
    while (done < r->length) {
        ssize_t n = write(l->fd, r->text + done, r->length - done);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return file_failure(l, HOST_LOG_FILE_WRITE, n < 0 ? errno : EIO);
        done += (size_t)n;
    }
    l->file_size += done;
    return 0;
}
static void *logger_main(void *arg) {
    host_logger *l = arg;
    pthread_mutex_lock(&l->mutex);
    for (;;) {
        while (!l->stop && !l->queue.head)
            pthread_cond_wait(&l->ready, &l->mutex);
        if (l->stop)
            break;
        host_log_record *r = host_log_queue_pop(&l->queue);
        pthread_mutex_unlock(&l->mutex);
        int e = write_record(l, r);
        free(r);
        pthread_mutex_lock(&l->mutex);
        if (e) {
            host_log_queue_disable(&l->queue, e);
            if (l->fd >= 0) {
                close(l->fd);
                l->fd = -1;
            }
        }
    }
    host_log_queue_disable(&l->queue, ECANCELED);
    pthread_mutex_unlock(&l->mutex);
    if (l->fd >= 0)
        close(l->fd);
    return NULL;
}
int host_logger_init(host_logger *l, const camctl_host_config *config) {
    memset(l, 0, sizeof(*l));
    l->config = config;
    l->fd = -1;
    host_log_queue_init(&l->queue, config->log_queue_capacity, config->log_record_capacity);
    int e = pthread_mutex_init(&l->mutex, NULL);
    if (e)
        return e;
    e = pthread_cond_init(&l->ready, NULL);
    if (e) {
        pthread_mutex_destroy(&l->mutex);
        return e;
    }
    e = pthread_create(&l->thread, NULL, logger_main, l);
    if (e) {
        pthread_cond_destroy(&l->ready);
        pthread_mutex_destroy(&l->mutex);
    }
    return e;
}
void host_logger_abort(host_logger *l) {
    pthread_mutex_lock(&l->mutex);
    l->stop = true;
    pthread_cond_signal(&l->ready);
    pthread_mutex_unlock(&l->mutex);
    pthread_join(l->thread, NULL);
    pthread_cond_destroy(&l->ready);
    pthread_mutex_destroy(&l->mutex);
}
void host_log(host_logger *l, const char *format, ...) {
    size_t cap = l->config->log_record_capacity;
    char *text = malloc(cap + 1);
    if (!text) {
        pthread_mutex_lock(&l->mutex);
        if (!l->queue.file_error && l->queue.dropped != UINT64_MAX)
            l->queue.dropped++;
        pthread_mutex_unlock(&l->mutex);
        return;
    }
    va_list args;
    va_start(args, format);
    int n = vsnprintf(text, cap + 1, format, args);
    va_end(args);
    if (n >= 0) {
        pthread_mutex_lock(&l->mutex);
        host_log_queue_push(&l->queue, text, (size_t)n > cap ? cap : (size_t)n);
        pthread_cond_signal(&l->ready);
        pthread_mutex_unlock(&l->mutex);
    }
    free(text);
}
