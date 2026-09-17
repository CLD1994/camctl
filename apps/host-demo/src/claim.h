#ifndef HOST_CLAIM_H
#define HOST_CLAIM_H
#include <stddef.h>
#include "logger.h"
/* 返回正 errno；next 返回 1=名称，0=完成，负 errno=读取失败。 */
typedef struct {
    int (*begin)(void *);
    int (*next)(void *, char name[256]);
    int (*finish_listing)(void *);
    int (*healthy)(void *);
    int (*move)(void *, const char *);
    int (*sync)(void *);
    void (*end)(void *);
    void (*diagnose)(void *, const char *, const char *, int);
} host_claim_ops;
void host_claim_files(const host_claim_ops *ops, void *context, size_t list_limit);
void host_claim_posix(const camctl_host_config *config, host_logger *logger);
#endif
