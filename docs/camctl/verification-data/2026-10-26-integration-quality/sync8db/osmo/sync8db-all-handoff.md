本次验证固定在 70149be69088714b8574900b57efa1b16fe3fa68，使用 Python 3.11.17、Node.js 24.22.1、独立 Node 依赖、uv 缓存、构建与临时根。测试 SHA-256 为 e295d5a5288f9db125ea3504b7ddc2213480255dc32783d163814d393b3be435。没有修改仓库跟踪文件，无提交。

原完整文件选择八项并保留 -x。首个 normal 在 test_osmo_builtin_record_segmentation_roundtrip.py:223 等待预期来源报告满 150 秒后失败，七项未执行。pytest 为 179.34 秒，外包装 181.3285 秒，退出码 1。客户端每操作预算仍为 120 秒；本次仅完成首次计划 export，客户端正常关闭。

本轮直接证实：scan 门闩在真实 save_result_page 提交后到达，旧执行组停止，新 host 重送原请求。重启后 CHECK_CAPTURE_RESULTS 的 run4/attempt4 已保存 result_event_id=60，action capture 仍 RUNNING，last_event_id=63。分段集合 id1 已创建（created_event_id=62，current_final_page_event_id=43，state=1），尚无 published_event_id，未见 finish_segment_recording 日志，未产生预期来源报告。

本轮同键日志证据：deployment-camctl.log 的第220行记录 release_occupancy、resumed=false、_ActivityReleaseCommand、outcome=completed；第221行 segment_save 的 release_occupancy 标记 resumed=true；第222行记录同一命令 resumed=true、outcome=completed。两个 db_transaction 使用相同 operation_key=3e1071edb2f5b98dd5eea9aebbd01583。此前第218/219行 create_result_set 已完成。精简日志保存在 sync8db-all-selected-log.json。

因果边界：当前证据表明推进停在集合创建与占用释放之后、来源终态发布之前。同键普通与恢复调用和停滞在本次运行中共现；本次未捕获运行时 pending 映射或 Python 栈，尚不能独立证明 Future 覆盖、重复应用或其他机制直接造成该停滞。需要用户修复生产后再由 root 集成并复验。

资源收场：六个 CLI 专属组均实际落实内存与交换空间合计 2GiB 上限，最大合计峰值 282722304 字节，oom_kill/failcnt/memsw_failcnt 均为零；六组已移除。外层 launcher errors=[]、remaining_pids=[]，委派目录已移除。本次属于业务失败，不能记录为完整业务预算验收通过。最终核对 pytest、六个安装 CLI、Node 和 tsx 已记录 PID 均已不存在；按实际 argv 核对，没有本次 runtime 的 host/CLI/client 遗留进程。证据在 sync8db-all-final-processes.json、sync8db-all-budget/launcher.json 及 worlds 下 resource-receipts.json。

可归档轻量文件位于当前目录：command/result/summary JSON、pytest 日志/XML、stages 与 progress、selected-log JSON、live/final DB JSON、824KiB 的 final-state.db、ADB/安装来源/门闩记录、wheel 与摘要、安装命令回执、worlds 下客户端/模块/CLI日志与资源收据。全量原始时钟读取日志仍在工作区 tmp/osmo-installed-chain-archive-1643509 与独立 runtime 中；轻量集不复制该高频日志。
