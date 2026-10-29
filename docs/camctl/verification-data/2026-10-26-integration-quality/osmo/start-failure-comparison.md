# 首次 START 等待的失败定位对照

统一生产基线为 `61c675710d8350a96f2f7d164f5ff1d6a220684f`。本次只在 `test_osmo_builtin_record_segmentation_roundtrip.py` 首次构造 HostDemo 后、启动与提交前创建 `demo.watch_failures()`，再将其传给原 90 秒 START 等待。成功 SQL、90 秒预算、其他提交门闩、八参数和 29 条原断言保持。

该观察器在本次操作前固定 module.log 读取起点。原成功谓词仍先执行；尚未成功时，观察器发现本次 host 的 run 明确失败，直接给出该次调用日志。正常 run 退出并不因此被视为失败。本改动只覆盖首次 START 等待。

| 执行身份 | pytest 总时长 | 包装墙钟 | call 阶段 | 实际结果 |
| --- | --- | --- | --- | --- |
| 同生产、测试文件恢复为 509002c2 原版，fixed-baseline-normal | 113.58 秒 | 115.0907 秒 | 96.4367 秒 | START 原等待自然超时；一次性 export 后生产 cleanup 报 SessionContext.database 缺失。 |
| 同生产、已复用 ClientSession，未接本次观察器，fixed-after-normal | 109.71 秒 | 110.6816 秒 | 95.88 秒 | START 原等待自然超时；相同生产错误。 |
| 同生产、已复用 ClientSession，接入首次 START 失败观察，fixed-after-start-failure | 17.89 秒 | 18.3611 秒 | 5.12 秒 | 发现本次 run 调用 1 已失败后立即报告；相同生产错误。 |

第三项失败信息为 `START 尚未可靠保存: 本次 host 的 run 调用 1 已失败`，保留 CLI 的 `state_db_error`：cleanup 访问缺失的 `SessionContext.database`；随后关闭诊断为对 None 执行 await。原版独立对照也复现上述错误，因此不能归因于会话接入。生产责任已交主代理处理，本工作区没有改生产。

前两项没有本次失败观察器。第二、三项之间只增加本次两行接入，适合说明失败定位提前；第一项另外包含客户端启动方式差异。所有运行均失败，不能据此宣称完整业务提速或八项联合链通过。准备阶段和同期独立工作区负载不同，本记录是单次诊断样本，不能外推稳定性能比例。原 90 秒合法成功预算没有缩短。

改后四个被测 CLI 资源组均落实 2GiB 内存与交换空间合计限额，最大合计峰值为 84,365,312 字节，OOM 与触限事件为零，全部组移除。客户端 startup、唯一 export 和 close 均完成，Node 状态为 closed，实例预算为 120 秒；没有 HTTP 或浏览器进程。预算启动器 errors 为空、remaining_pids 为空，委派目录移除。业务失败不记作完整功能或业务预算验收通过。

完整命令、源码提交与测试文件摘要分别保存在同标签的 `*-command.json`；日志、XML、阶段及预算收据保留同标签身份。改后派生摘要为 `fixed-after-start-failure-summary.json`，参数和原断言的 AST 不变核对为 `fixed-after-start-failure-static.json`。Python 3.11.17 的 py_compile 与 git diff --check 通过。没有启动全部八项，也没有推广到其他门闩。
