#ifndef HOST_CALLBACKS_H
#define HOST_CALLBACKS_H
#include "camctl_host.h"
#include <stdbool.h>
typedef struct {
    camctl_host_motor_control_callback motor;
    bool initialized;
} host_callbacks;
int host_callbacks_register(host_callbacks *c, camctl_host_motor_control_callback callback);
#endif
