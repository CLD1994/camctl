#ifndef CAMCTL_HOST_H
#define CAMCTL_HOST_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

/* 路径字节数不含结尾 NUL。所有路径均为绝对路径。 */
#define CAMCTL_HOST_PATH_MAX 4095
typedef struct camctl_host_config {
    const char *camctl_path;
    const char *ready_path;
    const char *processing_path;
    const char *log_path;
    const char *config_path; /* NULL：由 camctl 使用默认配置来源。 */
    uint32_t retry_limit;
    uint32_t retry_delay_ms;
    size_t plan_capacity;
    size_t stdout_capacity;
    size_t log_queue_capacity;
    size_t log_record_capacity;
    size_t log_file_size;
    uint32_t log_file_count;
} camctl_host_config;

#define CAMCTL_HOST_CONFIG_INIT                                                                    \
    {                                                                                              \
        NULL, NULL, NULL,       NULL,       NULL,     3,                                           \
        5000, 64,   256 * 1024, 256 * 1024, 8 * 1024, 10 * 1024 * 1024,                            \
        3}

/* 初始化一次，成功后持续运行。0 仅表示本地安排成功，失败 -1 并设置 errno。
 * 主程序保留模块子进程的退出记录，不显式忽略 SIGCHLD、不设置 SA_NOCLDWAIT，
 * 不抢先回收模块子进程；模块检查可检查的信号设置，不修改全局处置。 */
int camctl_host_init(const camctl_host_config *config, const char *initial_plan_path);
/* 复制路径；调用方保留完整写入并关闭的文件。0 不代表计划已受理。 */
int camctl_host_submit(const char *plan_path);
/* 主程序决定调用时机，同步逐文件移动并覆盖同名文件，诊断异步记录。 */
void camctl_host_claim(void);

#ifdef __cplusplus
}
#endif
#endif
