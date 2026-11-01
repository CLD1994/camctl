#ifndef HOST_UPDATE_RESULT_H
#define HOST_UPDATE_RESULT_H
#include "camctl_host.h"
#include "result.h"
#include <stdbool.h>
#include <stddef.h>

typedef camctl_host_update_result host_update_result;

/* 对不完整、非法、重复、信号退出或矛盾结果保留未知，不从退出码推断效果。 */
bool host_update_result_parse(camctl_update_action expected, const char *data, size_t length,
                              host_exit termination, int exit_code, host_update_result *result);
#endif
