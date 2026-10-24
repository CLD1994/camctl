# apps/camctl/tests 运行指南

本目录包含 camctl 的单元测试（`unit/`）和组件集成测试（`integration/`，按业务模块分目录）。仓库根的 `tests/integration/` 是另一层：跨组件集成测试（真实 CLI 进程、客户端与 C 主程序的组合），与本目录分开维护。本文件只覆盖本目录；跑跨组件测试请到仓库根并阅读 `tests/integration/README.md`。

测试设计与分类遵守[共享测试规则](../../../.agents/rules/testing.md)；本文件补充 camctl 组件的运行命令和局部限制。

## 怎么跑

所有命令都从仓库根目录执行，用 uv 管理的 Python 3.11（部署规格版本）：

```bash
# 单元测试（约 10 秒）
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit -q

# 组件集成测试：按目录逐个跑，不要合并成一条命令
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/reporting -q
```

当前集成目录由 `python scripts/run-integration.py --scope camctl --list` 从实际源码发现，不另行维护目录清单。完整回归可以使用[隔离并行入口](../../../scripts/README.md#隔离并行集成测试)，默认覆盖全部目录与容量测试；`--target` 用于窄范围验证，不能替代完整回归。

不要用系统 Python 直接跑：系统解释器缺少测试依赖（如 `referencing`），SQLite 版本也不满足运行条件。

## 运行边界与结果记录

**1. 集成测试当前默认分目录运行。** 集成测试模块会在导入时调用 `register_*_guards()`，这些调用修改同一进程内的 `NAMED_GUARDS` 注册表；不同模块注册同名守卫时会覆盖先前值。合并多个目录可能因此改变用例执行时的全局状态，所以全量回归按目录分别启动 pytest。这个默认安排规避已知的进程内副作用，不代表已查明某次长时间停滞的根因。

**2. 同一工作区的集成 pytest 顺序运行。** 每次集成测试启动时，`integration/conftest.py` 都会同步包资源到同一个 `src/camctl/_resources/` 目录。多个进程同时使用该工作区时，资源写入和另一进程读取可能重叠；在资源同步路径隔离前，同一工作区一次只运行一个集成 pytest 进程。不同工作区或独立资源发布物是否能并行，按共享文件、工具环境和其他实际边界判断；仓库的隔离并行入口为每个任务冻结独立完整源码副本，隔离该资源目录、临时根与工具写入；其具体边界见[入口说明](../../../scripts/README.md#隔离并行集成测试)。直接启动的测试仍须自行核对这些边界。单元测试不主动执行这条同步，但用例或其他进程若读取该目录，仍须与同步写入协调。

**3. 保留可追踪的执行记录。** 每次验证都记录命令、工作目录、Python 版本、执行会话、日志和退出码。前台运行便于查看和控制，但本身不构成隔离条件；后台运行时保留进程会话并将输出写入日志。`pytest … | tail` 中的 `tail` 默认等上游结束后才打印末尾行，因此运行期间可能看不到进度；需要持续观察输出时，将输出重定向到文件并用 `tail -f` 查看新增内容。使用隔离并行入口时，直接查看 `summary.json` 与任务的 `progress.json` 可取得 PID、当前节点与阶段；这些状态由执行钩子写入，不依赖日志是否产生新行。

## 历史时长参考

下表是此前记录的单目录粗略耗时；原记录未保留日期、环境、完整命令或计时口径，只供预留时间时参考，不能作为性能目标或挂起阈值。无输出或超过表中时间都不能单独证明测试已挂起，应查看本次执行日志、进程状态和退出结果。

目录|历史粗估|说明
-|-|-
unit|约 10 秒|历史参考
outputs|2 分钟|文件 IO 量最大
host_files|1 分钟|真实文件操作
bootstrap|1.5—2 分钟|含真实进程会话
acceptance|40 秒
其余目录|数秒到半分钟

## Windows 开发机历史环境现象与诊断

以下只记录 Windows 开发机上单元全量或集成长跑曾出现的**随机位置 setup 错误**（例如 `WinError 10055` 系统套接字缓冲区不足，表现为 asyncio 事件循环创建失败或 `TestRealWorkerProcess` setup 报错）。这是特定环境的历史现象，不作为 Linux 容器要求测试串行的依据；失败位置变化、用例单跑结果或旧版本对照都只是诊断线索，不能单独决定归因。

故障再次出现时，按本次失败逐项记录与核对：

- 记录失败用例和阶段、命令、操作系统与解释器版本、完整日志或堆栈，以及同期可观察到的资源和进程情况；
- 需要比较单用例或旧代码时，把它们作为补充证据。只有证据能说明环境条件与失败相关时，才将该次失败归因于环境；失败稳定或随机都不能单独定性；
- 只处理由本次执行会话持有且已确认残留的进程。不得按进程名批量终止其他 Python 进程，也不得在共享工作区用 `git stash` 做旧代码对照；需要对照时使用独立快照或工作区。

完整目标范围有一次带日志和退出码的通过结果后即可记录通过，不强制再跑第二轮。仍有未解决的资源错误时，针对失败用例或相关范围复测并保留新证据。

## 写测试时容易踩的点

- **集成测试的守卫要显式注册。** 事件守卫（`register_*_guards()`）不经 import 链自动触发。新文件按所在目录的惯例补齐：outputs 侧常见 `register_operation_guards()` + `register_capture_guards()`；capture 侧常见 `register_outputs_guards()`。漏注册时写入类用例会以"未接入具名校验"失败。参考同目录既有测试文件的开头。
- **单元测试不启动真实线程或子进程**，不访问真实文件系统；线程、事件循环与进程组合放集成测试。
- **测试目录带 `__init__.py`**，同目录模块（如 fixtures）靠 pytest 的 prepend 导入。
- **替身受真实接口约束**（stub/fake/mock 按真实协作方的契约编写），具体要求见[共享测试规则](../../../.agents/rules/testing.md)。

## 环境备忘

- 包资源由 `scripts/sync_resources.py` 生成（`integration/conftest.py` 在 pytest 启动时自动执行），`src/camctl/_resources/` 不入库。
- 会话锁在 Windows 上用 msvcrt 字节范围锁模拟目标部署的 flock（双后端同一契约）；目标机联调时需复核。
- 涉及 C 主程序（host-demo）的组合测试在仓库根 `tests/integration/`，POSIX 模块需按部署验证裁决在 WSL 构建。
