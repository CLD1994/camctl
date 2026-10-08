#define _GNU_SOURCE
#include "camctl_host.h"
#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
/* 参数：camctl config ready processing log initial-or-- [hold]。
 * stdout每行只记录初始化、回调开始/返回或本地submit结果。
 * stdin：release、submit <绝对路径>、claim、exit。hold只延迟回调返回。 */
static pthread_mutex_t gate_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t gate_ready = PTHREAD_COND_INITIALIZER;
static bool held;
static size_t sequence;
static void motor(int position) {
  pthread_mutex_lock(&gate_mutex);
  size_t index = ++sequence;
  printf("{\"event\":\"callback\",\"sequence\":%zu,\"position\":%d}\n", index,
         position);
  fflush(stdout);
  while (held)
    pthread_cond_wait(&gate_ready, &gate_mutex);
  pthread_mutex_unlock(&gate_mutex);
  printf("{\"event\":\"callback_returned\",\"sequence\":%zu,\"position\":%d}\n",
         index, position);
  fflush(stdout);
}
int main(int argc, char **argv) {
  if (argc != 7 && argc != 8)
    return 2;
  setvbuf(stdout, NULL, _IOLBF, 0);
  held = argc == 8 && !strcmp(argv[7], "hold");
  camctl_host_config c = CAMCTL_HOST_CONFIG_INIT;
  c.camctl_path = argv[1];
  c.config_path = argv[2];
  c.ready_path = argv[3];
  c.processing_path = argv[4];
  c.log_path = argv[5];
  const char *initial = !strcmp(argv[6], "-") ? NULL : argv[6];
  if (camctl_host_register_motor_control_callback(motor) ||
      camctl_host_init(&c, initial)) {
    fprintf(stderr, "init errno=%d\n", errno);
    return 1;
  }
  puts("{\"event\":\"initialized\"}");
  char line[CAMCTL_HOST_PATH_MAX + 32];
  while (fgets(line, sizeof(line), stdin)) {
    line[strcspn(line, "\n")] = 0;
    if (!strcmp(line, "release")) {
      pthread_mutex_lock(&gate_mutex);
      held = false;
      pthread_cond_broadcast(&gate_ready);
      pthread_mutex_unlock(&gate_mutex);
    } else if (!strncmp(line, "submit ", 7)) {
      int rc = camctl_host_submit(line + 7), error = rc ? errno : 0;
      printf("{\"event\":\"submit\",\"result\":%d,\"errno\":%d}\n", rc, error);
    } else if (!strcmp(line, "claim"))
      camctl_host_claim();
    else if (!strcmp(line, "exit"))
      return 0;
    else
      return 2;
  }
  return 0;
}
