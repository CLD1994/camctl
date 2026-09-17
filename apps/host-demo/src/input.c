#include "input.h"
#include "config.h"
#include <errno.h>
#include <string.h>
int host_inputs_push(host_inputs *q, const char *path, uint64_t id) {
    int e = host_path_validate(path);
    if (e)
        return e;
    if (q->count == q->capacity)
        return EAGAIN;
    host_input *item = &q->items[(q->head + q->count) % q->capacity];
    strcpy(item->path, path);
    item->id = id;
    q->count++;
    return 0;
}
host_input *host_inputs_front(host_inputs *q) { return q->count ? &q->items[q->head] : NULL; }
void host_inputs_pop(host_inputs *q) {
    if (q->count) {
        q->head = (q->head + 1) % q->capacity;
        q->count--;
    }
}
