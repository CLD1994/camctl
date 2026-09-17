#include "scheduler.h"
#include <string.h>
void host_schedule_init(host_schedule *s, bool first, uint32_t limit, uint32_t delay) {
    memset(s, 0, sizeof(*s));
    s->first_plan = first;
    s->retry_limit = limit;
    s->delay_ms = delay;
    s->jobs[HOST_RUN].phase = HOST_NORMAL;
}
void host_schedule_need_run(host_schedule *s) {
    s->pending_run = true;
    s->fresh_run = true;
}
void host_schedule_submit(host_schedule *s) { s->jobs[HOST_SUBMIT].phase = HOST_NORMAL; }
bool host_schedule_take(host_schedule *s, host_command c, uint64_t now) {
    host_job *j = &s->jobs[c];
    if (j->phase == HOST_ACTIVE || j->phase == HOST_STARTING)
        return false;
    if (c == HOST_RUN && s->fresh_run)
        j->phase = HOST_NORMAL;
    if (j->phase == HOST_WAIT) {
        if (s->retries_used >= s->retry_limit) {
            j->phase = HOST_IDLE;
            return false;
        }
        if (now < j->due)
            return false;
        s->retries_used++;
    } else if (j->phase != HOST_NORMAL)
        return false;
    j->phase = HOST_STARTING;
    if (c == HOST_RUN) {
        s->pending_run = false;
        s->fresh_run = false;
    }
    return true;
}
void host_schedule_spawn_failed(host_schedule *s, host_command c, uint64_t now) {
    s->jobs[c].phase = HOST_WAIT;
    s->jobs[c].due = now + s->delay_ms;
    if (c == HOST_RUN)
        s->pending_run = true;
}
void host_schedule_spawned(host_schedule *s, host_command c) {
    s->jobs[c].phase = HOST_ACTIVE;
    /* 成功创建后的执行事实可能未知，不能再按“确定未启动”重送首次输入。 */
    if (c == HOST_RUN)
        s->first_plan = false;
}
void host_schedule_finished(host_schedule *s, host_command c, host_result r, uint64_t now) {
    s->jobs[c].phase = HOST_IDLE;
    if (c == HOST_SUBMIT) {
        if (r.kind == HOST_RESULT_SUCCESS && r.needs_run)
            host_schedule_need_run(s);
    } else if (r.kind == HOST_RESULT_ABNORMAL) {
        s->jobs[c].phase = HOST_WAIT;
        s->jobs[c].due = now + s->delay_ms;
    }
}
