#ifndef HOST_RESULT_H
#define HOST_RESULT_H
#include <stdbool.h>
#include <stddef.h>
typedef enum { HOST_RUN, HOST_SUBMIT } host_command;
typedef enum { HOST_EXIT_NORMAL, HOST_EXIT_SIGNAL, HOST_EXIT_UNKNOWN } host_exit;
typedef enum { HOST_RESULT_ABNORMAL, HOST_RESULT_SUCCESS, HOST_RESULT_ERROR } host_result_kind;
typedef struct {
    host_result_kind kind;
    bool needs_run;
} host_result;
host_result host_result_parse(host_command command, const char *data, size_t len,
                              host_exit termination, int code);
#endif
