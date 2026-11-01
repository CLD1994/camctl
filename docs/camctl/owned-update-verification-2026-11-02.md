# camctl 自身更新与 C 接入验证（2026-11-02）

本记录对应[更新接入规格](../architecture/software-update-interface.md)及[实施计划](../superpowers/plans/2026-10-31-camctl-owned-update-api.md)。验证使用 Linux x86_64 开发容器、CPython 3.11.17 和 GCC 13.3.0。更新组件归属、专用 ZIP、稳定维护入口、冻结恢复引擎、C 消费链及同进程显式重启已经进行独立源码复核。

## 已执行的隔离验证

根工作区 `/workspaces/camctl` 在集成后分别复跑以下八个文件。每个文件独立启动 pytest 进程，避免导入替身跨文件共享。初次 `23b000b6` 阶段为 78 项及 37 个子测试；补齐覆盖后，在 `15cbe8c1` 重跑受影响的 engine、router 和 diagnostics 文件，其余沿用未受影响的结果。下表汇总最终有效结果，均退出 0，共 95 项及 37 个子测试。

```sh
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  apps/camctl/.venv311/bin/python -m pytest -p no:cacheprovider -q \
  apps/camctl/tests/unit/update/<文件名>
```

| 文件 | 实际结果 | 验证对象 |
| --- | --- | --- |
| `test_owned_update_archive.py` | 11 通过 | 受控 ZIP 成员、摘要和内容集合 |
| `test_owned_update_config.py` | 9 通过 | 显式与默认来源、配置存在或缺失快照 |
| `test_owned_update_diagnostics.py` | 2 通过 | 异常编码失败或缺失时保留原事实及异常组 |
| `test_owned_update_engine.py` | 30 通过 | 受控更新、回滚、可靠完成、配置消费和错误交付编排 |
| `test_owned_update_publisher.py` | 1 通过 | 公开发布入口的资源完整性及发布顺序 |
| `test_owned_update_router.py` | 15 通过 | 恢复优先、引擎路由、非法记录拒绝、结果汇总及资格 |
| `test_update_transaction.py` | 26 通过，37 个子测试通过 | 恢复记录、路径归属及备份核验规则 |
| `test_update_worker.py` | 1 通过 | 安装子进程继承更新资格 |

测试先经独立静态隔离复核，文件、用户环境、数据库、ZIP/wheel、锁、内存文件描述符及子进程均由受接口约束的替身提供。这里只导入被测源码，没有读取真实发行物或执行实际升级。

根工作区随后完成 13 个维护 Python 文件的 AST 语法检查、`apps/host-demo/scripts/generate_update_protocol.py --check` 及指定 CPython 3.11.17 的 `uv lock --locked --offline` 检查，均退出 0。包资源首次 `--check` 报告缺少新增更新 Schema；从权威来源执行 `apps/camctl/scripts/sync_resources.py` 生成后，再次 `--check` 退出 0。生成对象是被 Git 忽略的源码包资源，没有构建或安装发行物。

## 同进程重启与共享收场

独立复核接受 `e847a6c3` 的显式重启、停止参与者计数及共享构造收场；根工作区以 `306e7fb4` 集成。候选线程未确认结束时保留依赖并阻止重启或维护，后续停止调用继续收场；普通初始化与显式重启共用同一资源边界。新运行周期复用配置和回调，重新检查资格并初始化重试预算，不重送原首次计划。

根工作区直接编译并执行完整 `test_stop.c` 与 `test_writer_stop.c`，编译和运行均退出 0，分别为 30 项和 5 项通过。完整停止入口包含 5 个重启分区，不能再将这 5 项重复累计。其进程、线程创建、等待、管道和文件调用均使用已独立审查的替身；真实同步对象只在当前测试线程中无竞争地使用。

同一集成点核对 8 个 C 源文件的 `-fsyntax-only` 检查，均退出 0。独立复核也确认新增 C 单元已登记到常规 CMake/CTest 的 `unit` 类别，沿统一 `-UNDEBUG` 保留 Release 中的断言；本次没有执行 CMake 或 CTest。根工作区在 `b9d52a0c` 集成旧更新覆盖补充后，单独复跑 engine 文件为 23 项通过，其余七个 Python 文件沿用上表未受影响的证据。

在 `15cbe8c1` 集成参数覆盖后，根工作区直接编译并执行 `test_spawn.c`，9 项通过；重新编译并执行 `test_maintenance_isolated.c`，退出 0。这些测试用替身验证显式配置与包参数转发、相对配置路径拒绝及结果消费，没有实际启动维护进程。结果解析、维护通道及演示成败的三个纯内存入口此前也已分别编译、执行并通过，后续未受影响。C 的计数入口共 44 项通过，三个纯内存入口的断言不另折算为用例数量。

## 覆盖归属与源码复核

维护行为从稳定生产入口验证；分区已经按输入、预期和失败事实逐项核对，重复部署更新脚本及其专属测试在 `535b7e5f` 移除。维护实现、构建资源、发布器和事务规则只在 camctl 组件维护。

| 行为分区 | 当前覆盖入口 |
| --- | --- |
| 备份失败、修改意图发布不确定、提交补同步、自动回滚、解锁或最终收据再次失败 | engine 的更新编排及正式记录消费 |
| 非法恢复记录、资格锁失败、旧恢复先于新包、终态不再约束旧配置 | router 的公开执行入口 |
| 默认与显式配置、缺失快照、旧数据库及客户端和媒体绑定 | config、engine 的原解释器探测及恢复入口 |
| 记录跨事务身份、creator 追加、路径或文件身份冲突先于控制材料创建 | engine 的正式记录读取及 transaction 路径守卫 |
| 原始异常图、cause、附加事实、异常组、编码器与输出通道失败 | engine 的实际诊断消费者及 diagnostics 后备编码 |
| C 参数转发、相对路径拒绝、机器结果与退出状态、显式重启及候选收场 | spawn、maintenance、result、demo、stop 及 writer_stop 单元 |

固定源码由独立 `gpt-6.1-sol` 评审。复核沿 ZIP 输入、恢复路由、冻结引擎、资格锁、事务执行、公共结果和 C 消费链完成；同进程重启另核对候选资源及所有停止观察者。未发现剩余确定阻断。源码接受与隔离验证不包含下面列出的真实组合。

## C 单元复现命令

从仓库根目录执行。下面的编译与运行命令对应上述已静态复核的替身边界，普通组件集成入口不在本次执行范围内。

```sh
cc -std=c11 -D_GNU_SOURCE -DUNIT_TESTING_DEBUG -DHAVE_SIGNAL_H -DHAVE_STRINGS_H -DHAVE_INTTYPES_H -DHAVE_STRSIGNAL -DHAVE_SIGLONGJMP -DHAVE_VSNPRINTF -DHAVE_SNPRINTF -DHAVE_GCC_THREAD_LOCAL_STORAGE -Wall -Wextra -Wpedantic -Werror -Wno-unused-function -Iapps/host-demo/include -Iapps/host-demo/src -Iapps/host-demo/vendor/yyjson -Iapps/host-demo/vendor/cmocka -pthread apps/host-demo/tests/unit/test_stop.c apps/host-demo/src/{callbacks,notification,callback_queue,result,scheduler,log_queue,process,process_group,claim,config,input,logger,diagnostic,linux_rename,claim_posix,update_result,maintenance}.c apps/host-demo/vendor/yyjson/yyjson.c apps/host-demo/vendor/cmocka/cmocka.c  -Wl,--wrap=host_log,--wrap=write,--wrap=read,--wrap=close,--wrap=clock_gettime,--wrap=getcwd,--wrap=host_group_scan_reset,--wrap=host_child_close_notifications,--wrap=host_child_collect,--wrap=host_child_done,--wrap=host_child_request_stop,--wrap=host_child_spawn,--wrap=host_result_parse,--wrap=host_diagnostic_busy,--wrap=host_diagnostic_request,--wrap=host_diagnostic_dispatch_pending,--wrap=host_diagnostic_close,--wrap=host_logger_close,--wrap=host_claim_posix,--wrap=poll,--wrap=pthread_join,--wrap=pthread_cond_wait,--wrap=pthread_cond_init,--wrap=host_maintenance_run,--wrap=host_maintenance_spawn,--wrap=pthread_create,--wrap=pipe2,--wrap=host_pipe,--wrap=host_pipe_take_close_errors,--wrap=host_child_prepare_notifications,--wrap=host_check_reaping_contract,--wrap=fcntl -o /tmp/camctl-root-stop-all-unit
env -i CMOCKA_MESSAGE_OUTPUT=STDOUT /tmp/camctl-root-stop-all-unit
cc -std=c11 -D_GNU_SOURCE -DUNIT_TESTING_DEBUG -DHAVE_SIGNAL_H -DHAVE_STRINGS_H -DHAVE_INTTYPES_H -DHAVE_STRSIGNAL -DHAVE_SIGLONGJMP -DHAVE_VSNPRINTF -DHAVE_SNPRINTF -DHAVE_GCC_THREAD_LOCAL_STORAGE -Wall -Wextra -Wpedantic -Werror -Wno-unused-function -Iapps/host-demo/include -Iapps/host-demo/src -Iapps/host-demo/vendor/yyjson -Iapps/host-demo/vendor/cmocka -pthread apps/host-demo/tests/unit/test_writer_stop.c apps/host-demo/src/{callbacks,notification,callback_queue,result,scheduler,log_queue,process,process_group,claim,config,input,logger,diagnostic,linux_rename,claim_posix,update_result,maintenance}.c apps/host-demo/vendor/yyjson/yyjson.c apps/host-demo/vendor/cmocka/cmocka.c  -Wl,--wrap=pthread_create,--wrap=pthread_join,--wrap=open,--wrap=fstat,--wrap=unlink,--wrap=write,--wrap=close -o /tmp/camctl-root-writer-stop-unit
env -i CMOCKA_MESSAGE_OUTPUT=STDOUT /tmp/camctl-root-writer-stop-unit
```

## 失败与更正记录

一次 worker 单元的静态许可遗漏了 argparse 的终端尺寸及 gettext 环境读取。该次 `1 passed` 不计入符合隔离要求的结果；补齐终端和翻译替身后，经复核重新执行通过，根工作区也再次通过。没有操作业务文件、数据库、子进程或设备。

首次 stop 单元编译因保护替身的可变参数宏不满足 `-Wpedantic` 失败，修正宏调用后编译及执行通过；首次失败未执行测试二进制。首次离线锁文件刷新因缺少 pytest 注册表缓存失败，未联网；依赖和摘要保持原样，从项目唯一版本来源同步项目元数据后，`uv lock --locked --offline` 检查退出 0。

`node scripts/check-doc-links.mjs` 在根工作区退出 1，报告部署包 README 模板中三处 `docs/` 链接。它们指向打包时导出的配置参考和公开头文件，在基线 `f18bcac3` 中同样存在；本次未删除链接或放宽检查。接收方 README 的实际导出与包内链接仍需完整包验证。

## 证据边界

没有新增或运行集成测试，没有实际构建 wheel、sdist、更新 ZIP 或完整部署包，没有安装或更新原 Python 环境，没有执行真实数据库迁移、文件恢复、进程组并发、设备或物理断电验证。源码复核及隔离单元不能替代这些真实组合。完整备份、可靠完成及 V9 起点继续沿用[前期事务验证](transactional-update-verification-2026-10-31.md)的契约与未验范围。
