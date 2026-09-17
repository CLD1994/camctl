#ifndef HOST_SCHEDULER_H
#define HOST_SCHEDULER_H
#include "result.h"
#include <stdint.h>
typedef enum { HOST_IDLE, HOST_NORMAL, HOST_WAIT, HOST_ACTIVE, HOST_STARTING } host_phase;
typedef struct {
    host_phase phase;
    uint64_t due;
} host_job;
typedef struct {
    host_job jobs[2];
    uint32_t retries_used, retry_limit, delay_ms;
    bool pending_run;
    bool fresh_run;
    bool first_plan;
} host_schedule;
void host_schedule_init(host_schedule *s, bool first_plan, uint32_t limit, uint32_t delay);
void host_schedule_need_run(host_schedule *s);
void host_schedule_submit(host_schedule *s);
bool host_schedule_take(host_schedule *s, host_command command, uint64_t now);
void host_schedule_spawn_failed(host_schedule *s, host_command command, uint64_t now);
void host_schedule_spawned(host_schedule *s, host_command command);
void host_schedule_finished(host_schedule *s, host_command command, host_result result,
                            uint64_t now);
#endif
