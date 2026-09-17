#ifndef HOST_LOG_QUEUE_H
#define HOST_LOG_QUEUE_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef struct host_log_record {
    struct host_log_record *next;
    size_t length;
    char text[];
} host_log_record;
typedef struct {
    host_log_record *head, *tail;
    size_t used, capacity, record_limit;
    uint64_t dropped;
    int file_error;
} host_log_queue;
void host_log_queue_init(host_log_queue *q, size_t capacity, size_t record_limit);
bool host_log_queue_push(host_log_queue *q, const char *text, size_t length);
host_log_record *host_log_queue_pop(host_log_queue *q);
void host_log_queue_disable(host_log_queue *q, int error);
#endif
