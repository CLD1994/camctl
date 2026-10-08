#ifndef CAMCTL_HOST_H
#define CAMCTL_HOST_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 路径字节数不含结尾 NUL。所有路径均为绝对路径。 */
#define CAMCTL_HOST_PATH_MAX 4095

/* 配置默认值；调用方可在初始化前覆盖对应结构体成员。 */
#define CAMCTL_HOST_RETRY_LIMIT_DEFAULT 3
#define CAMCTL_HOST_RETRY_DELAY_MS_DEFAULT 5000
#define CAMCTL_HOST_PLAN_CAPACITY_DEFAULT 64
#define CAMCTL_HOST_STDOUT_CAPACITY_DEFAULT (256 * 1024)
#define CAMCTL_HOST_LOG_QUEUE_CAPACITY_DEFAULT (256 * 1024)
#define CAMCTL_HOST_LOG_RECORD_CAPACITY_DEFAULT (8 * 1024)
#define CAMCTL_HOST_LOG_FILE_SIZE_DEFAULT (10 * 1024 * 1024)
#define CAMCTL_HOST_LOG_FILE_COUNT_DEFAULT 3
#define CAMCTL_HOST_NOTIFICATION_LINE_DEFAULT 4096
#define CAMCTL_HOST_NOTIFICATION_QUEUE_DEFAULT 64

/* 通知容量上限。 */
#define CAMCTL_HOST_NOTIFICATION_LINE_MAX 65536
#define CAMCTL_HOST_NOTIFICATION_QUEUE_MAX 4096

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
    /* 包含 LF；正值，不超过 CAMCTL_HOST_NOTIFICATION_LINE_MAX。 */
    size_t notification_line_capacity;
    /* 待调用记录；正值，不超过 CAMCTL_HOST_NOTIFICATION_QUEUE_MAX。 */
    size_t notification_queue_capacity;
} camctl_host_config;

/* 路径补齐函数使用的调用方缓冲区；保持有效直至 camctl_host_init 返回。
 * config 中生成的指针指向该对象，不能通过复制该对象来迁移指针。 */
typedef struct camctl_host_paths {
    char camctl_path[CAMCTL_HOST_PATH_MAX + 1];
    char ready_path[CAMCTL_HOST_PATH_MAX + 1];
    char processing_path[CAMCTL_HOST_PATH_MAX + 1];
    char log_path[CAMCTL_HOST_PATH_MAX + 1];
} camctl_host_paths;

/* host 解析参数后调用；位置单位和业务范围由主程序定义。 */
typedef void (*camctl_host_motor_control_callback)(int position);

#define CAMCTL_HOST_CONFIG_INIT                                                                   \
    {NULL,                                                                                        \
     NULL,                                                                                        \
     NULL,                                                                                        \
     NULL,                                                                                        \
     NULL,                                                                                        \
     CAMCTL_HOST_RETRY_LIMIT_DEFAULT,                                                             \
     CAMCTL_HOST_RETRY_DELAY_MS_DEFAULT,                                                          \
     CAMCTL_HOST_PLAN_CAPACITY_DEFAULT,                                                           \
     CAMCTL_HOST_STDOUT_CAPACITY_DEFAULT,                                                         \
     CAMCTL_HOST_LOG_QUEUE_CAPACITY_DEFAULT,                                                      \
     CAMCTL_HOST_LOG_RECORD_CAPACITY_DEFAULT,                                                     \
     CAMCTL_HOST_LOG_FILE_SIZE_DEFAULT,                                                           \
     CAMCTL_HOST_LOG_FILE_COUNT_DEFAULT,                                                          \
     CAMCTL_HOST_NOTIFICATION_LINE_DEFAULT,                                                       \
     CAMCTL_HOST_NOTIFICATION_QUEUE_DEFAULT}

/* 以绝对 home 补齐四个为 NULL 的必填路径；已指定路径及其他配置保持。
 * 不读取环境、不创建目录；config_path 保持 NULL 时由 CLI 选择默认配置。
 * 成功返回 0；失败返回 -1 并设置 errno，config 和 paths 均保持原值。
 * 无需补齐时允许 home 为 NULL；空／相对 home 为 EINVAL，路径超限为 ENAMETOOLONG。 */
int camctl_host_config_set_home_paths(camctl_host_config *config, camctl_host_paths *paths,
                                     const char *home);

/* 初始化前注册；回调在专用线程调用。成功返回 0，失败返回 -1 并设置 errno。
 * NULL 为 EINVAL，重复注册为 EALREADY，初始化成功后注册为 EBUSY。 */
int camctl_host_register_motor_control_callback(camctl_host_motor_control_callback callback);

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
