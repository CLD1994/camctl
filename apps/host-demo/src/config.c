#include "config.h"
#include "log_queue.h"
#include "notification.h"
#include <errno.h>
#include <string.h>
int camctl_host_config_set_home_paths(camctl_host_config *config, camctl_host_paths *paths,
                                     const char *home) {
    if (!config || !paths) {
        errno = EINVAL;
        return -1;
    }
    struct {
        const char **target;
        char *buffer;
        const char *suffix;
    } defaults[] = {
        {&config->camctl_path, paths->camctl_path, "/.camctl/venv/bin/camctl"},
        {&config->ready_path, paths->ready_path, "/.camctl/ready"},
        {&config->processing_path, paths->processing_path, "/.camctl/processing"},
        {&config->log_path, paths->log_path, "/.camctl/host.log"},
    };
    if (config->camctl_path && config->ready_path && config->processing_path && config->log_path)
        return 0;
    if (!home || home[0] != '/') {
        errno = EINVAL;
        return -1;
    }
    size_t length = strnlen(home, CAMCTL_HOST_PATH_MAX + 1);
    if (length > CAMCTL_HOST_PATH_MAX) {
        errno = ENAMETOOLONG;
        return -1;
    }
    while (length && home[length - 1] == '/')
        --length;
    for (size_t i = 0; i < sizeof(defaults) / sizeof(defaults[0]); ++i) {
        if (!*defaults[i].target && length + strlen(defaults[i].suffix) > CAMCTL_HOST_PATH_MAX) {
            errno = ENAMETOOLONG;
            return -1;
        }
    }
    /* 先保存 home，允许调用方使用同一缓冲区中的旧路径作为输入。 */
    char base[CAMCTL_HOST_PATH_MAX + 1];
    memcpy(base, home, length);
    for (size_t i = 0; i < sizeof(defaults) / sizeof(defaults[0]); ++i) {
        if (*defaults[i].target)
            continue;
        memcpy(defaults[i].buffer, base, length);
        strcpy(defaults[i].buffer + length, defaults[i].suffix);
        *defaults[i].target = defaults[i].buffer;
    }
    return 0;
}

int host_path_validate(const char *p) {
    if (!p || p[0] != '/')
        return EINVAL;
    return strnlen(p, CAMCTL_HOST_PATH_MAX + 1) > CAMCTL_HOST_PATH_MAX ? ENAMETOOLONG : 0;
}
int host_config_validate(const camctl_host_config *c, const char *p) {
    if (!c)
        return EINVAL;
    const char *paths[] = {c->camctl_path, c->ready_path,  c->processing_path,
                           c->log_path,    c->config_path, p};
    for (size_t i = 0; i < sizeof(paths) / sizeof(paths[0]); i++) {
        if (i >= 4 && !paths[i])
            continue;
        int e = host_path_validate(paths[i]);
        if (e)
            return e;
    }
    if (!c->plan_capacity || c->plan_capacity > 4096 || c->stdout_capacity < 64 ||
        c->stdout_capacity > 16 * 1024 * 1024 || c->log_queue_capacity < 256 ||
        c->log_queue_capacity > 16 * 1024 * 1024 || c->log_record_capacity < 128 ||
        c->log_record_capacity > 64 * 1024 ||
        c->log_record_capacity + sizeof(host_log_record) + 1 > c->log_queue_capacity ||
        c->log_file_size < c->log_record_capacity || c->log_file_size > 1024u * 1024u * 1024u ||
        !c->log_file_count || c->log_file_count > HOST_LOG_FILE_COUNT_MAX ||
        c->retry_delay_ms > 86400000 || !c->notification_line_capacity ||
        c->notification_line_capacity > CAMCTL_HOST_NOTIFICATION_LINE_MAX ||
        !c->notification_queue_capacity ||
        c->notification_queue_capacity > CAMCTL_HOST_NOTIFICATION_QUEUE_MAX ||
        !host_notification_buffer_bound(c->notification_line_capacity,
                                        c->notification_queue_capacity))
        return EINVAL;
    return 0;
}
