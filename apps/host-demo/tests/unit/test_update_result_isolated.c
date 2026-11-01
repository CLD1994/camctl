/* 只在当前进程解析内存中的结果，禁止接触进程、管道、文件和线程。 */
#include "update_result.h"
#include <assert.h>
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include <errno.h>
#include <pthread.h>

static char captured[200000];
static size_t captured_length;
static int failed, writes, fail_at;
static int cancel_state = PTHREAD_CANCEL_ENABLE, locked;
int host_diagnostic_publish_update(const char *path, int (*writer)(int, void *), void *context) {
    (void)path; (void)writer; (void)context; assert(0); return EIO;
}
int __wrap_pthread_setcancelstate(int state, int *old) {
    assert(!locked);
    if (old) *old = cancel_state;
    cancel_state = state;
    return 0;
}
void __wrap_flockfile(FILE *f) { (void)f; assert(cancel_state == PTHREAD_CANCEL_DISABLE); locked = 1; }
void __wrap_funlockfile(FILE *f) { (void)f; assert(locked); locked = 0; }
int __wrap_ferror(FILE *f) { (void)f; return failed; }
static size_t capture(const void *p, size_t n) {
    ++writes;
    if (fail_at && writes >= fail_at) { failed = 1; errno = EIO; return 0; }
    assert(captured_length + n <= sizeof(captured));
    memcpy(captured + captured_length, p, n); captured_length += n;
    return n;
}
int __wrap_fprintf(FILE *f, const char *format, ...) {
    (void)f; char buf[1024]; va_list ap; va_start(ap, format);
    int n = vsnprintf(buf, sizeof(buf), format, ap); va_end(ap);
    assert(n >= 0 && (size_t)n < sizeof(buf));
    return capture(buf, (size_t)n) == (size_t)n ? n : -1;
}
int __wrap___fprintf_chk(FILE *f, int flag, const char *format, ...) {
    (void)f; (void)flag; char buf[1024]; va_list ap; va_start(ap, format);
    int n = vsnprintf(buf, sizeof(buf), format, ap); va_end(ap);
    assert(n >= 0 && (size_t)n < sizeof(buf));
    return capture(buf, (size_t)n) == (size_t)n ? n : -1;
}
size_t __wrap_fwrite(const void *p, size_t size, size_t n, FILE *f) {
    (void)f; return size ? capture(p, size * n) / size : 0;
}
int __wrap_fputc(int c, FILE *f) { (void)f; char byte = (char)c; return capture(&byte, 1) ? c : EOF; }
int __wrap_fflush(FILE *f) { (void)f; return 0; }

static bool parse(const char *json, host_exit termination, int code, host_update_result *result) {
    return host_update_result_parse(CAMCTL_UPDATE_ACTION_APPLY, json, strlen(json), termination, code, result);
}

int main(void) {
    const char *applied = "{\"version\":1,\"action\":\"apply\",\"outcome\":\"applied\","
        "\"effect\":\"committed\",\"eligibility\":\"allowed\",\"prior_recovery\":\"none\",\"prior_evidence\":null,"
        "\"reason\":null,\"stage\":null,\"evidence\":\"/receipt\"}\n";
    const char *unchanged = "{\"version\":1,\"action\":\"apply\",\"outcome\":\"failed\","
        "\"effect\":\"unchanged\",\"eligibility\":\"allowed\",\"prior_recovery\":\"recovered\",\"prior_evidence\":\"/previous\","
        "\"reason\":\"bad package\",\"stage\":\"preflight\",\"evidence\":null}\n";
    const char *rolled_back = "{\"version\":1,\"action\":\"apply\",\"outcome\":\"failed\","
        "\"effect\":\"rolled_back\",\"eligibility\":\"allowed\",\"prior_recovery\":\"none\",\"prior_evidence\":null,"
        "\"reason\":null,\"stage\":null,\"evidence\":null}\n";
    const char *duplicate = "{\"version\":1,\"action\":\"apply\",\"outcome\":\"applied\","
        "\"effect\":\"committed\",\"eligibility\":\"allowed\",\"prior_recovery\":\"none\",\"prior_evidence\":null,"
        "\"reason\":null,\"stage\":null,\"stage\":null}\n";
    const char *contradictory = "{\"version\":1,\"action\":\"apply\",\"outcome\":\"applied\","
        "\"effect\":\"rolled_back\",\"eligibility\":\"allowed\",\"prior_recovery\":\"none\",\"prior_evidence\":null,"
        "\"reason\":null,\"stage\":null,\"evidence\":null}\n";
    const char *not_checked = "{\"version\":1,\"action\":\"check\",\"outcome\":\"not_started\","
        "\"effect\":\"not_started\",\"eligibility\":\"allowed\",\"prior_recovery\":\"none\",\"prior_evidence\":null,"
        "\"reason\":null,\"stage\":null,\"evidence\":null}\n";
    host_update_result result;
    assert(parse(applied, HOST_EXIT_NORMAL, 0, &result));
    assert(result.outcome == CAMCTL_UPDATE_OUTCOME_APPLIED && result.effect == CAMCTL_UPDATE_EFFECT_COMMITTED);
    assert(result.evidence_present && !strcmp(result.evidence, "/receipt"));
    camctl_host_update_result public_result;
    host_update_result_project(&result, 0, &public_result);
    assert(!strcmp(public_result.diagnostic_path, "/receipt"));
    result.evidence[0] = 'x';
    host_update_result_project(&result, 0, &public_result);
    assert(!public_result.diagnostic_path[0]);
    result.evidence[0] = '/'; result.evidence[2] = '\0';
    host_update_result_project(&result, 0, &public_result);
    assert(!public_result.diagnostic_path[0]);
    result.evidence_truncated = true;
    host_update_result_project(&result, 0, &public_result);
    assert(!public_result.diagnostic_path[0]);
    assert(parse(unchanged, HOST_EXIT_NORMAL, 1, &result));
    assert(result.effect == CAMCTL_UPDATE_EFFECT_UNCHANGED && result.prior_recovery == CAMCTL_UPDATE_PRIOR_RECOVERY_RECOVERED);
    assert(result.prior_evidence_present && !strcmp(result.prior_evidence, "/previous"));
    host_update_result_project(&result, 0, &public_result);
    assert(!strcmp(public_result.reason, "bad package"));
    memset(result.reason, 'a', sizeof(result.reason));
    result.reason_length = CAMCTL_HOST_UPDATE_REASON_MAX + 5;
    host_update_result_project(&result, 0, &public_result);
    assert(strlen(public_result.reason) == CAMCTL_HOST_UPDATE_REASON_MAX);
    result.reason[3] = '\0';
    host_update_result_project(&result, 0, &public_result);
    assert(strlen(public_result.reason) == 3);
    assert(parse(rolled_back, HOST_EXIT_NORMAL, 1, &result));
    assert(result.effect == CAMCTL_UPDATE_EFFECT_ROLLED_BACK);
    assert(!parse(applied, HOST_EXIT_NORMAL, 1, &result));
    assert(result.effect == CAMCTL_UPDATE_EFFECT_UNKNOWN && result.eligibility == CAMCTL_UPDATE_ELIGIBILITY_UNKNOWN);
    assert(!parse(applied, HOST_EXIT_UNKNOWN, 0, &result));
    assert(!parse(duplicate, HOST_EXIT_NORMAL, 0, &result));
    assert(!parse(contradictory, HOST_EXIT_NORMAL, 0, &result));
    assert(!host_update_result_parse(CAMCTL_UPDATE_ACTION_APPLY, applied, strlen(applied) - 1,
                                     HOST_EXIT_NORMAL, 0, &result));
    assert(!host_update_result_parse(CAMCTL_UPDATE_ACTION_RECOVER, applied, strlen(applied),
                                     HOST_EXIT_NORMAL, 0, &result));
    assert(!host_update_result_parse(CAMCTL_UPDATE_ACTION_CHECK, not_checked, strlen(not_checked),
                                     HOST_EXIT_NORMAL, 1, &result));
    memset(&result, 0, sizeof(result));
    result.outcome = CAMCTL_UPDATE_OUTCOME_FAILED;
    result.eligibility = CAMCTL_UPDATE_ELIGIBILITY_BLOCKED;
    memcpy(result.stdout_data, "A\0B", 3); result.stdout_length = 3; result.stdout_bytes_seen = 7;
    memcpy(result.stderr_data, "C\0D", 3); result.stderr_length = 3; result.stderr_truncated = true;
    memcpy(result.process_diagnostics, "E\0F", 3); result.process_diagnostics_length = 3;
    result.process_exit_known = true; result.process_signaled = true; result.process_exit_code = 9;
    errno = EDOM;
    host_update_result_deliver(&result, CAMCTL_UPDATE_ACTION_APPLY, 0);
    assert(errno == EDOM);
    assert(cancel_state == PTHREAD_CANCEL_ENABLE && !locked);
    assert(memmem(captured, captured_length, "A\0B", 3));
    assert(memmem(captured, captured_length, "seen=7", 6));
    assert(memmem(captured, captured_length, "C\0D", 3));
    assert(memmem(captured, captured_length, "E\0F", 3));
    assert(memmem(captured, captured_length, "exit_known=1 signaled=1 exit_code=9", strlen("exit_known=1 signaled=1 exit_code=9")));
    assert(memmem(captured, captured_length, "truncated=1", 11));
    captured_length = 0; writes = 0;
    memset(&result, 0, sizeof(result));
    result.outcome = CAMCTL_UPDATE_OUTCOME_APPLIED;
    result.eligibility = CAMCTL_UPDATE_ELIGIBILITY_ALLOWED;
    host_update_result_deliver(&result, CAMCTL_UPDATE_ACTION_APPLY, 0);
    assert(!writes);
    captured_length = 0; writes = 0;
    host_update_result_deliver(NULL, CAMCTL_UPDATE_ACTION_APPLY, EINVAL);
    assert(writes && errno == EDOM);
    result.outcome = CAMCTL_UPDATE_OUTCOME_FAILED;
    captured_length = 0; writes = failed = 0; fail_at = 2;
    host_update_result_deliver(&result, CAMCTL_UPDATE_ACTION_APPLY, 0);
    assert(writes == 2 && errno == EDOM);
    host_update_result_deliver(&result, CAMCTL_UPDATE_ACTION_APPLY, 0);
    assert(writes == 2 && errno == EDOM);
    return 0;
}
