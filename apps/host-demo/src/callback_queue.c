#include "callback_queue.h"
bool host_callback_queue_push(host_callback_queue *q, host_notification v) {
    if (q->length == q->capacity)
        return false;
    q->items[(q->head + q->length) % q->capacity] = v;
    q->length++;
    return true;
}
bool host_callback_queue_pop(host_callback_queue *q, host_notification *v) {
    if (!q->length)
        return false;
    *v = q->items[q->head];
    q->head = (q->head + 1) % q->capacity;
    q->length--;
    return true;
}
