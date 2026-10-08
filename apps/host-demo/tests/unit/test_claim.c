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
    int syncs;
    host_claim_move_kind failure_kind;
    bool fail_all;
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
static host_claim_move_result move(void *p, const char *name) {
    fake *f = p;
    assert_true(f->listing_complete);
    assert_int_equal(name[0], 'a' + f->attempts);
    f->attempts++;
    if ((f->attempts == 1 || f->fail_all) && f->move_error)
        return (host_claim_move_result){f->failure_kind, f->move_error};
    f->completed++;
    return (host_claim_move_result){HOST_CLAIM_MOVED, 0};
}
static host_claim_sync_result sync_dirs(void *p) {
    ((fake *)p)->syncs++;
    return (host_claim_sync_result){0};
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
    fake f = {.entries = 3, .move_error = EACCES, .failure_kind = HOST_CLAIM_MOVE_FAILED};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.attempts, 3);
    assert_int_equal(f.completed, 2);
    assert_int_equal(f.diagnoses, 1);
}
static void missing_source_continues(void **p) {
    (void)p;
    fake f = {.entries = 3, .move_error = ENOENT, .failure_kind = HOST_CLAIM_SOURCE_CHECK_FAILED};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.attempts, 3);
    assert_int_equal(f.completed, 2);
}
static void unknown_move_continues(void **p) {
    (void)p;
    fake f = {.entries = 3, .move_error = EIO, .failure_kind = HOST_CLAIM_MOVE_UNKNOWN};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.attempts, 3);
    assert_int_equal(f.completed, 2);
}
static void directory_failure_stops(void **p) {
    (void)p;
    fake f = {.entries = 3, .move_error = ENOENT, .health_error = ENOENT,
              .failure_kind = HOST_CLAIM_SOURCE_CHECK_FAILED};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.attempts, 1);
}
static void single_unknown_requires_sync(void **p) {
    (void)p;
    fake f = {.entries = 1, .move_error = EIO, .failure_kind = HOST_CLAIM_MOVE_UNKNOWN};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.syncs, 1);
    assert_int_equal(f.completed, 0);
}
static void all_unknown_require_sync(void **p) {
    (void)p;
    fake f = {.entries = 3, .move_error = EINTR, .failure_kind = HOST_CLAIM_MOVE_UNKNOWN,
              .fail_all = true};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.attempts, 3);
    assert_int_equal(f.syncs, 1);
}
static void unknown_before_directory_failure_requires_sync(void **p) {
    (void)p;
    fake f = {.entries = 3, .move_error = EIO, .failure_kind = HOST_CLAIM_MOVE_UNKNOWN,
              .health_error = EACCES};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.attempts, 1);
    assert_int_equal(f.syncs, 1);
}
static void source_check_failure_has_no_sync(void **p) {
    (void)p;
    fake f = {.entries = 1, .move_error = EIO, .failure_kind = HOST_CLAIM_SOURCE_CHECK_FAILED};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.syncs, 0);
}
static void non_regular_has_no_sync(void **p) {
    (void)p;
    fake f = {.entries = 1, .move_error = EINVAL, .failure_kind = HOST_CLAIM_SOURCE_NOT_REGULAR};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.syncs, 0);
}
static void definite_failure_has_no_sync(void **p) {
    (void)p;
    fake f = {.entries = 1, .move_error = EACCES, .failure_kind = HOST_CLAIM_MOVE_FAILED};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.syncs, 0);
}
static void completed_files_remain(void **p) {
    (void)p;
    fake f = {.entries = 3, .health_error = EACCES};
    host_claim_files(&ops, &f, 4096);
    assert_int_equal(f.completed, 1);
    assert_int_equal(f.attempts, 1);
    assert_int_equal(f.syncs, 1);
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
                                   cmocka_unit_test(completed_files_remain),
                                   cmocka_unit_test(single_unknown_requires_sync),
                                   cmocka_unit_test(all_unknown_require_sync),
                                   cmocka_unit_test(unknown_before_directory_failure_requires_sync),
                                   cmocka_unit_test(source_check_failure_has_no_sync),
                                   cmocka_unit_test(non_regular_has_no_sync),
                                   cmocka_unit_test(definite_failure_has_no_sync)};
    return cmocka_run_group_tests(t, NULL, NULL);
}
