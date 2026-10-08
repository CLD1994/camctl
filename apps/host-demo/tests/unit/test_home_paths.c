#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <errno.h>
#include <string.h>
#include "camctl_host.h"

static void fills_missing_paths(void **state) {
    (void)state;
    camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
    camctl_host_paths paths;
    char home[] = "/home/用户 有空格///";
    assert_int_equal(camctl_host_config_set_home_paths(&config, &paths, home), 0);
    home[1] = 'X'; /* 结果不借用 home 的存储。 */
    assert_string_equal(config.camctl_path, "/home/用户 有空格/.camctl/venv/bin/camctl");
    assert_string_equal(config.ready_path, "/home/用户 有空格/.camctl/ready");
    assert_string_equal(config.processing_path, "/home/用户 有空格/.camctl/processing");
    assert_string_equal(config.log_path, "/home/用户 有空格/.camctl/host.log");
    assert_null(config.config_path);
}

static void preserves_explicit_configuration(void **state) {
    (void)state;
    camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
    camctl_host_paths paths;
    config.camctl_path = "/tools/camctl";
    config.processing_path = "/volume/processing";
    config.config_path = "/settings/camctl.toml";
    config.retry_limit = 19;
    assert_int_equal(camctl_host_config_set_home_paths(&config, &paths, "/account"), 0);
    assert_string_equal(config.camctl_path, "/tools/camctl");
    assert_string_equal(config.processing_path, "/volume/processing");
    assert_string_equal(config.config_path, "/settings/camctl.toml");
    assert_int_equal(config.retry_limit, 19);
    assert_string_equal(config.ready_path, "/account/.camctl/ready");
    assert_string_equal(config.log_path, "/account/.camctl/host.log");
}

static void completed_paths_need_no_home(void **state) {
    (void)state;
    camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
    camctl_host_paths paths;
    assert_int_equal(camctl_host_config_set_home_paths(&config, &paths, "/account"), 0);
    /* 再次调用保持原路径，不把已有缓冲区清空。 */
    assert_int_equal(camctl_host_config_set_home_paths(&config, &paths, NULL), 0);
    assert_string_equal(config.camctl_path, "/account/.camctl/venv/bin/camctl");
}

static void invalid_home_preserves_outputs(void **state) {
    (void)state;
    const char *invalid[] = {NULL, "", "relative", "~", "$HOME"};
    for (size_t i = 0; i < sizeof(invalid) / sizeof(invalid[0]); ++i) {
        camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
        camctl_host_paths paths, before;
        memset(&paths, 'x', sizeof(paths));
        memcpy(&before, &paths, sizeof(paths));
        assert_int_equal(camctl_host_config_set_home_paths(&config, &paths, invalid[i]), -1);
        assert_int_equal(errno, EINVAL);
        assert_null(config.camctl_path);
        assert_null(config.ready_path);
        assert_null(config.processing_path);
        assert_null(config.log_path);
        assert_memory_equal(&paths, &before, sizeof(paths));
    }
}

static void null_output_is_rejected(void **state) {
    (void)state;
    camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
    camctl_host_paths paths;
    assert_int_equal(camctl_host_config_set_home_paths(NULL, &paths, "/account"), -1);
    assert_int_equal(errno, EINVAL);
    assert_int_equal(camctl_host_config_set_home_paths(&config, NULL, "/account"), -1);
    assert_int_equal(errno, EINVAL);
}

static void root_home_has_one_separator(void **state) {
    (void)state;
    camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
    camctl_host_paths paths;
    assert_int_equal(camctl_host_config_set_home_paths(&config, &paths, "/"), 0);
    assert_string_equal(config.camctl_path, "/.camctl/venv/bin/camctl");
    assert_string_equal(config.ready_path, "/.camctl/ready");
}

static void path_limit_includes_suffix(void **state) {
    (void)state;
    camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
    camctl_host_paths paths, before;
    /* 最长默认后缀也计入完整路径的字节上限。 */
    const size_t suffix = strlen("/.camctl/venv/bin/camctl");
    char home[CAMCTL_HOST_PATH_MAX + 2];
    memset(home, 'x', sizeof(home));
    home[0] = '/';
    home[CAMCTL_HOST_PATH_MAX - suffix] = '\0';
    assert_int_equal(camctl_host_config_set_home_paths(&config, &paths, home), 0);
    assert_int_equal(strlen(config.camctl_path), CAMCTL_HOST_PATH_MAX);
    config = (camctl_host_config)CAMCTL_HOST_CONFIG_INIT;
    home[CAMCTL_HOST_PATH_MAX - suffix] = 'x';
    home[CAMCTL_HOST_PATH_MAX - suffix + 1] = '\0';
    memcpy(&before, &paths, sizeof(paths));
    assert_int_equal(camctl_host_config_set_home_paths(&config, &paths, home), -1);
    assert_int_equal(errno, ENAMETOOLONG);
    assert_null(config.camctl_path);
    assert_memory_equal(&paths, &before, sizeof(paths));
}

static void later_overflow_does_not_fill_earlier_path(void **state) {
    (void)state;
    camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
    camctl_host_paths paths, before;
    config.camctl_path = "/explicit/camctl";
    config.log_path = "/explicit/log";
    char home[CAMCTL_HOST_PATH_MAX + 1];
    memset(home, 'x', sizeof(home));
    home[0] = '/';
    home[CAMCTL_HOST_PATH_MAX - strlen("/.camctl/ready")] = '\0';
    memset(&paths, 'z', sizeof(paths));
    memcpy(&before, &paths, sizeof(paths));
    assert_int_equal(camctl_host_config_set_home_paths(&config, &paths, home), -1);
    assert_int_equal(errno, ENAMETOOLONG);
    assert_null(config.ready_path);
    assert_null(config.processing_path);
    assert_string_equal(config.camctl_path, "/explicit/camctl");
    assert_memory_equal(&paths, &before, sizeof(paths));
}

static void overlong_home_is_rejected(void **state) {
    (void)state;
    camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
    camctl_host_paths paths;
    char home[CAMCTL_HOST_PATH_MAX + 2];
    memset(home, 'x', sizeof(home));
    home[0] = '/';
    home[sizeof(home) - 1] = '\0';
    assert_int_equal(camctl_host_config_set_home_paths(&config, &paths, home), -1);
    assert_int_equal(errno, ENAMETOOLONG);
    assert_null(config.camctl_path);
}

static void explicit_empty_path_is_not_defaulted(void **state) {
    (void)state;
    camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
    camctl_host_paths paths;
    config.camctl_path = "";
    assert_int_equal(camctl_host_config_set_home_paths(&config, &paths, "/account"), 0);
    assert_string_equal(config.camctl_path, "");
}

int main(void) {
    const struct CMUnitTest tests[] = {
        cmocka_unit_test(fills_missing_paths),
        cmocka_unit_test(preserves_explicit_configuration),
        cmocka_unit_test(completed_paths_need_no_home),
        cmocka_unit_test(invalid_home_preserves_outputs),
        cmocka_unit_test(null_output_is_rejected),
        cmocka_unit_test(root_home_has_one_separator),
        cmocka_unit_test(path_limit_includes_suffix),
        cmocka_unit_test(later_overflow_does_not_fill_earlier_path),
        cmocka_unit_test(overlong_home_is_rejected),
        cmocka_unit_test(explicit_empty_path_is_not_defaulted),
    };
    return cmocka_run_group_tests(tests, NULL, NULL);
}
