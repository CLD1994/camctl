# 阶段 16：D42 组合、P3 持久化与相关边界证据

本阶段只归档已结束的收据、日志、JUnit、源码摘要、补丁、静态调查与少量失败数据库；没有运行测试，也没有修改生产或测试源码。归档压缩包均使用 gzip mtime 0。`manifest.json` 为逐载荷的权威清单，记录来源、原始与压缩 SHA-256、字节数、解析状态、运行身份及各项缺失。

## 独立运行组

- **D42 六任务组合**：冻结 HEAD `73153d697258bf8068d906614497762831ee89a3`，六个任务的 cases/result 汇总为 2167 项、2137 通过、30 失败；runner 墙钟约 647.297 秒、launcher 墙钟约 647.785 秒，2 个任务槽；每个任务结束时均核对 9817 个快照文件且 changed=[]，委派清理完成。每个任务的计划、命令、inventory、cases、result、progress、preparations、日志和 events 分别保存。该批失败身份包含 28 个旧 RESULTS 参数变体、1 个异步 `_flow` 用例准备异常及 1 个 capture gap 节点；只保存每类一个代表数据库，不把失败等同生产缺陷。
- **D42 容量叶组**：在 persistence 任务的 `test_fixed_batch_write_and_ver0` 中保存实际 worker 的 `memory-budget.json`、输入/期望/结果/计时和 stdout/stderr。收据记录 2 GiB memory+swap 合计限制，合计峰值 36,040,704 字节，OOM/under-OOM/failcnt 均为 0，叶组已移除。这个断言只覆盖独立 worker；普通 pytest 任务不因此宣称受 2 GiB 预算约束。D42 委派父组实际显示极大哨兵上限 `9223372036854771712`；该父组不是受测worker，原始控制记录单独保存。
- **报告语料**：分开保留 43 项基线、两个 44 项迁移后全组、生成器运行及一个单样本。来源 HEAD/源码 SHA 不同；`23/4244` 等只描述语料资产生成事实，不是业务永久要求。某些旧的脏测试/helper 源码没有精确副本，manifest 逐项列明，未用后续 Git 源替换。原跟踪 gzip fixture 只引用其 Git blob，未重复存储；最终生成资产保留原 gzip 字节。
- **P3 持久化端口分组**：配对重构、结果页、结果集、基准读取、批处理与存储按各自提交和运行记录。窄轮仅为诊断；单文件基线、修改后完整文件以及其他独立节点不相加为全套验收。独立 import preflight 不是 pytest 进程内观测。
- **P3 历史与 persistence 完整目录**：快照 HEAD `2f8cdb6a0805ab3be901cb540ef9516c63727099`，两任务分别为 history 349 通过、persistence 952 通过；合计 1301 通过，root runner 墙钟约 365.864 秒、launcher 墙钟约 366.295 秒、2 个槽。两个任务结束时各自核对 9909 个快照文件且 changed=[]，委派清理无残留。委派父组显示哨兵上限 `9223372036854771712`，不能代替叶组预算。另保存真实容量 worker 叶组：2 GiB memory+swap 合计限制、合计峰值 36,098,048 字节、OOM/under-OOM/failcnt 为 0，叶组已移除。与 D42 是不同源码快照，不能合并宣称单次回归。
- **历史读取辅助证据**：D42 的单项修正复验与 `f5142b01` 命名变更后单项分别保存；两次不是同一源码状态。另保留作者的读取诊断组与 timelapse 测试文件运行（JUnit 14 项）；它们也不与其他快照相加。
- **静态审计**：P3 命令边界与 runner preparation 文件仅作为静态证据。P3 审计冻结后，HEAD `2f8c` 已删除死 `plan_operation`；审计并未逐一完成全部内部接口检查。静态材料不是运行验收。

## 计时和缺项

JUnit 的 `suite_time_seconds` 与 `testcase_time_seconds_sum`、pytest 日志中的汇总秒数、每任务/单次运行墙钟及 root launcher 墙钟分别记录，不能混用。容量控制的委派父组收据不代表叶组预算；只有上面标出的 worker `memory-budget.json` 支持独立容量 worker 的预算断言。

本批没有归档通过用例数据库、完整临时目录、完整源码树快照或缓存；manifest 所列精确 Git 源码文件仅作为变更证据单独导出。D42 只保留三个失败代表 DB。报告语料若缺少确切旧 dirty source，缺项保持明确；不根据后续源码补写。P3/Persistence 的分组、静态调查和修正前后运行不可拼接成一次统一回归，也不代表全项目/设备验收。

逐文件 SHA、来源、HEAD、套件/用例时间、墙钟及缺失项见 [manifest.json](manifest.json)。
