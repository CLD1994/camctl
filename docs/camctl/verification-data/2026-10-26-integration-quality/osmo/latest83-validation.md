# 固定最终基线的 OSMO 分段验证

源码固定为 `f143fbbecf68e9333ef6c4db36188b2c6c642630`，测试文件 SHA-256 为 `e295d5a5288f9db125ea3504b7ddc2213480255dc32783d163814d393b3be435`。本轮不追随后续上游，未修改生产、测试包装或验收目标。Python 为 3.11.17，各次运行使用独立 pytest 临时根、外部证据和 2GiB 内存与交换空间合计预算。61 基线的失败只作为中间版本历史，未混入本轮结论。

## 实际结果

| 执行 | 结果与范围 | 时长 |
| --- | --- | --- |
| latest83-normal | 单独 normal 通过，完成真实扫描提交重启、来源发布、三类交付、累计 ACK 与媒体导入。 | pytest 72.59 秒，包装墙钟 73.5693 秒；setup 22.12 秒、call 50.18 秒。 |
| latest83-all | 按同模块一个 pytest 进程选择全部八项，使用 -x；首个 normal 在扫描提交后实际重启，随后来源报告等待 150 秒超时。其余七项没有执行。 | pytest 179.92 秒，包装墙钟 180.3834 秒；setup 14.81 秒、call 164.65 秒。 |

两次源码与测试文件摘要相同。单独通过不能替代全八项验收；本轮没有完整通过结果，也没有同一最终生产基线的原版成功耗时对照，不能宣称稳定业务提速。

## 客户端与资源证据

单项 normal 的真实客户端只启动一个 Node、一个 Application 和一个 Files；16 个操作全部通过，分别为 export 7 次、报告 import 4 次、import-media 5 次。startup 为 3.3400 秒，全部操作合计 1.5794 秒，close 为 0.0742 秒。13 个被测 CLI 资源组均落实 2GiB 合计限额，最大合计峰值 151,187,456 字节，OOM 和触限事件为零，全部组与外层委派目录移除。

全模块运行只完成首次 export，客户端正常 close；六个被测 CLI 资源组均落实 2GiB 合计限额，最大合计峰值 313,610,240 字节，OOM 与触限事件为零，全部组与外层委派目录移除。两次启动器 errors 均为空，remaining_pids 均为空。失败运行不计作完整功能或业务预算验收通过。

## 本轮失败位置与已核实事实

全模块运行已经到达实际 save_result_page 提交后的 scan 门闩；旧 host 与执行组已停止，新 host 已接收原请求。等待结束时 capture 仍为 RUNNING；PREPARE、START 和 STOP 的结果事件分别为 8、18、27；CHECK_CAPTURE_RESULTS 的原 run 4、attempt 4 未保存结果事件，segment_sets 为空。重启后没有新的 ADB 结果扫描或媒体读取，最终因缺少预期来源报告超时，未出现 result=error。

两次安装版 CLI 均输出 RuntimeWarning：`resume_segmented_preparation_failure` 从未被 await。当前生产 `capture/preparation.py:355` 以同步 if 调用该函数，`capture/segment_completion.py:774` 将它定义为 async。这个缺失已核实，但它是否直接造成本次恢复超时尚未独立证明；单项 normal 曾在同一告警下完成。没有根据这一线索自行修生产或再次运行。

测试观察器包装的四个入口 save_result_page、finish_segment_recording、fix_segment_selection 和 record_report_bytes 仍是同步仓储／策略函数。scan 门闩记录只在原生产调用完成且事务结束后读取事实并暂停；本轮没有发现观察器接口落后的证据，也没有修改它。

完整命令、源码身份、测试摘要、日志、XML、阶段与预算回执分别使用 latest83-normal 和 latest83-all 标签保存；派生摘要为对应的 `*-summary.json`，客户端与逐 CLI 资源收据在对应 `*-worlds/`。同期其他组在独立工作区运行，这些单次采样不外推稳定性能比例或目标硬件结论。native-tail 原偏移恢复证明局限仍以 audit.md 为准，未改变门闩。
