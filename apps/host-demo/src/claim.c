#include "claim.h"
#include <errno.h>
#include <stdlib.h>
#include <string.h>
typedef struct name_node {
    struct name_node *next;
    char name[];
} name_node;
void host_claim_files(const host_claim_ops *ops, void *context, size_t list_limit) {
    name_node *head = NULL, **tail = &head;
    size_t used = 0;
    bool sync_required = false;
    int e = ops->begin(context);
    if (e) {
        ops->diagnose(context, "directories", "", e);
        goto cleanup;
    }
    for (;;) {
        char name[256];
        int n = ops->next(context, name);
        if (!n)
            break;
        if (n < 0) {
            ops->diagnose(context, "enumerate", "", -n);
            goto cleanup;
        }
        size_t size = sizeof(name_node) + strlen(name) + 1;
        if (size > list_limit - used) {
            ops->diagnose(context, "list_limit", name, ENOBUFS);
            goto cleanup;
        }
        name_node *node = malloc(size);
        if (!node) {
            ops->diagnose(context, "list_allocate", name, ENOMEM);
            goto cleanup;
        }
        node->next = NULL;
        strcpy(node->name, name);
        *tail = node;
        tail = &node->next;
        used += size;
    }
    e = ops->finish_listing(context);
    if (e) {
        ops->diagnose(context, "enumerate_close", "", e);
        goto cleanup;
    }
    for (name_node *node = head; node; node = node->next) {
        e = ops->healthy(context);
        if (e) {
            ops->diagnose(context, "directories", node->name, e);
            break;
        }
        host_claim_move_result result = ops->move(context, node->name);
        e = result.error;
        if (result.kind == HOST_CLAIM_MOVED || result.kind == HOST_CLAIM_MOVE_UNKNOWN)
            sync_required = true;
        static const char *const events[] = {
            [HOST_CLAIM_SOURCE_CHECK_FAILED] = "source_check_failed",
            [HOST_CLAIM_SOURCE_NOT_REGULAR] = "source_not_regular",
            [HOST_CLAIM_MOVE_FAILED] = "move_failed",
            [HOST_CLAIM_MOVED] = "moved",
            [HOST_CLAIM_MOVE_UNKNOWN] = "move_unknown"};
        ops->diagnose(context, events[result.kind], node->name, e);
        if (e && (e = ops->healthy(context))) {
            ops->diagnose(context, "directories", node->name, e);
            break;
        }
    }
    if (sync_required) {
        host_claim_sync_result result = ops->sync(context);
        if (result.processing_error)
            ops->diagnose(context, "processing_directory_sync_unknown", "", result.processing_error);
        if (result.ready_error)
            ops->diagnose(context, "ready_directory_sync_unknown", "", result.ready_error);
    }
cleanup:
    while (head) {
        name_node *next = head->next;
        free(head);
        head = next;
    }
    ops->end(context);
}
