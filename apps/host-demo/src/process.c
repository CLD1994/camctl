#include "process.h"
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>
extern char **environ;
void host_child_init(host_child *c, char *b, size_t n) {
    memset(c, 0, sizeof(*c));
    c->output = b;
    c->capacity = n;
    c->out_fd = c->err_fd = -1;
}
int host_child_spawn(host_child *c, const camctl_host_config *cfg, host_command cmd,
                     const char *plan) {
    char *argv[7];
    size_t n = 0;
    argv[n++] = (char *)cfg->camctl_path;
    argv[n++] = cmd == HOST_RUN ? "run" : "submit";
    if (plan)
        argv[n++] = (char *)plan;
    if (cfg->config_path) {
        argv[n++] = "--config";
        argv[n++] = (char *)cfg->config_path;
    }
    argv[n] = NULL;
    int out[2], err[2], rc;
    if (host_pipe(out))
        return errno;
    if (host_pipe(err)) {
        rc = errno;
        close(out[0]);
        close(out[1]);
        return rc;
    }
    posix_spawn_file_actions_t actions;
    posix_spawnattr_t attr;
    rc = posix_spawn_file_actions_init(&actions);
    if (rc)
        goto pipes;
    rc = posix_spawnattr_init(&attr);
    if (rc)
        goto actions;
    sigset_t mask, defaults;
    sigemptyset(&mask);
    sigfillset(&defaults);
    sigdelset(&defaults, SIGKILL);
    sigdelset(&defaults, SIGSTOP);
    if ((rc = posix_spawnattr_setsigmask(&attr, &mask)) ||
        (rc = posix_spawnattr_setsigdefault(&attr, &defaults)) ||
        (rc = posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF)) ||
        (rc = posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)) ||
        (rc = posix_spawn_file_actions_adddup2(&actions, out[1], 1)) ||
        (rc = posix_spawn_file_actions_adddup2(&actions, err[1], 2)) ||
        (rc = posix_spawn_file_actions_addclose(&actions, out[0])) ||
        (rc = posix_spawn_file_actions_addclose(&actions, err[0])) ||
        (rc = posix_spawn_file_actions_addclose(&actions, out[1])) ||
        (rc = posix_spawn_file_actions_addclose(&actions, err[1])))
        goto attr;
    pid_t pid;
    rc = posix_spawn(&pid, cfg->camctl_path, &actions, &attr, argv, environ);
    if (!rc) {
        char *buffer = c->output;
        size_t capacity = c->capacity;
        uint64_t id = c->id;
        host_child_init(c, buffer, capacity);
        c->id = id;
        c->pid = pid;
        c->out_fd = out[0];
        c->err_fd = err[0];
    }
attr:
    posix_spawnattr_destroy(&attr);
actions:
    posix_spawn_file_actions_destroy(&actions);
pipes:
    close(out[1]);
    close(err[1]);
    if (rc) {
        close(out[0]);
        close(err[0]);
    }
    return rc;
}
static void drain(host_child *c, int *fd, bool output, host_child_log log, void *p) {
    char data[4096];
    for (size_t quota = 0; *fd >= 0 && quota < 64 * 1024;) {
        ssize_t n = read(*fd, data, sizeof(data));
        if (n > 0) {
            quota += (size_t)n;
            c->execution = HOST_EXEC_OBSERVED;
            if (output) {
                size_t keep = (size_t)n;
                if (keep > c->capacity - c->length) {
                    keep = c->capacity - c->length;
                    c->overflow = true;
                }
                memcpy(c->output + c->length, data, keep);
                c->length += keep;
            } else if (log)
                log(p, "stderr", data, (size_t)n);
        } else if (n == 0) {
            close(*fd);
            *fd = -1;
        } else if (errno == EINTR) {
            break;
        } else if (errno == EAGAIN || errno == EWOULDBLOCK) {
            /* 本进程已退出但继承管道的其他进程未关闭：不能无限等待它们。 */
            if (c->ended) {
                if (output)
                    c->io_error = true;
                if (log) {
                    const char *message = "pipe remained open after child exit";
                    log(p, output ? "stdout_incomplete" : "stderr_closed_after_exit", message,
                        strlen(message));
                }
                close(*fd);
                *fd = -1;
            }
            break;
        } else {
            if (output)
                c->io_error = true;
            if (log)
                log(p, output ? "stdout_read_error" : "stderr_read_error", strerror(errno),
                    strlen(strerror(errno)));
            close(*fd);
            *fd = -1;
        }
    }
}
void host_child_collect(host_child *c, host_child_log log, void *p) {
    if (!c->pid)
        return;
    if (!c->ended && !c->wait_fault) {
        int status;
        pid_t result = waitpid(c->pid, &status, WNOHANG);
        if (result == c->pid && (WIFEXITED(status) || WIFSIGNALED(status))) {
            c->ended = true;
            c->termination = WIFEXITED(status) ? HOST_EXIT_NORMAL : HOST_EXIT_SIGNAL;
            c->exit_code = WIFEXITED(status) ? WEXITSTATUS(status) : WTERMSIG(status);
        } else if (result == c->pid) {
            if (log) {
                const char *message = "state change without termination";
                log(p, "child_not_exited", message, strlen(message));
            }
        } else if (result < 0 && errno != EINTR) {
            /* 没有退出证据时保留该进程占位，不能猜测已退出并启动第二个 run。 */
            c->wait_fault = true;
            c->termination = HOST_EXIT_UNKNOWN;
            if (log)
                log(p, "wait_unknown", strerror(errno), strlen(strerror(errno)));
        }
    }
    drain(c, &c->out_fd, true, log, p);
    drain(c, &c->err_fd, false, log, p);
}
bool host_child_done(const host_child *c) {
    return c->pid && c->ended && c->out_fd < 0 && c->err_fd < 0;
}
int host_pipe(int fds[2]) {
    if (pipe2(fds, O_CLOEXEC))
        return -1;
    for (int i = 0; i < 2; i++) {
        if (fds[i] < 3) {
            int fd = fcntl(fds[i], F_DUPFD_CLOEXEC, 3);
            if (fd < 0) {
                int e = errno;
                close(fds[0]);
                close(fds[1]);
                errno = e;
                return -1;
            }
            close(fds[i]);
            fds[i] = fd;
        }
    }
    if (fcntl(fds[0], F_SETFL, O_NONBLOCK)) {
        int e = errno;
        close(fds[0]);
        close(fds[1]);
        errno = e;
        return -1;
    }
    return 0;
}
