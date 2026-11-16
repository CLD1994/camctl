# 内存预算测试

## 验收目标

camctl 主进程及其启动的全部后代进程共享固定预算：内存与交换空间的合计峰值默认不超过 `2GiB`，即 2,147,483,648 字节。合法数据的累计数量增加时，程序仍须在预算内完整处理数据；处理时间和磁盘使用可以随数据量增长。严格无交换运行能力单独验收。

默认值和容量解析由测试侧支持模块维护。预算通过测试参数配置，不进入生产配置。直接 pytest 与隔离集成运行器使用相同参数与解析入口：

- `--camctl-memory-limit` 指定预算，默认值由支持模块提供。
- `--camctl-memory-cgroup-root` 指定已经委派给当前测试用户的资源组目录；不指定时探测当前控制器位置。该目录必须允许创建独立叶组并读写所需控制文件。

容量采用正整数紧跟明确单位的写法，例如 `2GiB`、`1536MiB` 或 `2147483648B`，支持 `B`、`KiB`、`MiB`、`GiB`、`KB`、`MB`、`GB`。预算对应一次被测运行及其后代；并行测试不会平分单实例预算，其他实例和测试准备不混入该实例计量。

## 执行与计量范围

测试在执行生产入口前，将被测进程纳入新建的专属 Linux 资源组。所有后代继承该范围，包括报告进程和外部工具。测试运行器、准备器、客户端构建及组外业务断言不属于被测程序。组件测试原本直接在 pytest 内调用生产函数时，容量验收须另建独立进程执行边界，不能用整个 pytest 的内存代替 camctl 的内存。

按内核资源组口径统计实际内存，覆盖 Python、原生分配和归属该组的文件缓存；共享页按内核归属记账，不能按进程 RSS 简单相加。组外准备可能影响缓存归属，测量须记录输入准备和缓存条件，不声称计入每个被测进程访问过的全部共享缓存。

从启动到全部后代收场均受同一预算约束。记录实际生效的内存限额、合计限额、内存峰值、合计峰值、内存不足及触限事件、退出结果和独立业务核对结果。合计峰值来自内核的同一计数器，不能将发生在不同时刻的内存峰值与交换峰值相加。主进程先退出也不能丢失子进程的内存不足事件。

启动前持久保存本次专属资源组的身份及待完成收据。中断测试可能将 CLI 与测试侧启动代理一起强制杀死，外层 pytest 仍负责核对待完成收据、读取资源组最终证据并清理本次后代，不能仅依赖启动代理的退出处理。

预算启动工具不代替程序处理正常取消。原测试向主进程发送终止信号时，仍由主进程自行通知和收场其后代；测试工具在超时或明确强杀后的最终清理阶段处理遗留进程。使用默认部署路径的测试仍通过实际默认路径启动。只用于观察 host 错误处理、并不执行 camctl 的测试脚本在组外运行。

`tracemalloc` 用于需要定位 Python 对象分配来源的诊断，不能作为整个进程组的资源预算验收。有限规模的通过结果只证明已执行负载；测试还须选择跨分页、缓存容量和累计历史的输入，并由处理边界的有限存活与容量检查共同发现随数据总量积累的问题。

## 判定与环境要求

| 运行情况 | 测试结果 |
| --- | --- |
| 内存与交换空间合计峰值符合配置、没有内存不足、全部业务结果完整 | 对本次实际负载的合计预算验收通过。 |
| 合计峰值超出预算、内存不足终止或分配失败 | 验收失败，保存资源证据；父进程吞掉子进程错误不能改变结论。 |
| 进程退出成功，但业务结果缺失或不一致 | 业务核对失败。 |
| 缺少控制器、权限、合计限额或可靠峰值计量 | 环境错误，不执行无约束替代，不将未执行验收记为通过。 |
| 测试超时、取消或被外层中断 | 清理本次拥有的进程和资源组，保留退出及资源证据。 |

控制器版本按实际运行环境确认。v1 将 `memory.limit_in_bytes` 和 `memory.memsw.limit_in_bytes` 都设置为预算，并回读确认；使用 `memory.memsw.max_usage_in_bytes` 核验合计峰值。换出的页面仍占用合计额度，宿主交换空间不会成为额外预算。`memory.failcnt` 与 `memory.memsw.failcnt` 记录触限回收次数，增加本身不等于已经发生超额分配，内存不足事件和业务结果分别核对。

v2 使用 `memory.max` 配合 `memory.swap.max=0`，以零交换约束使内存峰值同时成为合计峰值；仍须核实实际限额、交换约束、峰值和事件。缺少所需接口时报告环境错误。

当前用户不能创建专属资源组时，测试启动器通过 `sudo` 创建本次运行的独立委派目录，将目录及其控制文件交给调用用户。pytest、隔离运行器和 camctl 均继续以调用用户身份执行，各被测实例在委派目录下创建自己的叶组。启动器负责正常退出、失败和可处理的中断收场，保存委派身份及清理结果；仅操作本次创建的目录与后代。宿主交换设置和已有资源组保持由环境管理者维护。

内核限制和计量有其粒度与语义；测试仍按实际记录的峰值核对预算。控制器不提供所需可靠记录时报告环境不满足，不能用定时采样声称覆盖所有瞬时峰值。

## 执行入口

在仓库根使用已准备测试依赖的 Python 3.11。容量专项通过 `--test-kind performance` 显式选择；日常功能测试默认不运行这些负载。普通跨组件功能测试仍遵守原有进程组预算，预算控制工具的功能测试也不因此排除。需要 sudo 委派时使用测试启动器，预算参数由启动器传给原测试命令：

```bash
python scripts/run-memory-budget.py --camctl-memory-limit 2GiB -- \
  python -m pytest apps/camctl/tests/integration/outputs/test_source_capacity.py::test_source_write_and_original_key_fit_process_group_budget --test-kind performance -q

python scripts/run-memory-budget.py --camctl-memory-limit 2GiB -- \
  python scripts/run-integration.py --scope camctl --test-kind performance \
  --target apps/camctl/tests/integration/outputs/test_source_capacity.py::test_source_write_and_original_key_fit_process_group_budget
```

启动器在终端打印本次 `launcher.json` 的位置。可以用 `--evidence-dir` 指定一个尚不存在的证据目录；不指定时创建独立临时目录。该收据保存实际命令、调用用户、委派目录身份、退出状态及最终清理结果，各被测实例的资源和业务证据仍由测试保存。启动器收到中断或终止信号时，先通知原测试命令按其中断流程收场，再执行有界的最终清理；外层保留对应的中断退出状态。

已有可写委派目录时，直接使用相同预算参数：

```bash
python -m pytest apps/camctl/tests/integration/outputs/test_source_capacity.py::test_source_write_and_original_key_fit_process_group_budget --test-kind performance \
  --camctl-memory-limit 2GiB \
  --camctl-memory-cgroup-root /path/to/delegated-group -q

python scripts/run-integration.py --scope camctl --test-kind performance \
  --target apps/camctl/tests/integration/outputs/test_source_capacity.py::test_source_write_and_original_key_fit_process_group_budget --test-kind performance \
  --camctl-memory-limit 2GiB \
  --camctl-memory-cgroup-root /path/to/delegated-group
```

大规模容量验收集中在专门的真实业务见证中；读取批次、缓存释放、索引访问和事务正确性按其责任保留较窄测试。具体运行范围、通过结果和环境错误分别记录，不把辅助执行器单元测试通过解释为 2GiB 业务验收通过。

来源保存容量用例位于 `apps/camctl/tests/integration/outputs/test_source_capacity.py`。父测试在组外通过正式入口准备 2400 个来源成员并开始取回，关闭准备连接后启动独立业务进程。业务进程使用公开来源仓储，以批次 4 完成来源保存，再按同一申请和原操作键执行复用与重送。该输入形成 601 个历史事件，真实跨过写入和原结果读取边界。

每阶段返回后，父测试在组外独立核对成员、事件和完整返回；复用及重送前后的状态摘要须保持一致。预算组覆盖业务进程及其全部后代，准备和断言保持在组外。业务进程不导入 pytest，不启用 Python 分配追踪或逐条 SQL 观察器。这项用例只证明已执行负载，不能单凭 2400 个成员推导任意规模均符合预算。

`test_segment_transaction_batches.py` 继续验证批量写入、整组回滚、读取上限和原键核实。其中的容量工作进程功能检查、预算前置条件和合成结果格式检查，不承担真实内存预算验收。

容量用例的 `capacity-timing.json` 分别记录预算预检、组外准备、包含组外核对等待的进程时间，以及保存、复用和重送的工作时间。`memory-budget.json` 保存实际限额、峰值、事件、退出和资源组回收结果；pytest 总耗时不代替业务阶段耗时。

共享配置及资源组执行器位于 `scripts/camctl_memory.py`，pytest 参数与跨组件启动接缝位于 `scripts/camctl_memory_plugin.py`。正常子进程接口使用预算代理；需要保留 host 持有的 PID 和进程组身份时，启动器入组后直接替换为原入口。后者由组外 pytest 读取最终证据。启动握手需要 Linux 环境中的 `/bin/bash`。

执行器的真实内核专项位于 `tests/integration/test_memory_budget_launcher.py` 中，包括两个 `test_real_cgroup_*` 用例、直接执行入口的进程身份用例，以及启动握手、环境隔离和入组失败用例。它们使用固定的 `64MiB` 校准预算，验证整棵进程树收场、父子合计超限时仍被拒绝、直接执行时的进程身份和实际入组，以及握手和启动失败处理。该小预算只用于验证计量工具；业务容量用例继续使用统一参数。

## 严格无交换的独立验收

合计预算通过只证明内存与交换空间没有共同提供超过预算的容量，不能证明没有换页或关闭交换后业务一定完成。换页可能影响耗时。严格无交换能力使用可落实该约束的环境独立执行，记录自己的业务结果与资源证据，不由开发环境合计预算结果推导。

实施进度见[固定内存预算测试计划](../superpowers/plans/2026-10-25-memory-budget-testing.md)。验证记录必须说明源码、输入规模、解释器、控制器和委派范围、内存及合计预算、各自峰值、事件、业务结果及未执行覆盖。测试发现的生产缺陷独立报告，不在测试优化中修改生产实现。
