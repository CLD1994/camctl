/* 使用内存替身验证演示入口转交包路径及结果。 */
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

static int failures, successes;
static int fake_fprintf(FILE *stream, const char *format, ...) {
    (void)format;
    if (stream == stderr) failures++;
    return 0;
}
static int fake_puts(const char *text) { (void)text; successes++; return 0; }
#define fprintf fake_fprintf
#define puts fake_puts
#define main unused_demo_main
#include "../../src/demo.c"
#undef main
#undef fprintf
#undef puts

static int update_error;
int camctl_host_update(const char *bundle, const char *client_data) {
    assert(!strcmp(bundle, "/bundle.zip"));
    assert(client_data && !strcmp(client_data, "/client"));
    errno = EIO;
    return update_error;
}
int main(void) {
    update_error = -1;
    assert(run_maintenance("/bundle.zip", "/client") == 1);
    assert(failures == 1 && successes == 0);
    update_error = 0;
    assert(run_maintenance("/bundle.zip", "/client") == 0);
    assert(failures == 1 && successes == 1);
    return 0;
}
