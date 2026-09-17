#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <string.h>
#include "result.h"
typedef struct {
    host_command command;
    const char *json;
    host_exit termination;
    int code;
    host_result_kind kind;
    bool needs;
} example;
static void result_case(void **state) {
    const example *e = *state;
    host_result r =
        host_result_parse(e->command, e->json, strlen(e->json), e->termination, e->code);
    assert_int_equal(r.kind, e->kind);
    assert_int_equal(r.needs_run, e->needs);
}
int main(void) {
    const example cases[] = {
        {HOST_RUN, "{\"kind\":\"succeeded\"}\n", HOST_EXIT_NORMAL, 0, HOST_RESULT_SUCCESS, false},
        {HOST_SUBMIT, "{\"kind\":\"succeeded\",\"body\":{\"needs_run\":true}}\n", HOST_EXIT_NORMAL,
         0, HOST_RESULT_SUCCESS, true},
        {HOST_SUBMIT, "{\"kind\":\"succeeded\",\"body\":{\"needs_run\":false}}\n", HOST_EXIT_NORMAL,
         0, HOST_RESULT_SUCCESS, false},
        {HOST_RUN, "{\"kind\":\"error\",\"body\":{\"reason\":\"future_reason\",\"details\":{}}}\n",
         HOST_EXIT_NORMAL, 1, HOST_RESULT_ERROR, false},
        {HOST_RUN, "{\"kind\":\"succeeded\"}\n", HOST_EXIT_NORMAL, 1, HOST_RESULT_ABNORMAL, false},
        {HOST_RUN, "{\"kind\":\"error\",\"body\":{\"reason\":\"x\",\"details\":{}}}\n",
         HOST_EXIT_NORMAL, 0, HOST_RESULT_ABNORMAL, false},
        {HOST_RUN, "{\"kind\":\"succeeded\"}\n", HOST_EXIT_SIGNAL, 9, HOST_RESULT_ABNORMAL, false},
        {HOST_RUN, "{\"kind\":\"succeeded\"}\n", HOST_EXIT_UNKNOWN, 0, HOST_RESULT_ABNORMAL, false},
        {HOST_SUBMIT, "{\"kind\":\"succeeded\"}\n", HOST_EXIT_NORMAL, 0, HOST_RESULT_ABNORMAL,
         false},
        {HOST_SUBMIT, "{\"kind\":\"succeeded\",\"body\":{\"needs_run\":1}}\n", HOST_EXIT_NORMAL, 0,
         HOST_RESULT_ABNORMAL, false},
        {HOST_SUBMIT,
         "{\"kind\":\"succeeded\",\"body\":{\"needs_run\":true,\"needs_run\":false}}\n",
         HOST_EXIT_NORMAL, 0, HOST_RESULT_ABNORMAL, false},
        {HOST_RUN, "", HOST_EXIT_NORMAL, 0, HOST_RESULT_ABNORMAL, false},
        {HOST_RUN, "{\"kind\":\"succeeded\"}", HOST_EXIT_NORMAL, 0, HOST_RESULT_ABNORMAL, false},
        {HOST_RUN, "{\"kind\":\"succeeded\"}\n\n", HOST_EXIT_NORMAL, 0, HOST_RESULT_ABNORMAL,
         false},
        {HOST_RUN, "{\"kind\":\"succeeded\"}\n{}\n", HOST_EXIT_NORMAL, 0, HOST_RESULT_ABNORMAL,
         false},
        {HOST_RUN, "{\"kind\":\"succeeded\",\"kind\":\"error\"}\n", HOST_EXIT_NORMAL, 0,
         HOST_RESULT_ABNORMAL, false},
        {HOST_RUN, "{\"kind\":\"succeeded\\u0000x\"}\n", HOST_EXIT_NORMAL, 0, HOST_RESULT_ABNORMAL,
         false},
        {HOST_RUN, "{\"kind\":\"error\",\"body\":{\"reason\":4,\"details\":{}}}\n",
         HOST_EXIT_NORMAL, 1, HOST_RESULT_ABNORMAL, false},
        {HOST_RUN, "{\"kind\":\"error\",\"body\":{\"reason\":\"x\",\"details\":[]}}\n",
         HOST_EXIT_NORMAL, 1, HOST_RESULT_ABNORMAL, false},
        {HOST_RUN, "{\"kind\":\"succeeded\",\"extension\":true}\n", HOST_EXIT_NORMAL, 0,
         HOST_RESULT_SUCCESS, false},
        {HOST_RUN, "{\"kind\":\"succeeded\",\"x\":\"\xff\"}\n", HOST_EXIT_NORMAL, 0,
         HOST_RESULT_ABNORMAL, false}};
    struct CMUnitTest tests[sizeof(cases) / sizeof(cases[0])];
    for (size_t i = 0; i < sizeof(tests) / sizeof(tests[0]); ++i)
        tests[i] = (struct CMUnitTest){cases[i].json, result_case, NULL, NULL, (void *)&cases[i]};
    return cmocka_run_group_tests(tests, NULL, NULL);
}
