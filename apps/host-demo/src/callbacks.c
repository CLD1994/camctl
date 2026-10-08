#include "callbacks.h"
#include <errno.h>
int host_callbacks_register(host_callbacks *c, camctl_host_motor_control_callback callback) {
    if (!callback)
        return EINVAL;
    if (c->initialized)
        return EBUSY;
    if (c->motor)
        return EALREADY;
    c->motor = callback;
    return 0;
}
