#ifndef HOST_DIAGNOSTIC_H
#define HOST_DIAGNOSTIC_H

#include "logger.h"
#include "process.h"
#include <pthread.h>
#include <stddef.h>

#define HOST_DIAGNOSTIC_QUEUE_CAPACITY 2

typedef struct {
    pthread_mutex_t mutex;
    pthread_cond_t ready;
    pthread_t thread;
    host_child *items[HOST_DIAGNOSTIC_QUEUE_CAPACITY];
    size_t head, count, outstanding;
    host_child *children;
    bool stop;
    const camctl_host_config *config;
    host_logger *logger;
    pthread_mutex_t *publish_mutex;
    int wake_fd;
} host_diagnostic_writer;

int host_diagnostic_format_header(const host_child *child, const camctl_host_config *config,
                                  char *buffer, size_t capacity, size_t *written);
int host_diagnostic_rename_noreplace(int olddirfd, const char *oldpath, int newdirfd,
                                     const char *newpath);
int host_diagnostic_init(host_diagnostic_writer *writer, const camctl_host_config *config,
                        host_logger *logger, pthread_mutex_t *publish_mutex, int wake_fd,
                        host_child children[2]);
void host_diagnostic_abort(host_diagnostic_writer *writer);
void host_diagnostic_request(host_diagnostic_writer *writer, host_child *child);
void host_diagnostic_dispatch_pending(host_diagnostic_writer *writer, host_child children[2]);
bool host_diagnostic_busy(host_diagnostic_writer *writer, const host_child *child);

#endif
