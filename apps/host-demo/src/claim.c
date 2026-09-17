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
    bool moved = false;
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
        e = ops->move(context, node->name);
        if (!e)
            moved = true;
        ops->diagnose(context,
                      !e ? "moved" : (e == EIO || e == EINTR ? "move_unknown" : "move_failed"),
                      node->name, e);
        if (e && (e = ops->healthy(context))) {
            ops->diagnose(context, "directories", node->name, e);
            break;
        }
    }
    if (moved && (e = ops->sync(context)))
        ops->diagnose(context, "directory_sync_unknown", "", e);
cleanup:
    while (head) {
        name_node *next = head->next;
        free(head);
        head = next;
    }
    ops->end(context);
}
