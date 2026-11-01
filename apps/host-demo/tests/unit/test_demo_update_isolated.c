/* 使用内存替身验证演示入口的请求结果与启动资格。 */
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

static int failed_messages;
static int fake_fputs(const char *text, FILE *stream) {
    (void)text;
    if (stream == stderr)
        failed_messages++;
    return 0;
}
static int fake_printf(const char *format, ...) { (void)format; return 0; }
static int fake_fprintf(FILE *stream, const char *format, ...) { (void)stream; (void)format; return 0; }
static size_t fake_fwrite(const void *data, size_t size, size_t count, FILE *stream) {
    (void)data; (void)stream; return size ? count : 0;
}
static int fake_fputc(int value, FILE *stream) { (void)stream; return value; }
#define fputs fake_fputs
#define printf fake_printf
#define fprintf fake_fprintf
#define fwrite fake_fwrite
#define fputc fake_fputc
#define main unused_demo_main
#include "../../src/demo.c"
#undef main
#undef fputs
#undef printf
#undef fprintf
#undef fwrite
#undef fputc

static camctl_update_outcome outcome;
static camctl_update_effect effect;
static camctl_update_eligibility eligibility;
int camctl_host_update(const camctl_host_update_config *config, const camctl_host_update_request *request,
                       camctl_host_update_result *result) {
    (void)config; (void)request;
    memset(result, 0, sizeof(*result));
    result->outcome = outcome; result->effect = effect; result->eligibility = eligibility;
    return 0;
}
int main(void) {
    camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
    config.camctl_path = "/camctl"; config.updater_path = "/updater";
    eligibility = CAMCTL_UPDATE_ELIGIBILITY_ALLOWED;
    outcome = CAMCTL_UPDATE_OUTCOME_FAILED; effect = CAMCTL_UPDATE_EFFECT_ROLLED_BACK;
    assert(run_maintenance(&config, "/bundle.zip", NULL) == 1);
    assert(failed_messages == 1);
    effect = CAMCTL_UPDATE_EFFECT_UNCHANGED;
    assert(run_maintenance(&config, "/bundle.zip", NULL) == 1);
    assert(failed_messages == 2);
    outcome = CAMCTL_UPDATE_OUTCOME_APPLIED; effect = CAMCTL_UPDATE_EFFECT_COMMITTED;
    assert(run_maintenance(&config, "/bundle.zip", NULL) == 0);
    eligibility = CAMCTL_UPDATE_ELIGIBILITY_UNKNOWN;
    assert(run_maintenance(&config, "/bundle.zip", NULL) == 1);
    return 0;
}
