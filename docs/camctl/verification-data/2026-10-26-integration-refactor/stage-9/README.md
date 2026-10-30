# 阶段 9：取消、媒体和启动收场证据

本目录为持续问题记录保存若干独立运行的原始收据、JUnit XML、pytest 输出、有限失败复现材料和两份静态交接。各运行使用不同来源快照和测试目标；本目录不把它们合并为一次回归，也不表示完整集成验收或总体目标完成。

## 独立运行

| 运行组 | 收据中的 HEAD | JUnit 结果 | 套件时间（秒） | 用例时间总和（秒） | pytest 日志时间（秒） | 外层墙钟（秒） |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `merged-cancel-first-ready21` | `6ba76b54132b4f3ae1178598e817efeb11ac967c` | 21 通过，0 失败，0 跳过 | 41.755 | 41.012 | 41.77 | 43.628 |
| `merged-media-local9` | `c5a257769449bd0d109b8d709f66f3abe6879325` | 9 通过，0 失败，0 跳过 | 5.286 | 4.365 | 5.30 | 7.397 |
| `work-files-media-full-1` | `6fc6a59cbff01b56814156d6d089e23e55d2e9bc` | 9 通过，0 失败，0 跳过 | 3.058 | 2.874 | 3.06 | 3.772 |
| `work-files-full-3` | `8f900b8b161f93ce3a1d10ebb435dbffda99137a` | 40 通过，0 失败，0 跳过 | 55.102 | 54.832 | 55.10 | 55.851 |
| `work-files-media-8` | `6fc6a59cbff01b56814156d6d089e23e55d2e9bc` | 1 通过，0 失败，0 跳过 | 1.100 | 0.867 | 1.10 | 1.874 |
| `work-files-media-6` | `6fc6a59cbff01b56814156d6d089e23e55d2e9bc` | 0 通过，1 失败，0 跳过 | 0.963 | 0.731 | 0.96 | 1.567 |
| `log-settlement-c597` | `8cce607753dfddeef15496d1ba9f8509c0cef88c` | 27 通过，1 失败，0 跳过 | 17.175 | 16.906 | 17.17 | 17.794 |
| `log-settlement-diagnostic3` | `9e35a730792c9b2a62b67b16dd8b27b69fb35c6f` | 0 通过，1 失败，0 跳过 | 10.490 | 10.175 | 10.49 | 11.450 |
| `media-race-audit-no-save-fault-fast` | `c5a257769449bd0d109b8d709f66f3abe6879325` | 0 通过，1 失败，0 跳过 | 4.957 | 3.923 | 4.96 | 6.682 |
| `history-cost-report-4200-1` | `c5a257769449bd0d109b8d709f66f3abe6879325` | 1 通过，0 失败，0 跳过 | 49.016 | 47.843 | 49.01 | 50.383 |
| `history-cost-segment-growth-1` | `c5a257769449bd0d109b8d709f66f3abe6879325` | 1 通过，0 失败，0 跳过 | 97.118 | 96.177 | 97.12 | 97.812 |

JUnit 套件时间、单个 testcase 时间总和、pytest 日志摘要和收据墙钟分别来自不同字段。manifest 为每一组保留完整命令、PID、来源目录、源码摘要和测试文件散列；请勿用其中一种计时替代另一种。

## 媒体运行的边界

`work-files-media-full-1` 是单文件 9 项通过记录；`work-files-full-3` 是另一来源快照上的 40 项通过组合；`work-files-media-8` 是单项通过；`work-files-media-6` 是独立单项失败。四者不能相加成“完整媒体回归”，也不能让通过运行覆盖失败。

`work-files-media-6-review/read-only-facts.json.gz` 和 `handoff.md.gz` 记录对原失败数据库以只读方式提取的事实。失败运行的 `state.db`、WAL 与 SHM 另以原字节保存，归档时没有打开 SQLite。证据显示 scheduler 在 `ffprobe` 启动前先收到同文件仍有活动任务的 `FileTaskError`；之后取消收场保存 `FAILED/tool_cancelled` 时，COMMIT 回执丢失并记录 `UNKNOWN`。因此 `UNKNOWN` 是后续收场观察，不能写成首次文件冲突原因。本次两个正式时钟入口使用不同单调读数的核对仍待统一复审；交接将它列为待核事项，不据此下结论。

`work-files-media-8` 未保存 `source-sha256.txt`。manifest 明确记录此缺项，没有从同 HEAD 的其他运行推定其工作树源码摘要。该组只保留小型工具脚本、调用记录、计时和 10 字节输入，不复制通过用例的数据库。

`work-files-coverage-map/coverage-map.json.gz` 保存旧目标到新测试的映射。它说明部分首次结果、ready 撤回和媒体目标已有对应证据，同时指出 ready 九个同拥有者重送分区以及旧 media 八项尚未迁移；映射本身不证明其余目标已完成。`work-files-full-3` 的 40 项结果只属于它自己的五文件命令和源码快照。

## 启动扫描收场证据

`log-settlement-c597` 保存完整文件运行 `physicalfull` 的收据、命令、pytest 输出、JUnit 以及失败用例应用日志；结果为 27 通过、1 失败。交接记录的失败顺序是：受控任务占用唯一默认线程池工作线程，数据库专用线程因受控打开错误结束，启动扫描仍排队；收到取消后，原 10 秒门闩到期，线程池工作线程释放，扫描才进入目录并删除旧副本。断言在打开错误和取消的最终交付之前失败，所以本次运行没有核实二者的最终优先级或异常对象身份。配套 `production-handoff.md.gz` 保留该快照的分析与待守契约；它不代表物理设备或任意负载验证，也不替代源码修复或验收。

`log-settlement-diagnostic3` 是另一份独立窄运行，单项测试失败。它单独保存 PID、命令、来源 HEAD、执行前后应用源码与测试集合聚合 SHA、JUnit、输出、目标测试源码快照和失败用例应用日志。日志中，测试等待撤回通知的门闩超时，随后失败于“在占用工作线程结束前未撤回启动扫描”的断言。该项只记录自己的窄命令和失败结果，不能与 `physicalfull` 的完整文件运行混为同一运行。

`media-race-audit-no-save-fault-fast` 使用合法的 1 毫秒轮询，没有注入数据库保存故障。该运行的真实 READ、原片 probe、检查保存和 repair 均已发生；随后默认 cancel 恢复入口与仍占 file1 的 `media_repair` 任务发生准入冲突。它与 `work-files-media-6` 在不同阶段失败：media-6 首次冲突发生在 probe 启动前的 scheduler/`media_check` 阶段；本次 probe 和检查保存已完成，首次冲突来自 cancel 恢复与 `media_repair`。交接明确说明它不是对 media-6 精确窗口的复现，也未运行概率测量。运行前后 9512 项源码/资源摘要一致，manifest 保留完整散列和逐项 comparison。

`history-cost-report-4200-1` 与 `history-cost-segment-growth-1` 是两个不同测试节点的单次诊断运行，均在 c5a257769449bd0d109b8d709f66f3abe6879325 基线上，过程收据分别保留两个 dirty 测试文件的 SHA。report 节点的受理批次为 3、4200、3 个动作；此前将规模写作 4100 是口误。日志记录的阶段计时包含原 SQLite trace、progress 和弱引用观测，目标阶段不包括 fixturesetup。segment 节点的输入为 256 和 2048 成员；采集只用于诊断准备与目标成本边界，没有提速实现或阈值变化。补丁和 `boundary-audit.md.gz` 留存观测范围、无法替代的覆盖项和候选调查方向，不代表这些候选已经实施。

## 文件完整性与范围

`manifest.json` 列出 120 个 gzip 载荷，总原始长度 4,156,890 字节、压缩后 722,923 字节；逐项记录原始路径、原始 SHA-256/字节数、压缩 SHA-256/字节数与运行归属。所有 gzip 的 mtime 为 0；归档时逐件验证了解压字节与原始 SHA 一致，并解析了 JSON/XML。唯一记录的可选缺项是 `work-files-media-8/source-sha256.txt`。

归档只复制媒体 6 和无故障媒体竞态失败运行的数据库及 WAL/SHM，不复制其它通过用例的数据库、完整临时目录、依赖或源码树。历史成本取证仅保留两个节点的命令、输出、JUnit、测量补丁和边界报告，没有复制准备数据库。未运行测试，未打开或修改 SQLite，也未改动生产代码。
