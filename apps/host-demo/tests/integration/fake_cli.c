#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
    assert(argc >= 2);
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
