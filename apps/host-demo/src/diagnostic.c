#include "diagnostic.h"
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define HOST_DIAGNOSTIC_HEADER_CAPACITY (40 * 1024)

static const char *command_name(host_command command) {
    return command == HOST_RUN ? "run" : "submit";
}

static const char *classification_name(host_result_kind result) {
    switch (result) {
    case HOST_RESULT_SUCCESS:
        return "success";
    case HOST_RESULT_ERROR:
        return "error";
    case HOST_RESULT_ABNORMAL:
    default:
        return "abnormal";
    }
}

static const char *termination_name(const host_child *child) {
    if (!child->started)
        return "not_started";
    switch (child->termination) {
    case HOST_EXIT_NORMAL:
        return "normal";
    case HOST_EXIT_SIGNAL:
        return "signal";
    case HOST_EXIT_UNKNOWN:
    default:
        return "unknown";
    }
}

static int append_hex_path(char *buffer, size_t capacity, size_t *offset, const char *key,
                           const char *path) {
    static const char digits[] = "0123456789abcdef";
    int n = snprintf(buffer + *offset, capacity - *offset, "%s=", key);
    if (n < 0 || (size_t)n >= capacity - *offset)
        return EOVERFLOW;
    *offset += (size_t)n;
    size_t length = path ? strnlen(path, CAMCTL_HOST_PATH_MAX + 1) : 0;
    if (length > CAMCTL_HOST_PATH_MAX)
        return ENAMETOOLONG;
    for (size_t i = 0; i < length; i++) {
        unsigned char byte = (unsigned char)path[i];
        if (capacity - *offset < 3)
            return EOVERFLOW;
        buffer[(*offset)++] = digits[byte >> 4];
        buffer[(*offset)++] = digits[byte & 15];
    }
    if (capacity - *offset < 2)
        return EOVERFLOW;
    buffer[(*offset)++] = '\n';
    buffer[*offset] = 0;
    return 0;
}

int host_diagnostic_format_header(const host_child *child, const camctl_host_config *config,
                                  char *buffer, size_t capacity, size_t *written) {
    if (!child || !config || !buffer || !capacity || !written)
        return EINVAL;
    const char *exit_code = "not_applicable";
    const char *signal_number = "not_applicable";
    char value[32];
    if (child->started && child->termination == HOST_EXIT_NORMAL) {
        snprintf(value, sizeof(value), "%d", child->exit_code);
        exit_code = value;
    } else if (child->started && child->termination == HOST_EXIT_SIGNAL) {
        snprintf(value, sizeof(value), "%d", child->exit_code);
        signal_number = value;
    }
    const char *classification = child->started ? classification_name(child->result_kind)
                                                 : "not_started";
    int n = snprintf(buffer, capacity,
                     "CAMCTL-HOST-DIAGNOSTIC 1\n"
                     "call_id=%" PRIu64 "\n"
                     "attempt_id=%" PRIu64 "\n"
                     "command=%s\n"
                     "classification=%s\n"
                     "termination=%s\n"
                     "exit_code=%s\n"
                     "signal_number=%s\n"
                     "spawn_errno=%d\n"
                     "host_pid=%ld\n"
                     "child_pid=%ld\n"
                     "child_pgid=%ld\n"
                     "automatic=%d\n"
                     "retries_used=%u\n"
                     "working_directory_errno=%d\n"
                     "stdout_bytes_seen=%" PRIu64 "\n"
                     "stdout_bytes_saved=%zu\n"
                     "stdout_truncated=%d\n"
                     "stdout_read_error=%d\n"
                     "stderr_bytes_seen=%" PRIu64 "\n"
                     "stderr_bytes_saved=%zu\n"
                     "stderr_truncated=%d\n"
                     "stderr_read_error=%d\n",
                     child->call_id, child->attempt_id, command_name(child->command),
                     classification, termination_name(child), exit_code, signal_number,
                     child->spawn_errno, (long)getpid(), (long)child->completed_pid,
                     (long)child->completed_pgid, child->automatic, child->retries_used,
                     child->working_directory_errno,
                     child->output_bytes_seen, child->length, child->overflow, child->io_error,
                     child->stderr_bytes_seen, child->stderr_length, child->stderr_overflow,
                     child->stderr_io_error);
    if (n < 0 || (size_t)n >= capacity)
        return EOVERFLOW;
    size_t offset = (size_t)n;
    int e = append_hex_path(buffer, capacity, &offset, "camctl_path_hex", config->camctl_path);
    if (!e)
        e = append_hex_path(buffer, capacity, &offset, "config_path_hex", config->config_path);
    if (!e)
        e = append_hex_path(buffer, capacity, &offset, "plan_path_hex",
                            child->plan_path[0] ? child->plan_path : NULL);
    if (!e)
        e = append_hex_path(buffer, capacity, &offset, "working_directory_hex",
                            child->working_directory[0] ? child->working_directory : NULL);
    if (e)
        return e;
    n = snprintf(buffer + offset, capacity - offset, "\nstdout_begin\n");
    if (n < 0 || (size_t)n >= capacity - offset)
        return EOVERFLOW;
    offset += (size_t)n;
    *written = offset;
    return 0;
}

static int above_stdio(int fd) {
    if (fd < 0)
        return -1;
    if (fd >= 3)
        return fd;
    int duplicate = fcntl(fd, F_DUPFD_CLOEXEC, 3);
    int e = errno;
    close(fd);
    if (duplicate < 0)
        errno = e;
    return duplicate;
}

static int write_all(int fd, const void *data, size_t length) {
    const unsigned char *bytes = data;
    size_t written = 0;
    while (written < length) {
        ssize_t n = write(fd, bytes + written, length - written);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return n < 0 ? errno : EIO;
        written += (size_t)n;
    }
    return 0;
}

typedef struct {
    int error;
    const char *step;
    bool published;
    char file_name[256];
} publish_result;

static void fail_at(publish_result *result, const char *step, int error) {
    if (!result->error) {
        result->step = step;
        result->error = error;
    }
}

static publish_result publish_diagnostic(host_diagnostic_writer *writer, host_child *child) {
    publish_result result = {.step = "none"};
    int processing = -1, preparation = -1, file = -1;
    char preparation_name[256] = "";
    bool preparation_created = false;
    bool payload_published = false;
    char template[384];
    char header[HOST_DIAGNOSTIC_HEADER_CAPACITY];
    size_t header_length = 0;

    processing = open(writer->config->processing_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    processing = above_stdio(processing);
    if (processing < 0) {
        fail_at(&result, "open_processing", errno);
        goto cleanup;
    }
    int n = snprintf(template, sizeof(template),
                     "/proc/self/fd/%d/.camctl-host-diagnostic-prepare-%ld-%" PRIu64 "-XXXXXX",
                     processing, (long)getpid(), child->attempt_id);
    if (n < 0 || (size_t)n >= sizeof(template)) {
        fail_at(&result, "prepare_name", EOVERFLOW);
        goto cleanup;
    }
    if (!mkdtemp(template)) {
        fail_at(&result, "create_preparation", errno);
        goto cleanup;
    }
    preparation_created = true;
    const char *base = strrchr(template, '/');
    if (!base || strlen(base + 1) >= sizeof(preparation_name)) {
        fail_at(&result, "prepare_name", EOVERFLOW);
        goto cleanup;
    }
    strcpy(preparation_name, base + 1);
    preparation = openat(processing, preparation_name,
                         O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    preparation = above_stdio(preparation);
    if (preparation < 0) {
        fail_at(&result, "open_preparation", errno);
        goto cleanup;
    }
    const char *random_suffix = strrchr(preparation_name, '-') + 1;
    n = snprintf(result.file_name, sizeof(result.file_name),
                 "camctl-host-diagnostic-%ld-%" PRIu64 "-%s.log", (long)getpid(),
                 child->attempt_id, random_suffix);
    if (n < 0 || (size_t)n >= sizeof(result.file_name)) {
        fail_at(&result, "final_name", EOVERFLOW);
        goto cleanup;
    }
    file = openat(preparation, "record.tmp",
                  O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    file = above_stdio(file);
    if (file < 0) {
        fail_at(&result, "create_record", errno);
        goto cleanup;
    }
    int e = host_diagnostic_format_header(child, writer->config, header, sizeof(header),
                                          &header_length);
    if (e) {
        fail_at(&result, "format_header", e);
        goto cleanup;
    }
    e = write_all(file, header, header_length);
    if (!e)
        e = write_all(file, child->output, child->length);
    static const char stderr_marker[] = "\nstderr_begin\n";
    if (!e)
        e = write_all(file, stderr_marker, sizeof(stderr_marker) - 1);
    if (!e)
        e = write_all(file, child->stderr_output, child->stderr_length);
    if (!e && fsync(file))
        e = errno;
    if (e) {
        fail_at(&result, "write_record", e);
        goto cleanup;
    }
    if (close(file)) {
        file = -1;
        fail_at(&result, "close_record", errno);
        goto cleanup;
    }
    file = -1;
    if (fsync(preparation)) {
        fail_at(&result, "sync_preparation", errno);
        goto cleanup;
    }
    int lock_error = pthread_mutex_lock(writer->publish_mutex);
    if (lock_error) {
        fail_at(&result, "publish_lock", lock_error);
        goto cleanup;
    }
    if (host_diagnostic_rename_noreplace(preparation, "record.tmp", processing,
                                         result.file_name)) {
        e = errno;
        pthread_mutex_unlock(writer->publish_mutex);
        fail_at(&result, "publish_rename", e);
        goto cleanup;
    }
    result.published = payload_published = true;
    if (fsync(processing))
        fail_at(&result, "sync_processing", errno);
    if (fsync(preparation))
        fail_at(&result, "sync_preparation_after_publish", errno);
    int unlock_error = pthread_mutex_unlock(writer->publish_mutex);
    if (unlock_error)
        fail_at(&result, "publish_unlock", unlock_error);

cleanup:
    if (file >= 0 && close(file))
        fail_at(&result, "close_record", errno);
    if (preparation >= 0) {
        if (!payload_published && unlinkat(preparation, "record.tmp", 0) && errno != ENOENT)
            fail_at(&result, "remove_partial_record", errno);
        if (close(preparation))
            fail_at(&result, "close_preparation", errno);
    }
    if (preparation_created && processing >= 0 &&
        unlinkat(processing, preparation_name, AT_REMOVEDIR))
        fail_at(&result, "remove_preparation", errno);
    if (processing >= 0 && close(processing))
        fail_at(&result, "close_processing", errno);
    return result;
}

static void wake_worker(host_diagnostic_writer *writer) {
    ssize_t n = write(writer->wake_fd, "d", 1);
    if (n < 0 && errno != EAGAIN && errno != EINTR)
        host_log(writer->logger, "diagnostic_worker_wake_failed errno=%d", errno);
}

static void enqueue_if_room(host_diagnostic_writer *writer, host_child *child);

static void *diagnostic_main(void *context) {
    host_diagnostic_writer *writer = context;
    pthread_mutex_lock(&writer->mutex);
    for (;;) {
        while (!writer->stop && !writer->count)
            pthread_cond_wait(&writer->ready, &writer->mutex);
        if (writer->stop)
            break;
        host_child *child = writer->items[writer->head];
        writer->items[writer->head] = NULL;
        writer->head = (writer->head + 1) % HOST_DIAGNOSTIC_QUEUE_CAPACITY;
        writer->count--;
        child->diagnostic_state = HOST_DIAGNOSTIC_WRITING;
        pthread_mutex_unlock(&writer->mutex);

        publish_result result = publish_diagnostic(writer, child);
        if (result.error) {
            host_log(writer->logger,
                     "call=%" PRIu64 " attempt=%" PRIu64
                     " diagnostic_write_failed step=%s errno=%d published=%d file=%s detail=%s",
                     child->call_id, child->attempt_id, result.step, result.error,
                     result.published, result.file_name, strerror(result.error));
        } else {
            host_log(writer->logger,
                     "call=%" PRIu64 " attempt=%" PRIu64 " diagnostic_published file=%s",
                     child->call_id, child->attempt_id, result.file_name);
        }

        pthread_mutex_lock(&writer->mutex);
        child->diagnostic_state = HOST_DIAGNOSTIC_IDLE;
        if (writer->outstanding)
            writer->outstanding--;
        for (size_t i = 0; i < 2; i++)
            enqueue_if_room(writer, &writer->children[i]);
        pthread_cond_broadcast(&writer->ready);
        pthread_mutex_unlock(&writer->mutex);
        wake_worker(writer);
        pthread_mutex_lock(&writer->mutex);
    }
    pthread_mutex_unlock(&writer->mutex);
    return NULL;
}

static void enqueue_if_room(host_diagnostic_writer *writer, host_child *child) {
    if (child->diagnostic_state != HOST_DIAGNOSTIC_PENDING ||
        writer->outstanding >= HOST_DIAGNOSTIC_QUEUE_CAPACITY)
        return;
    size_t tail = (writer->head + writer->count) % HOST_DIAGNOSTIC_QUEUE_CAPACITY;
    writer->items[tail] = child;
    writer->count++;
    writer->outstanding++;
    child->diagnostic_state = HOST_DIAGNOSTIC_QUEUED;
    pthread_cond_signal(&writer->ready);
}

int host_diagnostic_init(host_diagnostic_writer *writer, const camctl_host_config *config,
                        host_logger *logger, pthread_mutex_t *publish_mutex, int wake_fd,
                        host_child children[2]) {
    memset(writer, 0, sizeof(*writer));
    writer->config = config;
    writer->logger = logger;
    writer->publish_mutex = publish_mutex;
    writer->wake_fd = wake_fd;
    writer->children = children;
    int e = pthread_mutex_init(&writer->mutex, NULL);
    if (e)
        return e;
    e = pthread_cond_init(&writer->ready, NULL);
    if (e) {
        pthread_mutex_destroy(&writer->mutex);
        return e;
    }
    e = pthread_create(&writer->thread, NULL, diagnostic_main, writer);
    if (e) {
        pthread_cond_destroy(&writer->ready);
        pthread_mutex_destroy(&writer->mutex);
    }
    return e;
}

void host_diagnostic_abort(host_diagnostic_writer *writer) {
    pthread_mutex_lock(&writer->mutex);
    writer->stop = true;
    pthread_cond_signal(&writer->ready);
    pthread_mutex_unlock(&writer->mutex);
    pthread_join(writer->thread, NULL);
    pthread_cond_destroy(&writer->ready);
    pthread_mutex_destroy(&writer->mutex);
}

void host_diagnostic_request(host_diagnostic_writer *writer, host_child *child) {
    pthread_mutex_lock(&writer->mutex);
    child->diagnostic_state = HOST_DIAGNOSTIC_PENDING;
    enqueue_if_room(writer, child);
    pthread_mutex_unlock(&writer->mutex);
}

void host_diagnostic_dispatch_pending(host_diagnostic_writer *writer, host_child children[2]) {
    pthread_mutex_lock(&writer->mutex);
    for (size_t i = 0; i < 2; i++)
        enqueue_if_room(writer, &children[i]);
    pthread_mutex_unlock(&writer->mutex);
}

bool host_diagnostic_busy(host_diagnostic_writer *writer, const host_child *child) {
    pthread_mutex_lock(&writer->mutex);
    bool busy = child->diagnostic_state != HOST_DIAGNOSTIC_IDLE;
    pthread_mutex_unlock(&writer->mutex);
    return busy;
}
