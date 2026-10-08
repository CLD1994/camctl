#define _GNU_SOURCE
#include "camctl_host.h"
#include <assert.h>
#include <dirent.h>
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>
static int malloc_failure, calloc_failure, pipe_failure, thread_failure;
void *__real_malloc(size_t);
void *__real_calloc(size_t, size_t);
int __real_pipe2(int[2], int);
int __real_pthread_create(pthread_t *, const pthread_attr_t *, void *(*)(void *), void *);
void *__wrap_malloc(size_t n) {
    if (malloc_failure && !--malloc_failure)
        return NULL;
    return __real_malloc(n);
}
void *__wrap_calloc(size_t n, size_t s) {
    if (calloc_failure && !--calloc_failure)
        return NULL;
    return __real_calloc(n, s);
}
int __wrap_pipe2(int fds[2], int flags) {
    if (pipe_failure) {
        pipe_failure = 0;
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
static int fd_count(void) {
    DIR *d = opendir("/proc/self/fd");
    assert(d);
    int count = 0;
    while (readdir(d))
        count++;
    closedir(d);
    return count;
}
int main(void) {
    camctl_host_config c = CAMCTL_HOST_CONFIG_INIT;
    c.camctl_path = "/missing-camctl";
    c.ready_path = "/ready";
    c.processing_path = "/processing";
    c.log_path = "/missing-dir/log";
    int baseline = fd_count();
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
    for (int i = 1; i <= 2; i++) {
        malloc_failure = i;
        assert(camctl_host_init(&c, NULL) == -1);
        assert(errno == ENOMEM);
        assert(fd_count() == baseline);
        calloc_failure = i;
        assert(camctl_host_init(&c, NULL) == -1);
        assert(errno == ENOMEM);
        assert(fd_count() == baseline);
        thread_failure = i;
        assert(camctl_host_init(&c, NULL) == -1);
        assert(errno == EAGAIN);
        assert(fd_count() == baseline);
    }
    pipe_failure = 1;
    assert(camctl_host_init(&c, NULL) == -1);
    assert(errno == EMFILE);
    assert(fd_count() == baseline);
    assert(camctl_host_init(&c, NULL) == 0);
    assert(camctl_host_init(&c, NULL) == -1);
    assert(errno == EALREADY);
    return 0;
}
