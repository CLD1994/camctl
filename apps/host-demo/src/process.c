#include "process.h"
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>
extern char **environ;
int host_check_reaping_contract(void) {
    struct sigaction action;
    if (sigaction(SIGCHLD, NULL, &action))
        return errno;
    return action.sa_handler == SIG_IGN || (action.sa_flags & SA_NOCLDWAIT) ? EINVAL : 0;
}
void host_child_init(host_child *c, char *b, size_t n) {
    memset(c, 0, sizeof(*c));
    c->output = b;
    c->capacity = n;
    c->out_fd = c->err_fd = c->notification_fd = c->notification_write_fd = -1;
    host_group_scan_init(&c->scan);
}
int host_child_prepare_notifications(host_child *c) {
    if (c->notification_fd >= 0)
        return 0;
    int fds[2];
    if (host_pipe(fds))
        return errno;
    c->notification_fd = fds[0];
    c->notification_write_fd = fds[1];
    return 0;
}
void host_child_close_notifications(host_child *c) {
    if (c->notification_fd >= 0)
        close(c->notification_fd);
    if (c->notification_write_fd >= 0)
        close(c->notification_write_fd);
    c->notification_fd = c->notification_write_fd = -1;
}
int host_child_spawn(host_child *c, const camctl_host_config *cfg, host_command cmd,
                     const char *plan) {
    if (c->pid && !host_child_done(c))
        return EBUSY;
    int contract = host_check_reaping_contract();
    if (contract)
        return contract;
    bool notify = cmd == HOST_RUN && c->notifications_enabled;
    if (notify) {
        int e = host_child_prepare_notifications(c);
        if (e)
            return e;
    }
    char notification_number[32];
    char *argv[9];
    size_t n = 0;
    argv[n++] = (char *)cfg->camctl_path;
    argv[n++] = cmd == HOST_RUN ? "run" : "submit";
    if (plan)
        argv[n++] = (char *)plan;
    if (cfg->config_path) {
        argv[n++] = "--config";
        argv[n++] = (char *)cfg->config_path;
    }
    if (notify) {
        snprintf(notification_number, sizeof(notification_number), "%d", c->notification_write_fd);
        argv[n++] = "--host-notification-fd";
        argv[n++] = notification_number;
    }
    argv[n] = NULL;
    int out[2], err[2], rc;
    if (host_pipe(out)) {
        rc = errno;
        if (notify)
            host_child_close_notifications(c);
        return rc;
    }
    if (host_pipe(err)) {
        rc = errno;
        close(out[0]);
        close(out[1]);
        if (notify)
            host_child_close_notifications(c);
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
        (rc = posix_spawnattr_setpgroup(&attr, 0)) ||
        (rc = posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF |
                                                  POSIX_SPAWN_SETPGROUP)) ||
        (rc = posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)) ||
        (rc = posix_spawn_file_actions_adddup2(&actions, out[1], 1)) ||
        (rc = posix_spawn_file_actions_adddup2(&actions, err[1], 2)) ||
        (rc = posix_spawn_file_actions_addclose(&actions, out[0])) ||
        (rc = posix_spawn_file_actions_addclose(&actions, err[0])) ||
        (rc = posix_spawn_file_actions_addclose(&actions, out[1])) ||
        (rc = posix_spawn_file_actions_addclose(&actions, err[1])))
        goto attr;
    if (notify && ((rc = posix_spawn_file_actions_adddup2(&actions, c->notification_write_fd,
                                                          c->notification_write_fd)) ||
                   (rc = posix_spawn_file_actions_addclose(&actions, c->notification_fd))))
        goto attr;
    pid_t pid;
    rc = posix_spawn(&pid, cfg->camctl_path, &actions, &attr, argv, environ);
    if (!rc) {
        char *buffer = c->output;
        size_t capacity = c->capacity;
        uint64_t id = c->id;
        int notification_fd = c->notification_fd, notification_write_fd = c->notification_write_fd;
        bool enabled = c->notifications_enabled;
        host_child_init(c, buffer, capacity);
        c->id = id;
        c->notifications_enabled = enabled;
        c->notification_fd = notification_fd;
        c->notification_write_fd = notification_write_fd;
        c->pid = pid;
        c->pgid = pid;
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
    if (notify) {
        close(c->notification_write_fd);
        c->notification_write_fd = -1;
        if (rc)
            host_child_close_notifications(c);
    }
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
            /* 原组已收场并完成最终回收；脱离原组的共享服务可能仍持有管道。 */
            if (c->reaped) {
                if (output)
                    c->io_error = true;
                if (log) {
                    const char *message = "pipe remained open after group settlement";
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
static void event(host_child_log log, void *context, const char *step, const char *message) {
    if (log)
        log(context, step, message, strlen(message));
}
static void identity_lost(host_child *c, host_child_log log, void *context, int error) {
    c->wait_fault = true;
    if (!c->ended)
        c->termination = HOST_EXIT_UNKNOWN;
    host_group_scan_reset(&c->scan);
    event(log, context, "wait_unknown", strerror(error));
}
static bool observe_exit(host_child *c, host_child_log log, void *context) {
    siginfo_t info = {0};
    if (waitid(P_PID, (id_t)c->pid, &info, WEXITED | WNOHANG | WNOWAIT)) {
        int error = errno;
        if (error == ECHILD)
            identity_lost(c, log, context, error);
        else if (error != EINTR)
            event(log, context, "wait_unknown", strerror(error));
        return false;
    }
    if (!info.si_pid) {
        if (c->ended)
            identity_lost(c, log, context, ECHILD);
        return false;
    }
    if (info.si_pid != c->pid) {
        identity_lost(c, log, context, ESTALE);
        return false;
    }
    if (info.si_code != CLD_EXITED && info.si_code != CLD_KILLED && info.si_code != CLD_DUMPED) {
        if (c->ended)
            identity_lost(c, log, context, ESTALE);
        else
            event(log, context, "child_not_exited", "state change without termination");
        return false;
    }
    host_exit termination = info.si_code == CLD_EXITED ? HOST_EXIT_NORMAL : HOST_EXIT_SIGNAL;
    if (c->ended && (c->termination != termination || c->exit_code != info.si_status)) {
        identity_lost(c, log, context, ESTALE);
        return false;
    }
    if (!c->ended) {
        c->ended = true;
        c->termination = termination;
        c->exit_code = info.si_status;
        event(log, context, "exit_observed", "exit record retained; group settlement pending");
    }
    return true;
}
static bool group_identity(host_child *c, host_child_log log, void *context) {
    pid_t group = getpgid(c->pid);
    if (c->pgid != c->pid || c->pgid <= 1 || group != c->pgid) {
        int error = group < 0 ? errno : ESTALE;
        if (error == ESRCH || error == ESTALE)
            identity_lost(c, log, context, error);
        else
            event(log, context, "group_identity_unknown", strerror(error));
        return false;
    }
    return true;
}
static void settle(host_child *c, host_child_log log, void *context) {
    if (!observe_exit(c, log, context) || !group_identity(c, log, context))
        return;
    host_group_result result = host_group_scan_batch(&c->scan, c->pgid, 64);
    if (result.status == HOST_GROUP_PENDING)
        return;
    if (result.status == HOST_GROUP_UNKNOWN) {
        if (c->settlement_error != result.error || c->settlement_operation != result.operation) {
            char message[256];
            snprintf(message, sizeof(message), "operation=%s member=%ld errno=%d detail=%s",
                     host_group_operation_name(result.operation), (long)result.member, result.error,
                     strerror(result.error));
            event(log, context, "group_unknown", message);
        }
        c->settlement_error = result.error;
        c->settlement_operation = result.operation;
        return;
    }
    c->settlement_error = 0;
    /* 扫描跨多个轮询，发信号和最终回收之前再次确认保留的退出记录。 */
    if (!observe_exit(c, log, context) || !group_identity(c, log, context)) {
        host_group_scan_reset(&c->scan);
        return;
    }
    if (result.status == HOST_GROUP_LIVE) {
        if (kill(-c->pgid, SIGKILL)) {
            event(log, context, "group_kill_failed", strerror(errno));
        } else {
            event(log, context, "group_kill_sent", "SIGKILL sent; settlement pending");
        }
        /* 请求成功、ESRCH 或其他错误均不能代替新一轮完整核验。 */
        host_group_scan_reset(&c->scan);
        return;
    }
    int status;
    pid_t pid = waitpid(c->pid, &status, WNOHANG);
    if (pid < 0) {
        int error = errno;
        if (error == ECHILD)
            identity_lost(c, log, context, error);
        else if (error != EINTR)
            event(log, context, "final_reap_unknown", strerror(error));
        return;
    }
    if (!pid) {
        event(log, context, "final_reap_unknown", "retained exit record not returned");
        return;
    }
    if (pid != c->pid || (!WIFEXITED(status) && !WIFSIGNALED(status)) ||
        (WIFEXITED(status) ? HOST_EXIT_NORMAL : HOST_EXIT_SIGNAL) != c->termination ||
        (WIFEXITED(status) ? WEXITSTATUS(status) : WTERMSIG(status)) != c->exit_code) {
        identity_lost(c, log, context, ESTALE);
        return;
    }
    c->reaped = true;
    event(log, context, "group_reaped", "all execution stopped; child record reaped");
}
void host_child_collect(host_child *c, host_child_log log, void *p) {
    if (!c->pid)
        return;
    if (!c->reaped && !c->wait_fault)
        settle(c, log, p);
    drain(c, &c->out_fd, true, log, p);
    drain(c, &c->err_fd, false, log, p);
}
bool host_child_done(const host_child *c) {
    return c->pid && c->reaped && c->out_fd < 0 && c->err_fd < 0 && c->notification_fd < 0;
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
