# 阶段 13：共享时钟、诊断与报告样本证据

本目录只保存指定的终态证据和一个报告样本提案。每个运行来自自己的基线、源码和目标范围；结果不得相加成同一轮全量通过。归档器没有运行测试，也没有修改生产代码或测试。

## 独立运行记录

| 运行组 | JUnit 结果 | JUnit 套件/用例时间 | pytest 摘要与外层墙钟 |
| --- | --- | --- | --- |
| consumer-clock-baseline | 6 项：6 通过、0 失败、0 错误、0 跳过 | 套件 1.847s；用例时间和 1.078s | 6 passed in 1.84s；墙钟 2.155s |
| consumer-clock-narrow | 6 项：4 通过、2 失败、0 错误、0 跳过 | 套件 1.894s；用例时间和 1.078s | 2 failed, 4 passed in 1.89s；墙钟 2.184s |
| consumer-clock-narrow3 | 6 项：6 通过、0 失败、0 错误、0 跳过 | 套件 1.386s；用例时间和 0.853s | 6 passed in 1.38s；墙钟 1.700s |
| consumer-clock-capture | 47 项：47 通过、0 失败、0 错误、0 跳过 | 套件 6.157s；用例时间和 5.474s | 47 passed in 6.16s；墙钟 6.497s |
| consumer-clock-reporting | 2 项：2 通过、0 失败、0 错误、0 跳过 | 套件 2.455s；用例时间和 2.271s | 2 passed in 2.45s；墙钟 3.177s |
| consumer-clock-history | 78 项：78 通过、0 失败、0 错误、0 跳过 | 套件 177.362s；用例时间和 176.331s | 78 passed in 177.36s (0:02:57)；墙钟 178.053s |
| merged-clock-followups-capture | 14 项：14 通过、0 失败、0 错误、0 跳过 | 套件 3.093s；用例时间和 2.590s | 14 passed in 3.10s；墙钟 3.868s |
| merged-clock-followups-outputs | 14 项：13 通过、1 失败、0 错误、0 跳过 | 套件 2.143s；用例时间和 1.371s | 1 failed, 13 passed in 2.16s；墙钟 2.864s |
| merged-clock-followups-history | 45 项：45 通过、0 失败、0 错误、0 跳过 | 套件 32.145s；用例时间和 31.554s | 45 passed in 32.16s；墙钟 32.860s |
| read-diagnostics-ports-full-1 | 14 项：13 通过、1 失败、0 错误、0 跳过 | 套件 2.786s；用例时间和 1.576s | 1 failed, 13 passed in 2.78s；墙钟 4.254s |
| timelapse-public-start-1 | 14 项：14 通过、0 失败、0 错误、0 跳过 | 套件 3.217s；用例时间和 2.646s | 14 passed in 3.21s；墙钟 3.564s |
| report-corpus-prototype-node-1 | 1 项：1 通过、0 失败、0 错误、0 跳过 | 套件 1.678s；用例时间和 0.679s | 1 passed in 1.68s；墙钟 2.920s |

JUnit 套件时间、JUnit 用例时间和、pytest 日志时间及外层墙钟分别记录在 manifest.json。上表按单组列结果，不合并不同源码快照。

## 共享时钟证据

consumer-clock 基线为 c67742cb；新增源的精确字节来自 22c50e84。change.diff 与 c67742cb 到 22c50e84 的三文件 Git 差异逐字节一致。baseline、首次 narrow 和修正后的 narrow3 都按独立六项范围记录；首次 narrow 中，临时诊断断言错误要求 photo 与 timelapse 的 START max_attempts 为 3。断言因记录值为 1 而失败，属于测试自身假设错误，不是生产失败。更正记录对应样本实际 START grant 为 1 次、30 秒、retry_interval 为 None。这一结论只覆盖 photo/timelapse 样本，不裁决 record 的 None，也不替代其他消费者的参数判断。

capture 47、reporting 2 和 history 78 是 22c50e84 源码快照的三个独立运行组。comparison、历史/报告预算、独立导入预检、窄范围日志和差异单独保存。证据根目录没有独立 handoff 文件；本阶段另存的 correction.md 是后续更正说明，不伪装成原始交接。

merged-clock-followups 来自主树 d7d60c47，capture 14 项通过、outputs 13 项通过且 1 项失败、history 45 项通过，三个收据均记录 source-manifest_changes 为空。preflight-import.json 是 pytest 外的独立解释器导入检查。outputs 失败用例返回 stopped 后，日志记录 source_read 的 local_read_failed；测试断言原本预期没有错误记录。归档了该用例的 state.db；复制前运行 PID 已退出，fuser 检查没有活动持有者。精确失败目录没有 state.db-wal 或 state.db-shm。归档没有打开 SQLite 数据库内容，也没有复制该目录中的 staging 输入或其他用例数据库。

## 作者侧读取与延时启动

read-diagnostics-ports 的测试源来自 0feae413。完整文件运行有 1 项停止场景断言失败：读取返回 stopped，转交副本时产生 segment_transfer_failed: stopped，随后记录 local_read_failed；用例断言预期没有错误记录。仓储端口替身可以模拟拒绝；该拒绝不是 SQLite 自发错误。日志不能证明原键重送已经完成，也不能证明诊断准备时序要求已满足。

timelapse-public-start 的测试源来自 5a468ce0，测试文件 SHA 与 Git 源已核对，补丁也与单文件 Git 差异一致。独立更正记录 photo 和 timelapse 的 START grant 为 1/30/None，并说明共享装配字段 3/10/3 不是这些 START 尝试的最终预算。更正没有新增 pytest，也未改写 Stage 12 原始审计；record 的单独预算和 None 语义仍需依具体契约判断。

## 报告样本提案

prototype commit 0c77dc05 的测试源码及测试文件差异取自 Git 对象。归档还保存了样本 JSON 和原始 749,892 字节的 report-selection-4200.sqlite.gz。该 gzip 按原字节复制，没有再套一层 gzip；manifest 分别记录原 gzip SHA-256、头部 mtime，以及解压后 6,483,968 字节样本的 SHA-256。

report-corpus-feasibility 只检查指定节点的只读副本。事实文件记录该副本压缩/解压诊断约 0.078 秒，但这不是 pytest 准备时间或提速预测。原型只运行一个用例：JUnit 套件 1.678 秒、单个用例 0.679 秒。这个单点结果不代表全文件执行或等价提速。原型尚未合并，仍待用户决定；主测试当时仍在运行期生成数据。除提案样本和小型收据外，没有复制通过用例数据库、原始全量数据库或 scratch 目录。

## 缺项与完整性

| 项目 | 现场情况 |
| --- | --- |
| consumer-clock 独立 handoff | 指定证据根及浅层路径内没有独立 handoff；更正文件另行归档。 |
| outputs 失败数据库边车 | 指定失败目录只有 state.db，没有 WAL 或 SHM。 |
| 通过用例数据库与完整临时目录 | 未复制；仅复制 outputs 中该失败用例的必要 state.db。 |

manifest.json 逐项记录新 gzip 载荷的原始/压缩字节数、SHA-256、mtime 0 和结构化解析结果。原型压缩样本另作原始 gzip 资产记录，区分压缩资产散列与解压后数据库散列。数据来源限于本阶段清单列出的路径，没有递归扫描完整数据库树。
