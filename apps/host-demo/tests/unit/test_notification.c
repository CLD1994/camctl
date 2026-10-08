#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include "notification.h"
#ifdef HOST_SHARED_CASES
#include "notification_cases.h"
#endif
static host_notification_stream stream;
static int positions[128], count, errors, limit;
static bool sink(void *ctx, host_notification n) {
    (void)ctx;
    if (count == limit)
        return false;
    positions[count++] = n.position;
    return true;
}
static void diagnostic(void *ctx, host_notification_error error) {
    (void)ctx;
    assert_int_not_equal(error, HOST_NOTIFICATION_OK);
    errors++;
}
static void reset(void) {
    host_notification_reset(&stream);
    count = errors = 0;
    limit = 128;
}
static host_notification_error parse(const char *number, int *p) {
    char data[512];
    int n = snprintf(
        data, sizeof(data),
        "{\"type\":\"motor_control\",\"action_instance_id\":\"12\",\"params\":{\"position\":%s}}\n",
        number);
    host_notification v;
    host_notification_error e = host_notification_parse(&stream, data, (size_t)n, &v);
    if (e == HOST_NOTIFICATION_OK)
        *p = v.position;
    return e;
}
static void numbers(void **state) {
    (void)state;
    int p = 0;
    const struct {
        const char *text;
        int expected;
    } cases[] = {{"-2147483648", INT_MIN},
                 {"2147483647", INT_MAX},
                 {"0", 0},
                 {"-0", 0},
                 {"1.0", 1},
                 {"1e2", 100},
                 {"-12.00e1", -120},
                 {"214748364700e-2", INT_MAX},
                 {"0e999999999999999", 0},
                 {"100000000000000000000000000000e-29", 1}};
    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        assert_int_equal(parse(cases[i].text, &p), HOST_NOTIFICATION_OK);
        assert_int_equal(p, cases[i].expected);
    }
    const char *bad[] = {"2147483648", "-2147483649", "1.0000000000000000001",
                         "0.5",        "true",        "null",
                         "\"1\"",      "1e999999",    "1e-999999",
                         "NaN",        "01",          "1.",
                         "1e"};
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++)
        assert_int_not_equal(parse(bad[i], &p), HOST_NOTIFICATION_OK);
}
static void structure(void **state) {
    (void)state;
    host_notification v;
    const char *bad[] = {
        "",
        "{}\n",
        "[]\n",
        "{\"type\":\"motor_control\",\"type\":\"motor_control\",\"params\":{\"position\":1}}\n",
        "{\"type\":\"motor_control\",\"action_instance_id\":\"01\",\"params\":{\"position\":1}}\n",
        "{\"type\":\"motor_control\",\"action_instance_id\":\"9223372036854775808\",\"params\":{"
        "\"position\":1}}\n",
        "{\"type\":\"motor_control\",\"action_instance_id\":\"1\",\"params\":{\"position\":1,"
        "\"posit\\u0069on\":2}}\n",
        "{\"type\":\"motor_control\",\"action_instance_id\":\"1\",\"params\":{\"position\":1,"
        "\"extra\":1}}\n",
        "{\"type\":\"motor_control\",\"action_instance_id\":\"1\",\"params\":{\"position\":1},"
        "\"extra\":0}\n",
        "{\"type\":\"other\",\"action_instance_id\":\"1\",\"params\":{\"position\":1}}\n",
        "{\"type\":\"motor_control\",\"action_instance_id\":\"\\ud800\",\"params\":{\"position\":1}"
        "}\n",
        "{\"type\":\"motor_control\",\"action_instance_id\":\"\xff\",\"params\":{\"position\":1}}"
        "\n"};
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++)
        assert_int_not_equal(host_notification_parse(&stream, bad[i], strlen(bad[i]), &v),
                             HOST_NOTIFICATION_OK);
    const char nul[] =
        "{\"type\":\"motor_control\",\"action_instance_id\":\"1\",\"params\":{\"position\":1}}\0\n";
    assert_int_not_equal(host_notification_parse(&stream, nul, sizeof(nul) - 1, &v),
                         HOST_NOTIFICATION_OK);
}
static const char good[] =
    "{\"type\":\"motor_control\",\"action_instance_id\":\"1\",\"params\":{\"position\":-12}}\n";
static void split_and_repeat(void **state) {
    (void)state;
    size_t n = strlen(good);
    for (size_t cut = 0; cut <= n; cut++) {
        reset();
        assert_int_equal(host_notification_feed(&stream, good, cut, sink, diagnostic, NULL), cut);
        assert_int_equal(count, cut == n ? 1 : 0);
        assert_int_equal(
            host_notification_feed(&stream, good + cut, n - cut, sink, diagnostic, NULL), n - cut);
        assert_int_equal(count, 1);
        assert_int_equal(positions[0], -12);
    }
    reset();
    host_notification_feed(&stream, good, n, sink, diagnostic, NULL);
    host_notification_feed(&stream, good, n, sink, diagnostic, NULL);
    assert_int_equal(count, 2);
}
static void backpressure(void **state) {
    (void)state;
    reset();
    limit = 1;
    char lines[512];
    snprintf(lines, sizeof(lines), "%s%s%s", good, good, good);
    size_t n = strlen(lines);
    size_t used = host_notification_feed(&stream, lines, n, sink, diagnostic, NULL);
    assert_true(used < n);
    assert_int_equal(count, 1);
    limit = 128;
    assert_int_equal(
        host_notification_feed(&stream, lines + used, n - used, sink, diagnostic, NULL), n - used);
    assert_int_equal(count, 3);
    assert_int_equal(errors, 0);
}
static void exact_line_capacity(void **state) {
    (void)state;
    char line[4097];
    for (size_t length = 4095; length <= 4097; length++) {
        reset();
        memset(line, ' ', sizeof(line));
        memcpy(line, good, strlen(good) - 1);
        line[length - 1] = '\n';
        assert_int_equal(host_notification_feed(&stream, line, length, sink, diagnostic, NULL),
                         length);
        assert_int_equal(count, length <= 4096 ? 1 : 0);
        assert_int_equal(errors, length <= 4096 ? 0 : 1);
        host_notification_feed(&stream, good, strlen(good), sink, diagnostic, NULL);
        assert_int_equal(count, length <= 4096 ? 2 : 1);
    }
}
static void long_and_eof(void **state) {
    (void)state;
    reset();
    char data[4097];
    memset(data, 'x', sizeof(data));
    host_notification_feed(&stream, data, sizeof(data), sink, diagnostic, NULL);
    assert_int_equal(errors, 1);
    host_notification_feed(&stream, "\n", 1, sink, diagnostic, NULL);
    host_notification_feed(&stream, good, strlen(good), sink, diagnostic, NULL);
    assert_int_equal(count, 1);
    host_notification_feed(&stream, "{}", 2, sink, diagnostic, NULL);
    host_notification_eof(&stream, diagnostic, NULL);
    assert_int_equal(errors, 2);
    assert_int_equal(count, 1);
    reset();
    host_notification_feed(&stream, good, strlen(good), sink, diagnostic, NULL);
    host_notification_feed(&stream, "\n{}\n", 4, sink, diagnostic, NULL);
    host_notification_feed(&stream, good, strlen(good), sink, diagnostic, NULL);
    assert_int_equal(count, 2);
    assert_int_equal(errors, 2);
}
#ifdef HOST_SHARED_CASES
static void shared(void **state) {
    (void)state;
    host_notification n;
    for (size_t i = 0; i < sizeof(shared_cases) / sizeof(shared_cases[0]); i++) {
        const shared_case *c = &shared_cases[i];
        host_notification_error e = host_notification_parse(&stream, c->json, strlen(c->json), &n);
        assert_int_equal(e == HOST_NOTIFICATION_OK, c->valid);
        if (c->valid)
            assert_int_equal(n.position, c->position);
    }
}
#endif
int main(void) {
    if (host_notification_init(&stream, 4096))
        return 1;
    const struct CMUnitTest t[] = {cmocka_unit_test(numbers),
                                   cmocka_unit_test(structure),
                                   cmocka_unit_test(split_and_repeat),
                                   cmocka_unit_test(backpressure),
                                   cmocka_unit_test(exact_line_capacity),
                                   cmocka_unit_test(long_and_eof)
#ifdef HOST_SHARED_CASES
                                       ,
                                   cmocka_unit_test(shared)
#endif
    };
    int e = cmocka_run_group_tests(t, NULL, NULL);
    host_notification_destroy(&stream);
    return e;
}
