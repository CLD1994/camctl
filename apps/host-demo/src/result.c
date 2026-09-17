#include "result.h"
#include "yyjson.h"
#include <stdlib.h>
#include <string.h>

/* 只读取唯一的判定字段；不让重复键的选取顺序改变命令行为。 */
static yyjson_val *unique(yyjson_val *obj, const char *name) {
    yyjson_val *key, *val, *found = NULL;
    size_t idx, max;
    yyjson_obj_foreach(obj, idx, max, key, val) {
        if (yyjson_equals_str(key, name)) {
            if (found)
                return NULL;
            found = val;
        }
    }
    return found;
}

host_result host_result_parse(host_command command, const char *data, size_t len,
                              host_exit termination, int code) {
    host_result result = {HOST_RESULT_ABNORMAL, false};
    if (!data || !len || termination != HOST_EXIT_NORMAL || data[len - 1] != '\n' ||
        memchr(data, '\n', len - 1))
        return result;
    size_t size = yyjson_read_max_memory_usage(len, 0);
    if (!size)
        return result;
    void *pool = malloc(size);
    yyjson_alc alc;
    if (!pool)
        return result;
    if (!yyjson_alc_pool_init(&alc, pool, size)) {
        free(pool);
        return result;
    }
    yyjson_doc *doc = yyjson_read_opts((char *)data, len, 0, &alc, NULL);
    yyjson_val *root = yyjson_doc_get_root(doc);
    yyjson_val *kind = unique(root, "kind"), *body = unique(root, "body");
    if (yyjson_equals_str(kind, "succeeded") && code == 0) {
        yyjson_val *needs = unique(body, "needs_run");
        if (command == HOST_RUN || yyjson_is_bool(needs)) {
            result.kind = HOST_RESULT_SUCCESS;
            result.needs_run = command == HOST_SUBMIT && yyjson_get_bool(needs);
        }
    } else if (yyjson_equals_str(kind, "error") && code == 1 &&
               yyjson_is_str(unique(body, "reason")) && yyjson_is_obj(unique(body, "details"))) {
        result.kind = HOST_RESULT_ERROR;
    }
    yyjson_doc_free(doc);
    free(pool);
    return result;
}
