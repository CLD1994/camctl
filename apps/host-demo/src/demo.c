#include "camctl_host.h"
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static void usage(FILE *stream) {
    fputs(
        "用法：host-demo --camctl <绝对路径> --ready <目录> --processing <目录> --log <日志路径>\n"
        "                 [--config <camctl配置路径>] [--initial-plan <计划路径>]\n"
        "                 [--retry-limit <次数>] [--retry-delay-ms <毫秒>]\n"
        "终端命令：submit <绝对路径>、claim、logs、help。路径保留空格，不加引号。\n",
        stream);
}
static int number(const char *text, uint32_t *value) {
    char *end;
    errno = 0;
    if (!*text || *text == '-')
        return -1;
    unsigned long long n = strtoull(text, &end, 10);
    if (errno || *end || n > UINT32_MAX)
        return -1;
    *value = (uint32_t)n;
    return 0;
}
static void show_logs(const char *path) {
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK);
    if (fd < 0) {
        printf("日志暂不可读：%s\n", strerror(errno));
        return;
    }
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode)) {
        puts("日志路径不是可读取的普通文件");
        close(fd);
        return;
    }
    /* 显示当前日志末尾最多 64 KiB；读取旧文件描述符不妨碍日志线程轮换。 */
    off_t offset = st.st_size > 65536 ? st.st_size - 65536 : 0;
    char buffer[4096];
    size_t remaining = 65536;
    puts("当前模块日志：");
    while (remaining) {
        size_t count = remaining < sizeof(buffer) ? remaining : sizeof(buffer);
        ssize_t n = pread(fd, buffer, count, offset);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0) {
            if (n < 0)
                printf("读取日志失败：%s\n", strerror(errno));
            break;
        }
        fwrite(buffer, 1, (size_t)n, stdout);
        offset += n;
        remaining -= (size_t)n;
    }
    close(fd);
    puts("日志显示结束");
}
int main(int argc, char **argv) {
    camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
    const char *initial = NULL;
    const struct option options[] = {{"camctl", required_argument, NULL, 'c'},
                                     {"ready", required_argument, NULL, 'r'},
                                     {"processing", required_argument, NULL, 'p'},
                                     {"log", required_argument, NULL, 'l'},
                                     {"config", required_argument, NULL, 'f'},
                                     {"initial-plan", required_argument, NULL, 'i'},
                                     {"retry-limit", required_argument, NULL, 'n'},
                                     {"retry-delay-ms", required_argument, NULL, 'd'},
                                     {"help", no_argument, NULL, 'h'},
                                     {NULL, 0, NULL, 0}};
    int opt;
    while ((opt = getopt_long(argc, argv, "", options, NULL)) != -1) {
        switch (opt) {
        case 'c':
            config.camctl_path = optarg;
            break;
        case 'r':
            config.ready_path = optarg;
            break;
        case 'p':
            config.processing_path = optarg;
            break;
        case 'l':
            config.log_path = optarg;
            break;
        case 'f':
            config.config_path = optarg;
            break;
        case 'i':
            initial = optarg;
            break;
        case 'n':
            if (number(optarg, &config.retry_limit)) {
                usage(stderr);
                return 1;
            }
            break;
        case 'd':
            if (number(optarg, &config.retry_delay_ms)) {
                usage(stderr);
                return 1;
            }
            break;
        case 'h':
            usage(stdout);
            return 0;
        default:
            usage(stderr);
            return 1;
        }
    }
    if (optind != argc || !config.camctl_path || !config.ready_path || !config.processing_path ||
        !config.log_path) {
        usage(stderr);
        return 1;
    }
    if (camctl_host_init(&config, initial)) {
        fprintf(stderr, "模块初始化失败：%s\n", strerror(errno));
        return 1;
    }
    setvbuf(stdout, NULL, _IOLBF, 0);
    puts("模块初始化完成；可输入 submit <绝对路径>、claim、logs 或 help。");
    char line[CAMCTL_HOST_PATH_MAX + 32];
    while (fgets(line, sizeof(line), stdin)) {
        size_t n = strlen(line);
        if (!n || line[n - 1] != '\n') {
            int c;
            while ((c = getchar()) != EOF && c != '\n') {
            }
            puts("命令过长或未以换行结束，本次未接收。");
            continue;
        }
        line[--n] = 0;
        if (n && line[n - 1] == '\r')
            line[n - 1] = 0;
        if (!strncmp(line, "submit ", 7)) {
            if (camctl_host_submit(line + 7))
                printf("路径未接收：%s\n", strerror(errno));
            else
                puts("路径已接收；计划受理与执行结果请查看状态报告。");
        } else if (!strcmp(line, "claim")) {
            camctl_host_claim();
            puts("领取调用已返回；可按主程序流程处理 processing 内的文件。");
        } else if (!strcmp(line, "logs"))
            show_logs(config.log_path);
        else if (!strcmp(line, "help"))
            usage(stdout);
        else if (*line)
            puts("未知命令；输入 help 查看用法。");
    }
    puts("终端输入已结束，模块继续运行；外部终止进程可结束本次演示。");
    for (;;)
        pause();
}
