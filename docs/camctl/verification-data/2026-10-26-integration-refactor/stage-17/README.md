# 阶段 17：Capture 结果扫描、夹具状态与 Action6 时钟证据

本阶段按各自源码身份归档已结束运行、计数比较和静态源码关系，没有运行测试，也没有修改生产或测试代码。所有新归档载荷使用 gzip mtime 0；[manifest.json](manifest.json) 逐项记录原始/压缩摘要与长度、来源路径、解析结果、Git源码、计时口径和缺项。

## 结果扫描仓储端口

`result-scan-repository-ports` 保留 baseline、首轮 after、final 三轮各 85 项通过。JUnit suite 时间分别为 27.579、25.808、26.597 秒；pytest 日志汇总、墙钟分别存放，墙钟为 28.009、26.247、26.960 秒。三轮的head仍记作 `f269eb2f`，首轮 after 的两个测试源 SHA 与Git对象不匹配且没有精确源文件副本：`test_result_scan_runtime.py` 的 `e2e2…14e7` 和 `test_result_scan_recovery.py` 的 `74f4…fc67`。manifest保留这两个中间源码缺项；final使用 `a894b3bd` 下四个文件的Git blob，未把final源码替换到首轮。数据库比较与导入预检按原始文件保存，比较器的范围以其原记录为准。

## Capture fixture 状态

`capture-fixture-state-baseline` 与 `capture-fixture-state-modified` 各有 41 项通过。基线源码对应 `fab33b41`，修改后两份测试文件对应 `db780dfe`；保存的 `change.patch` 与两提交之间的 Git diff 逐字节一致，生产源码摘要未变。原始比较在 `test_dispatch_routes_all_capab0/state.db` 中记录 `runs` 与 `attempts` 两处行次序/ID配对变化；规范化业务比较仍报告 41/41 节点配对且无规范化差异。两份比较都保留，不据规范化结果声称所有原始行字节相同。

## Capture 六文件合跑

`capture-ports-combined` 是独立快照 `83909355` 下的六文件组合，126 项通过；JUnit suite 时间 36.489 秒，pytest墙钟36.905秒。收据、日志、完整源码前后清单、命令及独立导入预检均已保存，前后源清单一致。这一组只覆盖记录的六个文件，不代表完整 capture 目录或全capture验收。

## Action6 时钟阶段

Action6各阶段按原快照分别记录：7c09基线12项、时钟修改尝试12项、e396生命周期时钟阶段12项、stop时钟完成12项、helper9项，以及root `7e7882fd`四文件合跑23项。它们的源码、结果与比较不能相加为一轮。时钟修改尝试的比较记录一个数据库中的39条事件，其变化仅落在原比较器事件行的时间字段；因此该尝试不属于状态等价结果。e396阶段的12项运行只表示生命周期时钟阶段，不能代表stop端口已完成。

stop登记值从 `False` 改为 `True` 是修复先前漏传stop端口，使装配值与原冻结的 `True` 一致；该调整不是声称两次运行逐字输入完全相同。stop完成与helper证据分别保留：stop完成12项，helper范围9项。二者运行时HEAD为 `e39667a9`，其测试源SHA与后续Git提交 `1f4c9751` 的三个文件匹配；README与manifest因此分别记录运行HEAD和实际源码blob身份。原patch与 `e396..1f4` Git差异逐字节匹配。

Root `action6-clock-combined` 在 `7e7882fd` 快照下运行四个指定文件，23项通过，JUnit suite 19.204 秒，pytest墙钟19.808秒；其中包括独立的 `test_action6_control_elapsed.py` 调用方。前后source manifest逐项一致。它不代表全capture目录或2 GiB容量验收；导入预检是独立步骤，不是pytest内测量。

两个合跑的前后源码清单各有 1,358 条路径，且每轮前后逐项一致；其中 1,331 条能匹配记录的Git HEAD，另有 27 条位于 `apps/camctl/src/camctl/_resources/`，在相应HEAD中没有Git blob。归档保留完整清单，但不把这27条描述成已由Git源码核实。

## 源码和证据边界

`result-scan` 的基线/最终四个源码文件、capture fixture 的两份版本、以及 Action6 的三个文件在 `source-git/` 按Git提交导出，并保留对应差异。Action6中间39事件时间尝试的 `test_action6_lifecycle_recovery.py` 源码SHA `4ce7…e54f` 未找到对应Git对象或文件副本；Stage17保留该轮source map和原始patch，并如实列为缺项。没有使用后续e396/1f4版本替代这份dirty源码。

本阶段未复制通过用例数据库、临时目录或缓存，没有容量受限运行结论，也没有调查后续pairing-preparation项目。各轮JUnit suite时间、testcase时间合计、pytest日志时间和外层墙钟在manifest中分别记录。
