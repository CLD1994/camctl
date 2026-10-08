#include "notification.h"
#include "camctl_host.h"
#include "yyjson.h"
#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
_Static_assert(INT_MIN == -2147483647 - 1 && INT_MAX == 2147483647, "通知要求32位int");
const char *host_notification_error_name(host_notification_error error) {
    switch (error) {
    case HOST_NOTIFICATION_OK:
        return "ok";
    case HOST_NOTIFICATION_JSON:
        return "json_invalid";
    case HOST_NOTIFICATION_STRUCTURE:
        return "structure_invalid";
    case HOST_NOTIFICATION_TYPE:
        return "type_unsupported";
    case HOST_NOTIFICATION_ID:
        return "action_id_invalid";
    case HOST_NOTIFICATION_POSITION:
        return "position_invalid";
    case HOST_NOTIFICATION_TOO_LONG:
        return "line_too_long";
    case HOST_NOTIFICATION_FRAGMENT:
        return "eof_fragment";
    }
    return "unknown";
}
size_t host_notification_buffer_bound(size_t line, size_t queue) {
    if (!line || line > CAMCTL_HOST_NOTIFICATION_LINE_MAX || !queue ||
        queue > CAMCTL_HOST_NOTIFICATION_QUEUE_MAX)
        return 0;
    size_t pool = yyjson_read_max_memory_usage(line, YYJSON_READ_NUMBER_AS_RAW);
    if (!pool || pool > SIZE_MAX - line)
        return 0;
    size_t bytes = pool + line;
    if (bytes > SIZE_MAX - HOST_NOTIFICATION_READ_BLOCK)
        return 0;
    bytes += HOST_NOTIFICATION_READ_BLOCK;
    if (queue > SIZE_MAX / sizeof(host_notification) - 1)
        return 0;
    size_t records = (queue + 1) * sizeof(host_notification);
    if (bytes > SIZE_MAX - records)
        return 0;
    return bytes + records;
}
int host_notification_init(host_notification_stream *s, size_t cap) {
    memset(s, 0, sizeof(*s));
    if (!cap || cap > CAMCTL_HOST_NOTIFICATION_LINE_MAX)
        return EINVAL;
    s->pool_size = yyjson_read_max_memory_usage(cap, YYJSON_READ_NUMBER_AS_RAW);
    if (!s->pool_size)
        return EOVERFLOW;
    s->line = malloc(cap);
    s->pool = malloc(s->pool_size);
    if (!s->line || !s->pool) {
        host_notification_destroy(s);
        return ENOMEM;
    }
    s->capacity = cap;
    return 0;
}
void host_notification_destroy(host_notification_stream *s) {
    free(s->line);
    free(s->pool);
    memset(s, 0, sizeof(*s));
}
void host_notification_reset(host_notification_stream *s) {
    s->length = 0;
    s->discarding = false;
}
static bool object_fields(yyjson_val *obj, const char *const *names, size_t count,
                          yyjson_val **values) {
    if (!yyjson_is_obj(obj) || yyjson_obj_size(obj) != count)
        return false;
    size_t idx, max;
    yyjson_val *key, *val;
    yyjson_obj_foreach(obj, idx, max, key, val) {
        size_t i;
        for (i = 0; i < count; i++)
            if (yyjson_equals_str(key, names[i]))
                break;
        if (i == count || values[i])
            return false;
        values[i] = val;
    }
    return true;
}
static bool entity_id(yyjson_val *v) {
    if (!yyjson_is_str(v))
        return false;
    const char *p = yyjson_get_str(v);
    size_t n = yyjson_get_len(v);
    if (!n || n > 19 || p[0] < '1' || p[0] > '9')
        return false;
    for (size_t i = 1; i < n; i++)
        if (p[i] < '0' || p[i] > '9')
            return false;
    return n < 19 || memcmp(p, "9223372036854775807", 19) <= 0;
}
/* yyjson负责数字语法；此适配只判定十进制数学值是否为范围内整数。 */
static bool position(yyjson_val *v, int *out) {
    if (!yyjson_is_raw(v))
        return false;
    const char *p = yyjson_get_raw(v);
    size_t n = yyjson_get_len(v), i = 0;
    bool negative = p[0] == '-';
    if (negative)
        i++;
    size_t first = SIZE_MAX, last = 0, digits = 0, fraction = 0;
    bool decimal = false;
    for (; i < n && p[i] != 'e' && p[i] != 'E'; i++) {
        if (p[i] == '.') {
            decimal = true;
            continue;
        }
        if (decimal)
            fraction++;
        if (p[i] != '0') {
            if (first == SIZE_MAX)
                first = digits;
            last = digits;
        }
        digits++;
    }
    int exponent = 0;
    if (i < n) {
        i++;
        bool minus = false;
        if (i < n && (p[i] == '+' || p[i] == '-')) {
            minus = p[i] == '-';
            i++;
        }
        /* 行内最多65536字节。超过该界限加10的指数无法被小数位或尾零
         * 抵消到int范围；零系数仍在下面独立处理。饱和只合并相同判定分区。 */
        const int exponent_limit = CAMCTL_HOST_NOTIFICATION_LINE_MAX + 11;
        for (; i < n; i++) {
            int digit = p[i] - '0';
            exponent =
                exponent > (exponent_limit - digit) / 10 ? exponent_limit : exponent * 10 + digit;
        }
        if (minus)
            exponent = -exponent;
    }
    if (first == SIZE_MAX) {
        *out = 0;
        return true;
    }
    int64_t scale = (int64_t)exponent - (int64_t)fraction;
    size_t trailing = digits - 1 - last;
    if (scale < 0 && (uint64_t)(-scale) > trailing)
        return false;
    int64_t significant = (int64_t)(digits - first) + scale;
    if (significant < 1 || significant > 10)
        return false;
    uint64_t value = 0;
    size_t ordinal = 0;
    size_t keep = scale < 0 ? digits - (size_t)(-scale) : digits;
    for (i = negative ? 1 : 0; i < n && p[i] != 'e' && p[i] != 'E'; i++) {
        if (p[i] == '.')
            continue;
        if (ordinal >= first && ordinal < keep)
            value = value * 10 + (unsigned)(p[i] - '0');
        ordinal++;
    }
    for (int64_t j = 0; j < scale; j++)
        value *= 10;
    uint64_t limit = negative ? 2147483648ULL : 2147483647ULL;
    if (value > limit)
        return false;
    *out = negative ? (value == 2147483648ULL ? INT_MIN : -(int)value) : (int)value;
    return true;
}
host_notification_error host_notification_parse(host_notification_stream *s, const char *data,
                                                size_t len, host_notification *out) {
    if (!len || len > s->capacity || data[len - 1] != '\n' || memchr(data, '\n', len - 1) ||
        memchr(data, 0, len))
        return HOST_NOTIFICATION_JSON;
    yyjson_alc alc;
    if (!yyjson_alc_pool_init(&alc, s->pool, s->pool_size))
        return HOST_NOTIFICATION_JSON;
    yyjson_doc *doc = yyjson_read_opts((char *)data, len, YYJSON_READ_NUMBER_AS_RAW, &alc, NULL);
    if (!doc)
        return HOST_NOTIFICATION_JSON;
    const char *names[] = {"type", "action_instance_id", "params"};
    yyjson_val *v[3] = {0};
    host_notification_error error = HOST_NOTIFICATION_STRUCTURE;
    if (!object_fields(yyjson_doc_get_root(doc), names, 3, v))
        goto done;
    error = HOST_NOTIFICATION_TYPE;
    if (!yyjson_equals_str(v[0], "motor_control"))
        goto done;
    error = HOST_NOTIFICATION_ID;
    if (!entity_id(v[1]))
        goto done;
    const char *param[] = {"position"};
    yyjson_val *pv[1] = {0};
    error = HOST_NOTIFICATION_POSITION;
    if (!object_fields(v[2], param, 1, pv) || !position(pv[0], &out->position))
        goto done;
    error = HOST_NOTIFICATION_OK;
done:
    yyjson_doc_free(doc);
    return error;
}
size_t host_notification_feed(host_notification_stream *s, const char *data, size_t n,
                              host_notification_sink sink, host_notification_diagnostic diagnostic,
                              void *ctx) {
    for (size_t i = 0; i < n; i++) {
        char c = data[i];
        if (s->discarding) {
            if (c == '\n')
                host_notification_reset(s);
            continue;
        }
        if (c == '\n') {
            s->line[s->length] = '\n';
            host_notification value;
            host_notification_error error =
                host_notification_parse(s, s->line, s->length + 1, &value);
            if (error == HOST_NOTIFICATION_OK && sink && !sink(ctx, value))
                return i;
            if (error != HOST_NOTIFICATION_OK && diagnostic)
                diagnostic(ctx, error);
            s->length = 0;
        } else if (s->length + 1 >= s->capacity) {
            s->discarding = true;
            s->length = 0;
            if (diagnostic)
                diagnostic(ctx, HOST_NOTIFICATION_TOO_LONG);
        } else
            s->line[s->length++] = c;
    }
    return n;
}
void host_notification_eof(host_notification_stream *s, host_notification_diagnostic d, void *ctx) {
    if (s->length && d)
        d(ctx, HOST_NOTIFICATION_FRAGMENT);
    host_notification_reset(s);
}
