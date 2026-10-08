#ifndef HOST_CLAIM_H
#define HOST_CLAIM_H
#include <stddef.h>
#include "logger.h"
typedef enum {
    HOST_CLAIM_SOURCE_CHECK_FAILED, /* 尚未尝试移动。 */
    HOST_CLAIM_SOURCE_NOT_REGULAR,  /* 尚未尝试移动。 */
    HOST_CLAIM_MOVE_FAILED,
    HOST_CLAIM_MOVED,
    HOST_CLAIM_MOVE_UNKNOWN
} host_claim_move_kind;
typedef struct {
    host_claim_move_kind kind;
    int error;
} host_claim_move_result;
typedef struct {
    int ready_error, processing_error;
} host_claim_sync_result;
/* 普通操作返回正 errno；next 返回 1=名称，0=完成，负 errno=读取失败。 */
typedef struct {
    int (*begin)(void *);
    int (*next)(void *, char name[256]);
    int (*finish_listing)(void *);
    int (*healthy)(void *);
    host_claim_move_result (*move)(void *, const char *);
    host_claim_sync_result (*sync)(void *);
    void (*end)(void *);
    void (*diagnose)(void *, const char *, const char *, int);
} host_claim_ops;
void host_claim_files(const host_claim_ops *ops, void *context, size_t list_limit);
void host_claim_posix(const camctl_host_config *config, host_logger *logger);
#endif
