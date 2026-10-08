#ifndef HOST_CONFIG_H
#define HOST_CONFIG_H
#include "camctl_host.h"
#define HOST_LOG_FILE_COUNT_MAX 64u
int host_config_validate(const camctl_host_config *config, const char *initial);
int host_path_validate(const char *path);
#endif
