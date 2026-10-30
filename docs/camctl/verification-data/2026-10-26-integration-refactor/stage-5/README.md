# 阶段 5：2026-10-30 恢复诊断证据

本目录只保存已有报告、运行收据和测试结果的压缩快照。manifest.json 为文件来源、源码身份、运行身份、范围、解压长度及 SHA-256 的权威清单；所有 .gz 均使用零时间戳，且已完整解压核验摘要和长度。归档过程未运行测试。

## 证据范围

- diagnostic-other14 来源记录的 HEAD 为 290f4ad39db1bffdf6b764e2ef77c97ce12a82fb；14 个串行目录任务均已终态。汇总为 6,635 项：6,539 通过、94 失败、2 跳过；任务汇总退出码为 1。pytest 阶段耗时合计 1233.498308 秒，14 个任务墙钟合计 1296.133471 秒，汇总总墙钟 1302.623823 秒。它仅是该次诊断运行，不表示该轮全量通过。
- 后续源码 HEAD db9c801f 的 baseline-preparation 两组分别为 63/63 与 21/21 通过；HEAD ad821cc6 的 guards144 为 144 项通过；HEAD ae4b769c 的 merged-capture375 为 375 项通过。merged-capture375 收据的范围为“已集成取消123、诊断49、原生59、守卫144；不是全capture或全量回归”，不能称为全 capture 或全量回归。
- 固定批量 513 的证据包含实际叶 cgroup 收据 memory-budget.json、业务结果与阶段计时。该单项 worker 报告合计限额 2 GiB、合计峰值 39063552 字节、OOM/限额事件为零，退出码为 0，业务结果核验 513 项；所属 persistence 目录任务整体仍以退出码 1 结束。单独交换峰值未测量，合计峰值来自 memory.memsw.max_usage_in_bytes；这只是单个 513 输入批次证据。
- 另存的 diagnostic-other14-memory/launcher.json 记录外层委派收据。其 groups 只有 delegation 父组，限额值接近无穷大，因此不用于证明叶 worker 的预算；叶组以 memory-budget.json 为准。

归档未包含源码快照、依赖、普通数据库、全量 events.jsonl、运行器源码或容量用例数据库。可选的 guards144/source-manifest.json 在来源目录不存在，已在清单中注明。没有单独的诊断根级 results 文件；根 summary 和 14 个 task result 均已纳入。该快照不建立 Git 对象，也不把不同 HEAD 的结果合并成一次通过。
