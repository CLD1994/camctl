#include "diagnostic.h"
#include <linux/fs.h>
#include <sys/syscall.h>
#include <unistd.h>

int host_diagnostic_rename_noreplace(int olddirfd, const char *oldpath, int newdirfd,
                                     const char *newpath) {
    return (int)syscall(SYS_renameat2, olddirfd, oldpath, newdirfd, newpath,
                        RENAME_NOREPLACE);
}
