#ifndef HOST_INPUT_H
#define HOST_INPUT_H
#include "camctl_host.h"
typedef struct {
    char path[CAMCTL_HOST_PATH_MAX + 1];
    uint64_t id;
} host_input;
typedef struct {
    host_input *items;
    size_t head, count, capacity;
} host_inputs;
/* 调用方持锁；队首在提交结束前一直占用容量。 */
int host_inputs_push(host_inputs *q, const char *path, uint64_t id);
host_input *host_inputs_front(host_inputs *q);
void host_inputs_pop(host_inputs *q);
#endif
