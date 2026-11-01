#ifndef HOST_MAINTENANCE_H
#define HOST_MAINTENANCE_H
#include "update_result.h"
#include <stdatomic.h>

int host_maintenance_validate(const camctl_host_update_config *config,
                              const camctl_host_update_request *request, bool allow_check);
void host_maintenance_result_init(host_update_result *result, camctl_update_action action);
int host_maintenance_run(const camctl_host_update_config *config,
                         const camctl_host_update_request *request, host_update_result *result);
int host_maintenance_run_interruptible(const camctl_host_update_config *config,
                         const camctl_host_update_request *request, host_update_result *result,
                         const atomic_bool *stop);
#endif
