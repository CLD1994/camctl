#include "demo_stop.h"
#include "camctl_host.h"
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int demo_stop_prepare(demo_stop *stop) {
    atomic_init(&stop->exiting, false);
    sigemptyset(&stop->signals);
    sigaddset(&stop->signals, SIGINT);
    sigaddset(&stop->signals, SIGTERM);
    int error = pthread_sigmask(SIG_BLOCK, &stop->signals, NULL);
    if (error) {
        errno = error;
        return -1;
    }
    return 0;
}
static void *wait_for_stop(void *argument) {
    demo_stop *stop = argument;
    int number;
    int error = sigwait(&stop->signals, &number);
    if (error) {
        fprintf(stderr, "等待退出信号失败：%s\n", strerror(error));
        demo_stop_exit(stop, EXIT_FAILURE);
    }
    demo_stop_exit(stop, EXIT_SUCCESS);
}
void demo_stop_start(demo_stop *stop) {
    pthread_t thread;
    int error = pthread_create(&thread, NULL, wait_for_stop, stop);
    if (error) {
        fprintf(stderr, "创建退出信号线程失败：%s\n", strerror(error));
        demo_stop_exit(stop, EXIT_FAILURE);
    }
}
bool demo_stop_command(demo_stop *stop, const char *command) {
    if (!strcmp(command, "quit") || !strcmp(command, "exit"))
        demo_stop_exit(stop, EXIT_SUCCESS);
    return false;
}
_Noreturn void demo_stop_exit(demo_stop *stop, int status) {
    if (atomic_exchange(&stop->exiting, true))
        pthread_exit(NULL);
    if (camctl_host_stop()) {
        fprintf(stderr, "停止 camctl 未完成：%s\n", strerror(errno));
        status = EXIT_FAILURE;
    }
    exit(status);
}
