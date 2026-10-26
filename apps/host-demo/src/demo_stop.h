#ifndef CAMCTL_DEMO_STOP_H
#define CAMCTL_DEMO_STOP_H

#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>

typedef struct {
    sigset_t signals;
    atomic_bool exiting;
} demo_stop;

/* 在初始化模块、创建任何线程前调用；状态存活到进程结束。 */
int demo_stop_prepare(demo_stop *stop);
/* 模块初始化成功后调用；线程创建失败同样完成停止后退出。 */
void demo_stop_start(demo_stop *stop);
bool demo_stop_command(demo_stop *stop, const char *command);
_Noreturn void demo_stop_exit(demo_stop *stop, int status);

#endif
