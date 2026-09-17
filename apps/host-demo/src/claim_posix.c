#include "claim.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
typedef struct {
    const camctl_host_config *config;
    host_logger *logger;
    int ready, processing;
    DIR *listing;
    struct stat ready_stat, processing_stat;
} claim_context;
static int begin(void *p) {
    claim_context *c = p;
    c->ready = open(c->config->ready_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (c->ready < 0)
        return errno;
    c->processing = open(c->config->processing_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (c->processing < 0)
        return errno;
    if (fstat(c->ready, &c->ready_stat) || fstat(c->processing, &c->processing_stat))
        return errno;
    if (c->ready_stat.st_dev != c->processing_stat.st_dev)
        return EXDEV;
    if (c->ready_stat.st_ino == c->processing_stat.st_ino)
        return EINVAL;
    int fd = fcntl(c->ready, F_DUPFD_CLOEXEC, 3);
    if (fd < 0)
        return errno;
    c->listing = fdopendir(fd);
    if (!c->listing) {
        int e = errno;
        close(fd);
        return e;
    }
    return 0;
}
static int next(void *p, char name[256]) {
    claim_context *c = p;
    for (;;) {
        errno = 0;
        struct dirent *item = readdir(c->listing);
        if (!item)
            return -errno;
        if (!strcmp(item->d_name, ".") || !strcmp(item->d_name, ".."))
            continue;
        size_t n = strnlen(item->d_name, 256);
        if (n == 256)
            return -ENAMETOOLONG;
        memcpy(name, item->d_name, n + 1);
        return 1;
    }
}
static int finish(void *p) {
    claim_context *c = p;
    DIR *d = c->listing;
    c->listing = NULL;
    return closedir(d) ? errno : 0;
}
static int check_directory(const char *path, const struct stat *original) {
    struct stat st;
    if (stat(path, &st))
        return errno;
    if (!S_ISDIR(st.st_mode) || st.st_dev != original->st_dev || st.st_ino != original->st_ino)
        return ESTALE;
    if (faccessat(AT_FDCWD, path, R_OK | W_OK | X_OK, AT_EACCESS))
        return errno;
    return 0;
}
static int healthy(void *p) {
    claim_context *c = p;
    int e = check_directory(c->config->ready_path, &c->ready_stat);
    return e ? e : check_directory(c->config->processing_path, &c->processing_stat);
}
static int move(void *p, const char *name) {
    claim_context *c = p;
    struct stat st;
    if (fstatat(c->ready, name, &st, AT_SYMLINK_NOFOLLOW))
        return errno;
    if (!S_ISREG(st.st_mode))
        return EINVAL;
    return renameat(c->ready, name, c->processing, name) ? errno : 0;
}
static int sync_dirs(void *p) {
    claim_context *c = p;
    int e = 0;
    if (fsync(c->processing))
        e = errno;
    if (fsync(c->ready) && !e)
        e = errno;
    return e;
}
static void end(void *p) {
    claim_context *c = p;
    if (c->listing)
        closedir(c->listing);
    if (c->ready >= 0)
        close(c->ready);
    if (c->processing >= 0)
        close(c->processing);
}
static void diagnose(void *p, const char *event, const char *name, int e) {
    claim_context *c = p;
    host_log(c->logger, "claim operation=%s ready=%s processing=%s file=%s errno=%d detail=%s",
             event, c->config->ready_path, c->config->processing_path, name, e,
             e ? strerror(e) : "ok");
}
void host_claim_posix(const camctl_host_config *config, host_logger *logger) {
    claim_context c = {.config = config, .logger = logger, .ready = -1, .processing = -1};
    const host_claim_ops ops = {begin, next, finish, healthy, move, sync_dirs, end, diagnose};
    host_claim_files(&ops, &c, 16 * 1024 * 1024);
}
