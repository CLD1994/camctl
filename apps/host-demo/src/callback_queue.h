#ifndef HOST_CALLBACK_QUEUE_H
#define HOST_CALLBACK_QUEUE_H
#include "notification.h"
typedef struct {
    host_notification *items;
    size_t capacity, head, length;
} host_callback_queue;
bool host_callback_queue_push(host_callback_queue *q, host_notification v);
bool host_callback_queue_pop(host_callback_queue *q, host_notification *v);
#endif
