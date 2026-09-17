#ifndef HOST_LOGGER_H
#define HOST_LOGGER_H
#include "log_queue.h"
#include "camctl_host.h"
#include <pthread.h>
typedef struct {
    pthread_mutex_t mutex;
    pthread_cond_t ready;
    pthread_t thread;
    host_log_queue queue;
    const camctl_host_config *config;
    bool stop;
    int fd;
    size_t file_size;
} host_logger;
int host_logger_init(host_logger *l, const camctl_host_config *config);
void host_logger_abort(host_logger *l);
void host_log(host_logger *l, const char *format, ...) __attribute__((format(printf, 2, 3)));
#endif
