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
    const char *failed_step, *failed_path;
} claim_context;
static int directory_failure(claim_context *c, const char *step, const char *path, int e) {
    c->failed_step = step;
    c->failed_path = path;
    return e;
}
static int begin(void *p) {
    claim_context *c = p;
    c->ready = open(c->config->ready_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (c->ready < 0)
        return directory_failure(c, "open", c->config->ready_path, errno);
    c->processing = open(c->config->processing_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (c->processing < 0)
        return directory_failure(c, "open", c->config->processing_path, errno);
    if (fstat(c->ready, &c->ready_stat))
        return directory_failure(c, "fstat", c->config->ready_path, errno);
    if (fstat(c->processing, &c->processing_stat))
        return directory_failure(c, "fstat", c->config->processing_path, errno);
    if (c->ready_stat.st_dev != c->processing_stat.st_dev)
        return directory_failure(c, "filesystem", c->config->processing_path, EXDEV);
    if (c->ready_stat.st_ino == c->processing_stat.st_ino)
        return directory_failure(c, "identity", c->config->processing_path, EINVAL);
    int fd = fcntl(c->ready, F_DUPFD_CLOEXEC, 3);
    if (fd < 0)
        return directory_failure(c, "duplicate_listing_fd", c->config->ready_path, errno);
    c->listing = fdopendir(fd);
    if (!c->listing) {
        int e = errno;
        close(fd);
        return directory_failure(c, "fdopendir", c->config->ready_path, e);
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
static int check_directory(claim_context *c, const char *path, const struct stat *original) {
    struct stat st;
    if (stat(path, &st))
        return directory_failure(c, "stat", path, errno);
    if (!S_ISDIR(st.st_mode) || st.st_dev != original->st_dev || st.st_ino != original->st_ino)
        return directory_failure(c, "identity", path, ESTALE);
    if (faccessat(AT_FDCWD, path, R_OK | W_OK | X_OK, AT_EACCESS))
        return directory_failure(c, "access", path, errno);
    return 0;
}
static int healthy(void *p) {
    claim_context *c = p;
    int e = check_directory(c, c->config->ready_path, &c->ready_stat);
    return e ? e : check_directory(c, c->config->processing_path, &c->processing_stat);
}
static host_claim_move_result move(void *p, const char *name) {
    claim_context *c = p;
    struct stat st;
    if (fstatat(c->ready, name, &st, AT_SYMLINK_NOFOLLOW))
        return (host_claim_move_result){HOST_CLAIM_SOURCE_CHECK_FAILED, errno};
    if (!S_ISREG(st.st_mode))
        return (host_claim_move_result){HOST_CLAIM_SOURCE_NOT_REGULAR, EINVAL};
    if (!renameat(c->ready, name, c->processing, name))
        return (host_claim_move_result){HOST_CLAIM_MOVED, 0};
    int e = errno;
    return (host_claim_move_result){e == EIO || e == EINTR ? HOST_CLAIM_MOVE_UNKNOWN
                                                         : HOST_CLAIM_MOVE_FAILED, e};
}
static host_claim_sync_result sync_dirs(void *p) {
    claim_context *c = p;
    host_claim_sync_result result = {0};
    if (fsync(c->processing))
        result.processing_error = errno;
    if (fsync(c->ready))
        result.ready_error = errno;
    return result;
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
    if (!strcmp(event, "directories")) {
        host_log(c->logger,
                 "claim operation=%s step=%s directory=%s file=%s errno=%d detail=%s",
                 event, c->failed_step, c->failed_path, name, e, strerror(e));
        return;
    }
    host_log(c->logger, "claim operation=%s ready=%s processing=%s file=%s errno=%d detail=%s",
             event, c->config->ready_path, c->config->processing_path, name, e,
             e ? strerror(e) : "ok");
}
void host_claim_posix(const camctl_host_config *config, host_logger *logger) {
    claim_context c = {.config = config, .logger = logger, .ready = -1, .processing = -1};
    const host_claim_ops ops = {begin, next, finish, healthy, move, sync_dirs, end, diagnose};
    host_claim_files(&ops, &c, 16 * 1024 * 1024);
}
