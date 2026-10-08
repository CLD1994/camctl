#define _GNU_SOURCE
#include "camctl_host.h"
#include <assert.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <pthread.h>
#include <signal.h>
#include <stdlib.h>
#include <stdatomic.h>
#include <sys/wait.h>
#include <unistd.h>
static int malloc_failure, calloc_failure, pipe_failure, thread_failure, mutex_failure,
    cond_failure, fcntl_failure;
static atomic_int allocations;
void __real_free(void *);
void __wrap_free(void *p) {
    if (p)
        atomic_fetch_sub(&allocations, 1);
    __real_free(p);
}
int __real_fcntl(int, int, ...);
int __wrap_fcntl(int fd, int cmd, ...) {
    va_list args;
    va_start(args, cmd);
    int arg = va_arg(args, int);
    va_end(args);
    if (fcntl_failure && !--fcntl_failure) {
        errno = EIO;
        return -1;
    }
    return __real_fcntl(fd, cmd, arg);
}
static void motor(int position) {
    (void)position;
    assert(!"未发布的实例不应调用回调");
}
int __real_pthread_mutex_init(pthread_mutex_t *, const pthread_mutexattr_t *);
int __real_pthread_cond_init(pthread_cond_t *, const pthread_condattr_t *);
int __wrap_pthread_mutex_init(pthread_mutex_t *m, const pthread_mutexattr_t *a) {
    if (mutex_failure && !--mutex_failure)
        return EAGAIN;
    return __real_pthread_mutex_init(m, a);
}
int __wrap_pthread_cond_init(pthread_cond_t *c, const pthread_condattr_t *a) {
    if (cond_failure && !--cond_failure)
        return EAGAIN;
    return __real_pthread_cond_init(c, a);
}
void *__real_malloc(size_t);
void *__real_calloc(size_t, size_t);
int __real_pipe2(int[2], int);
int __real_pthread_create(pthread_t *, const pthread_attr_t *, void *(*)(void *), void *);
void *__wrap_malloc(size_t n) {
    if (malloc_failure && !--malloc_failure)
        return NULL;
    void *p = __real_malloc(n);
    if (p)
        atomic_fetch_add(&allocations, 1);
    return p;
}
void *__wrap_calloc(size_t n, size_t s) {
    if (calloc_failure && !--calloc_failure)
        return NULL;
    void *p = __real_calloc(n, s);
    if (p)
        atomic_fetch_add(&allocations, 1);
    return p;
}
int __wrap_pipe2(int fds[2], int flags) {
    if (pipe_failure && !--pipe_failure) {
        errno = EMFILE;
        return -1;
    }
    return __real_pipe2(fds, flags);
}
int __wrap_pthread_create(pthread_t *t, const pthread_attr_t *a, void *(*fn)(void *), void *p) {
    if (thread_failure && !--thread_failure)
        return EAGAIN;
    return __real_pthread_create(t, a, fn, p);
}
static int directory_count(const char *path) {
    DIR *d = opendir(path);
    assert(d);
    int count = 0;
    while (readdir(d))
        count++;
    closedir(d);
    return count;
}
static int fd_count(void) { return directory_count("/proc/self/fd"); }
int main(void) {
    camctl_host_config c = CAMCTL_HOST_CONFIG_INIT;
    c.camctl_path = "/missing-camctl";
    c.ready_path = "/ready";
    c.processing_path = "/processing";
    c.log_path = "/missing-dir/log";
    int baseline = fd_count();
    int threads = directory_count("/proc/self/task");
    assert(camctl_host_submit("/A") == -1);
    assert(errno == ENODEV);
    struct sigaction original, action = {0}, after;
    assert(!sigaction(SIGCHLD, NULL, &original));
    action.sa_handler = SIG_IGN;
    sigemptyset(&action.sa_mask);
    assert(!sigaction(SIGCHLD, &action, NULL));
    assert(camctl_host_init(&c, NULL) == -1);
    assert(errno == EINVAL);
    assert(!sigaction(SIGCHLD, NULL, &after));
    assert(after.sa_handler == SIG_IGN);
    action.sa_handler = SIG_DFL;
    action.sa_flags = SA_NOCLDWAIT;
    assert(!sigaction(SIGCHLD, &action, NULL));
    assert(camctl_host_init(&c, NULL) == -1);
    assert(errno == EINVAL);
    assert(fd_count() == baseline);
    assert(!sigaction(SIGCHLD, &original, NULL));
    assert(!camctl_host_register_motor_control_callback(motor));
    int *failures[] = {&malloc_failure, &calloc_failure, &pipe_failure, &thread_failure,
                       &mutex_failure,  &cond_failure,   &fcntl_failure};
    const int counts[] = {5, 3, 2, 3, 4, 2, 3};
    const int expected[] = {ENOMEM, ENOMEM, EMFILE, EAGAIN, EAGAIN, EAGAIN, EIO};
    for (size_t j = 0; j < sizeof(counts) / sizeof(counts[0]); j++) {
        for (int i = 1; i <= counts[j]; i++) {
            *failures[j] = i;
            assert(camctl_host_init(&c, NULL) == -1);
            assert(errno == expected[j]);
            assert(fd_count() == baseline);
            assert(directory_count("/proc/self/task") == threads);
            assert(atomic_load(&allocations) == 0);
            assert(camctl_host_register_motor_control_callback(motor) == -1 && errno == EALREADY);
            assert(camctl_host_submit("/A") == -1 && errno == ENODEV);
        }
    }
    assert(camctl_host_init(&c, NULL) == 0);
    assert(camctl_host_init(&c, NULL) == -1);
    assert(errno == EALREADY);
    return 0;
}
