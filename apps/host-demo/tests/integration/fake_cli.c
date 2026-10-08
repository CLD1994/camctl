#define _GNU_SOURCE
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <pthread.h>
#include <sys/prctl.h>
#include <string.h>
#include <unistd.h>
static void *leftover_thread(void *context) {
    int fd = *(int *)context;
    assert(!prctl(PR_SET_NAME, "tool ) worker", 0, 0, 0));
    assert(write(fd, "r", 1) == 1);
    for (;;) pause();
    return NULL;
}
int main(int argc, char **argv) {
    assert(argc >= 2);
    if (argc > 2 && !strcmp(argv[2], "/hold")) {
        for (;;) pause();
    }
    if (argc > 2 && !strncmp(argv[2], "/leftover-", 10)) {
        assert(argc == 5 && !strcmp(argv[3], "--config"));
        int ready[2];
        assert(!pipe(ready));
        pid_t pid = fork();
        assert(pid >= 0);
        if (!pid) {
            close(ready[0]);
            if (!strcmp(argv[2], "/leftover-thread")) {
                pthread_t thread;
                assert(!pthread_create(&thread, NULL, leftover_thread, &ready[1]));
                pthread_exit(NULL);
            }
            assert(write(ready[1], "r", 1) == 1);
            for (;;) pause();
        }
        close(ready[1]);
        char byte;
        assert(read(ready[0], &byte, 1) == 1);
        close(ready[0]);
        FILE *record = fopen(argv[4], "w");
        assert(record);
        fprintf(record, "%ld\n", (long)pid);
        assert(!fclose(record));
        puts("{\"kind\":\"succeeded\"}");
        return !strcmp(argv[2], "/leftover-error") ? 7 : 0;
    }
    if (argc > 2 && !strcmp(argv[2], "/exit127"))
        return 127;
    char block[4096];
    memset(block, 'd', sizeof(block));
    if (argc > 2 && !strcmp(argv[2], "/overflow")) {
        for (int i = 0; i < 256; i++)
            assert(write(1, block, sizeof(block)) == sizeof(block));
        return 0;
    }
    if (!strcmp(argv[1], "submit")) {
        assert(argc >= 3);
        assert(!strcmp(argv[2], "/中文 path.json"));
        for (int i = 0; i < 256; i++)
            assert(write(2, block, sizeof(block)) == sizeof(block));
        const char *result = "{\"kind\":\"succeeded\",\"body\":{\"needs_run\":true}}\n";
        for (size_t i = 0; i < strlen(result); i++)
            assert(write(1, result + i, 1) == 1);
    } else
        puts("{\"kind\":\"succeeded\"}");
    return 0;
}
