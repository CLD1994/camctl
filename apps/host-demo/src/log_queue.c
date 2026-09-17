#include "log_queue.h"
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <inttypes.h>
void host_log_queue_init(host_log_queue *q, size_t capacity, size_t limit) {
    memset(q, 0, sizeof(*q));
    q->capacity = capacity;
    q->record_limit = limit;
}
static bool append(host_log_queue *q, const char *text, size_t length) {
    static const char marker[] = " [truncated]\n";
    size_t n = length < q->record_limit ? length + 1 : q->record_limit;
    size_t cost = sizeof(host_log_record) + n + 1;
    if (q->file_error || cost > q->capacity - q->used)
        return false;
    host_log_record *r = malloc(cost);
    if (!r)
        return false;
    r->next = NULL;
    r->length = n;
    if (length >= q->record_limit) {
        size_t prefix = n - (sizeof(marker) - 1);
        memcpy(r->text, text, prefix);
        memcpy(r->text + prefix, marker, sizeof(marker) - 1);
    } else {
        memcpy(r->text, text, length);
        r->text[length] = '\n';
    }
    r->text[n] = 0;
    if (q->tail)
        q->tail->next = r;
    else
        q->head = r;
    q->tail = r;
    q->used += cost;
    return true;
}
static void recover(host_log_queue *q) {
    if (!q->dropped || q->file_error)
        return;
    char text[96];
    int n = snprintf(text, sizeof(text), "log_queue dropped=%" PRIu64, q->dropped);
    if (n > 0 && append(q, text, (size_t)n))
        q->dropped = 0;
}
bool host_log_queue_push(host_log_queue *q, const char *text, size_t length) {
    if (q->file_error)
        return false;
    recover(q);
    if (append(q, text, length))
        return true;
    if (q->dropped != UINT64_MAX)
        q->dropped++;
    return false;
}
host_log_record *host_log_queue_pop(host_log_queue *q) {
    host_log_record *r = q->head;
    if (!r)
        return NULL;
    q->head = r->next;
    if (!q->head)
        q->tail = NULL;
    q->used -= sizeof(*r) + r->length + 1;
    recover(q);
    return r;
}
void host_log_queue_disable(host_log_queue *q, int e) {
    if (!q->file_error)
        q->file_error = e;
    host_log_record *r;
    while ((r = host_log_queue_pop(q)))
        free(r);
}
