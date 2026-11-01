/* 只在当前进程解析内存中的结果，禁止接触进程、管道、文件和线程。 */
#include "update_result.h"
#include <assert.h>
#include <string.h>

static bool parse(const char *json, host_exit termination, int code, host_update_result *result) {
    return host_update_result_parse(CAMCTL_UPDATE_ACTION_APPLY, json, strlen(json), termination, code, result);
}

int main(void) {
    const char *applied = "{\"version\":1,\"action\":\"apply\",\"outcome\":\"applied\","
        "\"effect\":\"committed\",\"eligibility\":\"allowed\",\"prior_recovery\":\"none\","
        "\"reason\":null,\"stage\":null,\"evidence\":\"/receipt\"}\n";
    const char *unchanged = "{\"version\":1,\"action\":\"apply\",\"outcome\":\"failed\","
        "\"effect\":\"unchanged\",\"eligibility\":\"allowed\",\"prior_recovery\":\"recovered\","
        "\"reason\":\"bad package\",\"stage\":\"preflight\",\"evidence\":null}\n";
    const char *rolled_back = "{\"version\":1,\"action\":\"apply\",\"outcome\":\"failed\","
        "\"effect\":\"rolled_back\",\"eligibility\":\"allowed\",\"prior_recovery\":\"none\","
        "\"reason\":null,\"stage\":null,\"evidence\":null}\n";
    const char *duplicate = "{\"version\":1,\"action\":\"apply\",\"outcome\":\"applied\","
        "\"effect\":\"committed\",\"eligibility\":\"allowed\",\"prior_recovery\":\"none\","
        "\"reason\":null,\"stage\":null,\"stage\":null}\n";
    const char *contradictory = "{\"version\":1,\"action\":\"apply\",\"outcome\":\"applied\","
        "\"effect\":\"rolled_back\",\"eligibility\":\"allowed\",\"prior_recovery\":\"none\","
        "\"reason\":null,\"stage\":null,\"evidence\":null}\n";
    const char *not_checked = "{\"version\":1,\"action\":\"check\",\"outcome\":\"not_started\","
        "\"effect\":\"not_started\",\"eligibility\":\"allowed\",\"prior_recovery\":\"none\","
        "\"reason\":null,\"stage\":null,\"evidence\":null}\n";
    host_update_result result;
    assert(parse(applied, HOST_EXIT_NORMAL, 0, &result));
    assert(result.outcome == CAMCTL_UPDATE_OUTCOME_APPLIED && result.effect == CAMCTL_UPDATE_EFFECT_COMMITTED);
    assert(result.evidence_present && !strcmp(result.evidence, "/receipt"));
    assert(parse(unchanged, HOST_EXIT_NORMAL, 1, &result));
    assert(result.effect == CAMCTL_UPDATE_EFFECT_UNCHANGED && result.prior_recovery == CAMCTL_UPDATE_PRIOR_RECOVERY_RECOVERED);
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
    return 0;
}
