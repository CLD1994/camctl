#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <errno.h>
#include <stdbool.h>
#include <string.h>
#include "claim.h"
typedef struct {
    int read_error, finish_error, move_error, health_error, begin_error;
    int entries, index, attempts, completed, ended, diagnoses;
    bool listing_complete;
} fake;
static int begin(void *p) { return ((fake *)p)->begin_error; }
static int next(void *p, char name[256]) {
    fake *f = p;
    if (f->index == f->entries)
        return -f->read_error;
    name[0] = (char)('a' + f->index++);
    name[1] = 0;
    return 1;
}
static int finish(void *p) {
    fake *f = p;
    f->listing_complete = true;
    return f->finish_error;
}
static int healthy(void *p) {
    fake *f = p;
    return f->attempts ? f->health_error : 0;
}
static int move(void *p, const char *name) {
    fake *f = p;
    assert_true(f->listing_complete);
    assert_int_equal(name[0], 'a' + f->attempts);
    f->attempts++;
    if (f->attempts == 1 && f->move_error)
        return f->move_error;
    f->completed++;
    return 0;
}
static int sync_dirs(void *p) {
    (void)p;
    return 0;
}
static void end(void *p) { ((fake *)p)->ended++; }
static void diagnose(void *p, const char *event, const char *name, int e) {
    (void)event;
    (void)name;
    if (e)
        ((fake *)p)->diagnoses++;
}
static const host_claim_ops ops = {begin, next, finish, healthy, move, sync_dirs, end, diagnose};
static void success(void **p) {
    (void)p;
    fake f = {.entries = 3};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.completed, 3);
    assert_int_equal(f.ended, 1);
}
static void enumeration_failure(void **p) {
    (void)p;
    fake f = {.entries = 2, .read_error = EIO};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.attempts, 0);
    assert_int_equal(f.diagnoses, 1);
    assert_int_equal(f.ended, 1);
}
static void listing_close_failure(void **p) {
    (void)p;
    fake f = {.entries = 2, .finish_error = EIO};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.attempts, 0);
    assert_int_equal(f.diagnoses, 1);
}
static void listing_limit(void **p) {
    (void)p;
    fake f = {.entries = 4};
    host_claim_files(&ops, &f, 1);
    assert_int_equal(f.attempts, 0);
    assert_int_equal(f.diagnoses, 1);
}
static void file_failure_continues(void **p) {
    (void)p;
    fake f = {.entries = 3, .move_error = EACCES};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.attempts, 3);
    assert_int_equal(f.completed, 2);
    assert_int_equal(f.diagnoses, 1);
}
static void missing_source_continues(void **p) {
    (void)p;
    fake f = {.entries = 3, .move_error = ENOENT};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.attempts, 3);
    assert_int_equal(f.completed, 2);
}
static void unknown_move_continues(void **p) {
    (void)p;
    fake f = {.entries = 3, .move_error = EIO};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.attempts, 3);
    assert_int_equal(f.completed, 2);
}
static void directory_failure_stops(void **p) {
    (void)p;
    fake f = {.entries = 3, .move_error = ENOENT, .health_error = ENOENT};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.attempts, 1);
}
static void completed_files_remain(void **p) {
    (void)p;
    fake f = {.entries = 3, .health_error = EACCES};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.completed, 1);
    assert_int_equal(f.attempts, 1);
}
int main(void) {
    const struct CMUnitTest t[] = {cmocka_unit_test(success),
                                   cmocka_unit_test(enumeration_failure),
                                   cmocka_unit_test(listing_close_failure),
                                   cmocka_unit_test(listing_limit),
                                   cmocka_unit_test(file_failure_continues),
                                   cmocka_unit_test(missing_source_continues),
                                   cmocka_unit_test(unknown_move_continues),
                                   cmocka_unit_test(directory_failure_stops),
                                   cmocka_unit_test(completed_files_remain)};
    return cmocka_run_group_tests(t, NULL, NULL);
}
