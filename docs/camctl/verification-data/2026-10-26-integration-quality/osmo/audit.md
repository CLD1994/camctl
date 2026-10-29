# OSMO 分段交付测试审计与局部接入

日期为 2026-10-26 UTC。源码基线为 `509002c25308eda98e0096068d3510485938027e`，工作区为 `/tmp/camctl-quality-osmo-20261026`，分支为 `test/quality-osmo-20261026`。选择身份来自该源码中的 `docs/camctl/verification-data/2026-10-26-integration-quality/selection.json`；本模块全部八项均保留。当前只修改 `tests/integration/test_osmo_builtin_record_segmentation_roundtrip.py`，不修改生产、配置、协议、依赖或公共测试辅助。

## 权威行为与真实协作者

行为依据是 `docs/superpowers/specs/2026-10-23-video-result-modes-and-segmentation-design.md` 的动作结果、默认取回、共同提交、历史与客户端合同，以及 `docs/superpowers/specs/2026-10-20-osmo360ii-record-segmentation-design.md` 的 OSMO 名称、下界、空尾与默认范围合同。`tests/integration/README.md` 的 OSMO 安装版联合链说明提供当前软件验收边界。

每个参数使用独立世界和客户端存储，真实组合仓库外构建和安装的 wheel、CLI、C host、SQLite、报告领取、客户端 Application 的计划导出和报告应用、Files.upload 的逐份媒体核验与累计 ACK。相机命令只在受合同约束的 ADB 进程端口替换，不接真实相机。运行时分段路径不执行媒体时长检查；本模块的合成字节不能证明真实 OSV 的时长、帧数、双路解码或物理断电持久性。

## 八项业务身份、预期与失败分区

| 参数 | 独立输入与预期 | 真实边界及失败区分 |
| --- | --- | --- |
| normal | 索引 7、8；两份原片和两份预览分别登记；默认原片、预览均取两份，显式最后一份另交付。 | 扫描页实际提交后终止旧 host 与原执行组，新 host 重送同请求；完成后唯一 START。 |
| overrun | 索引 7 至 12 全部保留；默认固定前三份；显式取范围外第 12 段。 | START 保存后实际重启，来源终态提交后再次重启；与提交门闩保存的完整产物身份比较。 |
| short-set | 确认多录但仅有索引 7、8；默认两份；第 8 段使用独立短尾字节，完整交付且不裁剪。 | `assessment_json.overrecord=1`，默认数量来自参数表；不以三份作为必要原片数。无专项重启。 |
| gap | 索引 7、8、10；来源失败；默认原片及预览仍交付缺口前两段；显式第 10 段可取。 | 精确区分 `capture_segment_gap` 和 `missing_position`，缺口从索引 9 开始；选择固定后实际重启且原 obtain_items 保持。 |
| zero-gap | 索引 9 的 OSV 为空但其 LRF 非空，另有 7、8、10；空原片及不合格预览不登记，默认只取 7、8。 | 精确区分 `capture_segment_gap` 和 `zero_byte_non_tail`；不同于位置缺失与空尾。无专项重启。 |
| empty | 首段 7 的 OSV/LRF 为空；索引 8、9 保留；默认和预览为空范围，不创建目标交付；显式末段仍可取。 | 默认失败码精确为 `default_segment_scope_empty`；报告字节登记后实际重启，最终比较原报告 ID、历史边界、大小和摘要。 |
| unknown | 文件名 UNKNOWN.OSV/LRF 无可解析顺序；合格文件留存；默认和预览范围未知；显式 ID 可取。 | 默认失败码精确为 `default_segment_scope_unconfirmed`，与已知空范围区分。无专项重启。 |
| native-tail | 索引 7、8 非零，9 的 OSV/LRF 为空；保留两组；沿 300 秒目标与 30 秒余量自然完成，无 STOP。 | 原片读取真实数据块且至少提交 64 KiB 后停止旧 host，再由新 host 重送原请求；现有恢复证明的局限见下文。 |

`default_count` 和动作终态均来自固定参数表，没有调用生产选段公式。每次取回分别创建请求和业务状态，成功提交未在其他参数中复用。相同字节和摘要有意用于不同独立段，检查交付 ID 全部独立，防止按摘要合并。

预期产物 ID 取自数据库当前产物目录，按 `o.id` 排序，字节预期通过该产物 locator 查找预设样本。因此这里独立证明的是数量、选择数量、具体已登记文件的完整交付和每份独立身份，没有独立证明 ID 到预定索引的完整映射、全部预览配对或设备键与 ID 相反时的排序；后者由 README 指向的组件测试承接。本次未改变这些范围。

## 会话接入保持的行为

每个世界只建立一个 ClientSession，以原 `export`、`import`、`import-media` 操作及原 store 参数调用公共唯一分派入口；不改变原客户端业务路径。每次操作分配 `client-receipts/NNN-<原回执名>`，使新回执不能复用旧文件。操作预算仍为原来的 120 秒，公共辅助记录实际 startup、每条操作与 close 耗时。所有 host/CLI 重启和同步点原样保留。

各轮仍先完整应用已领取报告，核对报告 ID，然后导出累计 ACK，并核对 runtime_state 中的报告 ID 与水位，最后逐份导入媒体。每份交付继续核对领取字节、size、SHA-256、delivery_id、output_id 和客户端 verified。原请求重送等待真实 submit 成功退出后才比较交付身份。远端字节、唯一 START、STOP 次数、安装模块来源核对保持。

计划导出、host 初始化或后续操作失败时，收场仍覆盖已建立的客户端。测试先停止 host，再关闭客户端，最后归档世界；原失败、收场失败和归档失败可以同时保留，不互相遮蔽。

## native-tail 的现有恢复证明局限

当前场景在停止旧 host 后比较已保存的 `(copy_id, delivery_id, committed_bytes)`，随后在启动新 host 前释放读取 gate。恢复后没有在首个新数据字节之前核对原 plan/action/delivery/copy 身份、已提交偏移及 ADB 实际 `skip=1`。最终完整字节与摘要正确，不能单独证伪从零重拷或新建交付后最终得到相同字节。

具体补强方案可以沿相邻 `test_osmo_builtin_timelapse_roundtrip.py` 的既有模式：在原读取提交第一个完整数据块并停止后保留 gate；将恢复进程的 `CAMCTL_OSMO_TEST_READ_GATE_AFTER_BYTES` 设为 0；等待同一源文件的新 `read_started` 与零输出 `read_blocked`；检查实际 `skip=1`、原 plan/action/delivery/copy 身份和原 committed_bytes；再释放 gate，并在最终交付中关联同一 copy/delivery。此补强需要确定恢复门闩的验收位置。本次保持原门闩并明确限制，不声称已经取得沿原偏移恢复的完整证明。

其他证据范围也分别保留：overrun 比较当前产物身份，empty 比较原报告登记元数据，本模块不单独证明全部冻结来源或完整旧历史重建；native-tail 推进到 330 秒，但未单独证明 329 秒不能完成。现有 `_report_or_error` 搜索整份 module.log 的 `result=error`，没有按当前请求固定日志起点，也未单独检查不相容终态；后续可用既有 HostDemo.watch_failures 完善定位，保持原成功预算。

## 本次基线实测

命令和环境的完整权威回执为同目录 `before-command.json`，结果为 `before-result.json`，输出为 `before.log`、`before.xml`，阶段记录为 `before-stages.json`，预算收据在 `before-budget/`。入口为 `run.py before`，使用 Python 3.11.17、2GiB 内存与交换空间合计预算、独立 tmp/pycache/pytest 临时根；依赖和锁文件只读复用。其他质量任务可在独立工作区同期运行，本次单次带诊断包装的结果不外推稳定提速或目标硬件性能。

normal 在未修改测试源码时退出 1，整轮墙钟 29.554 秒，pytest setup 13.262 秒、call 14.431 秒、teardown 0.049 秒。第一个客户端计划导出耗时 1.747 秒；执行尚未进入报告导入和媒体交付，实测客户端启动数为 1，无法建立完整成功路径的优化前总耗时或实际启动数基线。静态路径的完整正常场景有 16 次一次性客户端命令，仍须用成功运行确认。

真实旧组退出并重启后，生产 ResultMembersCommand / append_result_members 回滚，CLI 报 `state_db_error`，细节为“分段保存尚未可靠完成: 分段保存只接受固定大小模型”。该失败发生在客户端会话接入之前；观察器只在实际提交后读库和暂停，不修改请求。主代理已把问题定位到生产返回类型并单独处理。本次测试优化不修改生产，也不删除或绕过原恢复门闩。预算委派与清理成功，未将业务失败记作预算或功能验收通过。


## 接入后的代表诊断

本地测试提交为 `49ef9bf4`。按主代理安排先集成公共辅助提交 `aff87d6b979b5970aa49d6e5e0f96697bedb2733`，本地组合 HEAD 为 `bbb39cdf`，只运行同一 normal 节点，没有重跑其余七个世界。

改后运行仍在同一生产分段成员保存步骤失败，退出 1。整轮墙钟为 25.0327 秒，pytest setup 为 10.8538 秒，call 为 13.4046 秒，teardown 为 0.0247 秒。预算收据保存的实际结束时间为 `2026-10-25T18:26:03.217333Z`。两次均未通过完整业务路径，准备时间与同期负载也不同，这些失败耗时不构成成功链的提速结论。

客户端会话实际启动 1 次，Application 和 Files 各建立 1 次，没有启动 HTTP 服务或浏览器。startup 为 1.0598 秒，首次 export 为 0.2317 秒，close 为 0.0394 秒，三阶段均通过；Node 保存 status=closed，Python 保存原 120 秒预算。报告导入与媒体上传尚未执行，因此不能用本轮证明全部会话操作及完整交付。

六个被测 CLI 资源组均落实 2GiB 内存与交换空间合计限额，最大合计峰值为 120,918,016 字节，OOM 和触限事件均为零，全部叶组与本次委派目录移除；启动器 errors 为空、remaining_pids 为空。业务失败仍为本轮结论，不计作功能或完整业务预算验收通过。

完整命令、源码身份、输出、XML、阶段、客户端会话与资源收据在 `after-*` 文件中，机器可读摘要为 `after-summary.json`。八参数与 29 条断言的静态不变核对另存 `static-checks.json`。Python 3.11 的 py_compile 和 git diff --check 通过，独立静态审查未发现本地接入阻断。
