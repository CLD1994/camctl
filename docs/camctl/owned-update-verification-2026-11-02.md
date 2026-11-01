# camctl 自身更新与 C 接入验证（2026-11-02）

本记录对应[更新接入规格](../architecture/software-update-interface.md)及[实施计划](../superpowers/plans/2026-10-31-camctl-owned-update-api.md)。验证使用 Linux x86_64 开发容器、CPython 3.11.17 和 GCC 13.3.0。更新组件归属、专用 ZIP、稳定维护入口、冻结恢复引擎、C 消费链及同进程显式重启已经进行独立源码复核。

## 已执行的隔离验证

根工作区 `/workspaces/camctl` 在 `23b000b6` 集成阶段复跑以下八个文件。每个文件独立启动 pytest 进程，避免导入替身跨文件共享；均退出 0，共 78 项及 37 个子测试。

```sh
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  apps/camctl/.venv311/bin/python -m pytest -p no:cacheprovider -q \
  apps/camctl/tests/unit/update/<文件名>
```

| 文件 | 实际结果 | 验证对象 |
| --- | --- | --- |
| `test_owned_update_archive.py` | 11 通过 | 受控 ZIP 成员、摘要和内容集合 |
| `test_owned_update_config.py` | 9 通过 | 显式与默认来源、配置存在或缺失快照 |
| `test_owned_update_diagnostics.py` | 1 通过 | 异常编码失败时保留原事实 |
| `test_owned_update_engine.py` | 17 通过 | 受控更新、回滚、可靠完成和错误交付编排 |
| `test_owned_update_publisher.py` | 1 通过 | 公开发布入口的资源完整性及发布顺序 |
| `test_owned_update_router.py` | 12 通过 | 恢复优先、引擎路由、结果汇总及资格 |
| `test_update_transaction.py` | 26 通过，37 个子测试通过 | 恢复记录、路径归属及备份核验规则 |
| `test_update_worker.py` | 1 通过 | 安装子进程继承更新资格 |

测试先经独立静态隔离复核，文件、用户环境、数据库、ZIP/wheel、锁、内存文件描述符及子进程均由受接口约束的替身提供。这里只导入被测源码，没有读取真实发行物或执行实际升级。

根工作区随后完成 13 个维护 Python 文件的 AST 语法检查、`apps/host-demo/scripts/generate_update_protocol.py --check` 及指定 CPython 3.11.17 的 `uv lock --locked --offline` 检查，均退出 0。包资源首次 `--check` 报告缺少新增更新 Schema；从权威来源执行 `apps/camctl/scripts/sync_resources.py` 生成后，再次 `--check` 退出 0。生成对象是被 Git 忽略的源码包资源，没有构建或安装发行物。

## 同进程重启与共享收场

独立复核接受 `e847a6c3` 的显式重启、停止参与者计数及共享构造收场；根工作区以 `306e7fb4` 集成。候选线程未确认结束时保留依赖并阻止重启或维护，后续停止调用继续收场；普通初始化与显式重启共用同一资源边界。新运行周期复用配置和回调，重新检查资格并初始化重试预算，不重送原首次计划。

根工作区直接编译并执行完整 `test_stop.c` 与 `test_writer_stop.c`，编译和运行均退出 0，分别为 30 项和 5 项通过。完整停止入口包含 5 个重启分区，不能再将这 5 项重复累计。其进程、线程创建、等待、管道和文件调用均使用已独立审查的替身；真实同步对象只在当前测试线程中无竞争地使用。

同一集成点核对 8 个 C 源文件的 `-fsyntax-only` 检查，均退出 0。独立复核也确认新增 C 单元已登记到常规 CMake/CTest 的 `unit` 类别，沿统一 `-UNDEBUG` 保留 Release 中的断言；本次没有执行 CMake 或 CTest。根工作区在 `b9d52a0c` 集成旧更新覆盖补充后，单独复跑 engine 文件为 23 项通过，其余七个 Python 文件沿用上表未受影响的证据。

## 失败与更正记录

一次 worker 单元的静态许可遗漏了 argparse 的终端尺寸及 gettext 环境读取。该次 `1 passed` 不计入符合隔离要求的结果；补齐终端和翻译替身后，经复核重新执行通过，根工作区也再次通过。没有操作业务文件、数据库、子进程或设备。

首次 stop 单元编译因保护替身的可变参数宏不满足 `-Wpedantic` 失败，修正宏调用后编译及执行通过；首次失败未执行测试二进制。首次离线锁文件刷新因缺少 pytest 注册表缓存失败，未联网；依赖和摘要保持原样，从项目唯一版本来源同步项目元数据后，`uv lock --locked --offline` 检查退出 0。

`node scripts/check-doc-links.mjs` 在根工作区退出 1，报告部署包 README 模板中三处 `docs/` 链接。它们指向打包时导出的配置参考和公开头文件，在基线 `f18bcac3` 中同样存在；本次未删除链接或放宽检查。接收方 README 的实际导出与包内链接仍需完整包验证。

## 证据边界

没有新增或运行集成测试，没有实际构建 wheel、sdist、更新 ZIP 或完整部署包，没有安装或更新原 Python 环境，没有执行真实数据库迁移、文件恢复、进程组并发、设备或物理断电验证。源码复核及隔离单元不能替代这些真实组合。完整备份、可靠完成及 V9 起点继续沿用[前期事务验证](transactional-update-verification-2026-10-31.md)的契约与未验范围。
