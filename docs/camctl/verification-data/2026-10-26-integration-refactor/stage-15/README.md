# 阶段 15：准备端口、自然等待与媒体收场证据

本目录归档三组彼此独立的已终止运行，以及一份只读入口契约事实补充。本批没有运行测试，也没有修改生产或测试源码。

## 运行身份

准备端口后续验证的三份收据均记录 e652 HEAD。`receipt-red` 保留源码集合指纹 `90d71ff8…`，3 项中 2 项通过、1 项失败；`receipt-green` 与 `fix-boundaries` 指纹为 `bfca8f30…`，分别为 3 项和 31 项通过。helper 修正精确对应 `86734082` 的 Git blob，且 `identity-fix.diff` 与 `git diff e652..86734082` 逐字节相同。`fix-capture` 的 495 项结果、身份修正补丁、运行脚本和 e652 helper 已在[阶段 14](../stage-14/README.md)保存；本阶段通过哈希核验的跨阶段引用指向原载荷，不重复压缩。

Stage 14 还保存了 `receipt-probe` 的 2 项通过收据、日志和 JUnit；同目录的 `test_receipt_probe.py` 源字节属于后续 3 个用例的快照，不能用来代表早期 2-case 执行。早期 2 个用例的测试源码没有精确字节副本，manifest 将此列为缺项。Stage 14 保存的 3 个用例源码 SHA-256 为 `331331a5…a3fa2ce`。本阶段另保存 `stageB` 探针的实际源码。

自然等待证据逐项核对 e652 旧侧与 c469 新侧六个文件的 Git blob SHA。27 项 capture 与 1 项 persistence 的同身份状态库比较都记录为相等。独立完整目录运行分别为 capture 299 项通过、persistence 13 项通过；它们与 27+1 比较分开记录。`independent_import_precheck` 来自独立预检进程，不是 pytest 内的运行时插桩。

媒体收场证据分别保留 e652 基线与 e652 HEAD 下迁移后源码的 20 项运行。20 项状态库比较记录 0 项差异；迁移后的脏文件只有目标测试，其 SHA 与 `77127598` 的 Git blob 相符。manifest 还保存 e652 至该提交的 Git 单文件差异。两次运行按各自来源和源码身份记录，不拼接成统一回归。

`segment-inspections-boundary/entry-contract-facts.md` 是只读静态事实补充，不是测试或验收；前序静态 audit 与 handoff 通过 SHA 引用[阶段 12](../stage-12/README.md)。

## 载荷与限制

每个载荷均为 gzip mtime 0。manifest 记录原始与压缩 SHA-256、字节数、源路径、源码身份、pytest 摘要、JUnit 套件时间、用例时间及外层墙钟，并记录 JSON/XML 解压解析。自然等待只归档运行目录中的直接文件；媒体不包含临时数据库。未归档通过用例数据库和完整临时目录。三组结果不可相加成一次完整回归。详细条目、跨阶段引用、缺项和边界见 [manifest.json](manifest.json)。
