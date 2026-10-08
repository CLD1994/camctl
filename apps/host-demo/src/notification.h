#ifndef HOST_NOTIFICATION_H
#define HOST_NOTIFICATION_H
#include <stddef.h>
#include <stdbool.h>
#define HOST_NOTIFICATION_READ_BLOCK 4096
typedef struct {
    int position;
} host_notification;
typedef enum {
    HOST_NOTIFICATION_OK,
    HOST_NOTIFICATION_JSON,
    HOST_NOTIFICATION_STRUCTURE,
    HOST_NOTIFICATION_TYPE,
    HOST_NOTIFICATION_ID,
    HOST_NOTIFICATION_POSITION,
    HOST_NOTIFICATION_TOO_LONG,
    HOST_NOTIFICATION_FRAGMENT
} host_notification_error;
typedef struct {
    char *line;
    void *pool;
    size_t capacity, length, pool_size;
    bool discarding;
} host_notification_stream;
typedef bool (*host_notification_sink)(void *, host_notification);
typedef void (*host_notification_diagnostic)(void *, host_notification_error);
const char *host_notification_error_name(host_notification_error error);
size_t host_notification_buffer_bound(size_t line_capacity, size_t queue_capacity);
int host_notification_init(host_notification_stream *s, size_t capacity);
void host_notification_destroy(host_notification_stream *s);
void host_notification_reset(host_notification_stream *s);
host_notification_error host_notification_parse(host_notification_stream *s, const char *data,
                                                size_t length, host_notification *out);
size_t host_notification_feed(host_notification_stream *s, const char *data, size_t length,
                              host_notification_sink sink, host_notification_diagnostic diagnostic,
                              void *context);
void host_notification_eof(host_notification_stream *s, host_notification_diagnostic diagnostic,
                           void *context);
#endif
