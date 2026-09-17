#include "config.h"
#include "log_queue.h"
#include <errno.h>
#include <string.h>
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
        !c->log_file_count || c->log_file_count > 64 || c->retry_delay_ms > 86400000)
        return EINVAL;
    return 0;
}
