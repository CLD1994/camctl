# 三项测试的目标与成本审计

工作区：`/tmp/camctl-quality-history-20261026`，分支 `test/quality-history-20261026`，源码 `509002c25308eda98e0096068d3510485938027e`（已包含 `81e100fb2fa3c982a0212997be9388b0ffbded16`）。本记录只描述这三项测试，不代表目录回归或整个程序资源验收。

解释器实际入口为 `/workspaces/camctl/apps/camctl/.venv311/bin/python3.11`，版本 CPython 3.11.17。任务所给 `python3.11.17` 文件名不存在，首条命令退出 127，随后改用该虚拟环境已有入口。所有 Python 源码搜索路径绑定本工作区；临时目录、缓存、pytest basetemp 在源码外私有目录。history 与 bootstrap 分进程顺序执行。未修改生产代码、协议、依赖或测试文件。

## 报告选择

`test_large_window_releases_overflow_once_and_keyset_pages_do_not_rescan_window` 验证固定 H 与水位窗口中对象选择的两条路径：候选数不超过 4096 时保存有界去重 ID，超出后释放候选、按对象 ID 从索引分页续读；窗口预查询每类型一次，父对象切换不重做。

依据是 `docs/camctl/database/report-changes.md` 的“有上限的候选预查询”“大范围按对象 ID 续读”“成本与故障边界”，以及 `historical-state-query.md` 的“按最后一个排序值续读”和“续读成本与中断”。真实受理生成事件、报告目录、父子关系和当前主表；真实 SQLite 执行索引选择、VM 指令统计、短读连接和关闭。这些无法由内存替身证明。此目标不需要完整 CLI 进程，也不承担整个程序内存预算。

准备为真实受理 3 + 4200 + 3 个动作，并真实完成首动作的 128 个产物。4200 必须越过 4096，17 条页产生 248 页，128 个其他类型产物保留为无关数据。三份计划使父对象切换及前/末稀疏窗口可以独立观察。本项没有重复建立相同大世界。

独立预期直接来自输入身份：计划为 [1,2,3]，大计划动作是 4..4203，末计划为 4204..4206；第一页是 4..20。候选弱引用应仅剩三份计划，退出后全部消失。实际 SQL 预查询应为 plan/action 各一次；续读 SQL 不含大 IN 表，实际计划使用父成员和实体变化索引且不临时排序，页 VM 最大值远小于预查询；两稀疏窗口工作量远小于大窗口。

明确错误反例包括超限后仍保留候选、每页重扫固定窗口、用 OFFSET 重读前缀、跨父对象重复预查询、混入窗口外对象和漏掉末页。当前 SQL 观察器依赖具体 SQL 前缀、别名与索引文字，这会约束等价 SQL 重构；该事实不是删除性能观察的理由。

## 分段历史容量

`test_large_growth_crosses_page_and_disk_cache_capacity` 验证当前投影恢复至磁盘历史句柄、按 7 条读取成员与缺口、生成快照时的有界处理和规模增长。原规模是 256/2048 个成员；文件名跨每目录 999 个文件的 CAM 目录边界，真实原结果页、固定文件事实、集合、缺口和默认范围逐批保存。

依据是 `docs/camctl/database/segments.md` 的同一 H 增长历史分页与旧整对象入口边界，`historical-state-query.md` 的分批读取、内存和续读成本要求，以及 `implementation.md` 的按需加载、缓存和及时释放要求。SQLite 原库及临时磁盘库必须真实存在，才能核对源批量读取、临时库页数/页大小及 2MiB 页缓存。当前同 pytest 进程执行并不证明整个 camctl 及后代的 2GiB 预算；`tracemalloc` 仅是 Python 分配诊断，RSS 高水位仅打印而未作为通过门槛。

`_large_fixture` 中 `advance_segment_assessment` / `finish_segment_assessment` 用来形成合法持久前态，不是用同一算法算测试预期。真正独立预期由输入决定：成员数 N、只有首尾非零所以缺口数 N-2、源 fetch 不超过 7、当前路径无需事件回放、缓存为 -2048KiB、大世界临时库超过 2MiB、快照块不超过 65536 字节。规模增大 8 倍时，Python 峰值应小于原值三倍且增加不足 1MB，源 SQL VM 指令增长小于 12 倍。

明确错误反例包括一次加载全部源行、按页重扫前缀导致近二次 SQL 工作量、把增长记录保留为 Python 全量对象、完全留在小于磁盘缓存的规模，以及回退全事件回放。`largest_fetch` 的实际观察范围是 `HistoryRepository._connect` 返回的源连接，不涵盖临时句柄自身的每次 fetch。快照在本项只核块上限，完整内容/三路径一致性另由同文件 `test_stream_snapshot_matches_all_paths_and_preserves_later_pending` 等用例承担，不能把本项单独描述成快照内容正确性的完整验收。

当前每条 SQLite VM 指令回调一次，同时在 `tracemalloc` 开启期间逐步更新 Python 整数计数。这能提供精确的源 SQL 工作量，但观察本身会增加时间与 Python 分配活动。不能在没有替代观察和误差依据时直接删除或稀疏采样后仍宣称原精确观察不变。若要把窄分配诊断换为独立进程合计预算、拆开容量责任或改变目标，需要先确定承接与验收语义。

## 首轮发现期间受理

`test_submit_during_initial_discovery_is_not_lost` 验证默认会话在批次大小 1 的初始取回发现过程中，真实受理后来计划后仍继续发现并执行。

依据是 `docs/architecture/protocol-session.md` 的新计划受理序列：固定本轮观察上界 L，完整吸收后才推进 last_seen；`scheduling-execution.md` 的新计划发现与内存调度缓存要求，以及运行中 run 持续吸收新受理计划的行为要求。

准备是真实初始化、受理并完成两设备照片、真实受理三个取回动作。同步点是第一次 `driver.open_read` 调用内部：先确认受理连接未在事务中，真实提交 request3，再继续原读取。新动作时间比旧动作更早，防止仅按旧调度游标向后前进遗漏新计划。真实数据库、受理事务、异步任务和实际文件拷贝必须组合；设备边界继续用受接口约束的读取流替身。该用例没有独立 submit CLI 进程，不能作为跨进程锁和收尾交接的证据。

独立预期为同步点确实进入、4 个取回动作均成功、两照片乘 4 次取回共 8 次读取。若初始化末尾错误地把发现水位推进到新计划而未装入它、在初始页终点退出，或仅使用旧队列，则新动作仍 pending 或读取次数为 6。本项保留真实同步点和 30 秒成功预算，没有随机 sleep；poll_interval=5ms 只是测试轮询。当前没有已证实可删的重复准备。

启动发现测试使用默认 `execute_command` 装配。代码中的 `WorkerSupervisor` 默认通过 multiprocessing spawn 启动真实报告子进程，所以 2.59 秒目标阶段也包含实际报告维护与子进程收场；本项并非纯函数调用。它仍没有独立的 CLI/submit 进程，也没有进程组预算证据，不能宣称整个程序内存验收。真实报告子进程是当前默认装配组合的一部分；是否为此发现水位目标保留全部报告职责属于测试责任划分，不能在本次审计中未经讨论替换。

基线结束后的真实库只读核对：报告世界有 3 计划、4206 动作、128 产物、128 设备文件、4864 历史事件、395 次历史事务和 4344 条报告变化关联。256/2048 分段世界分别有 532/4158 次历史事务、797/6229 条历史事件、256/2048 成员和 254/2046 缺口，均为 WAL。计数原始证据见 `baseline-world-counts.json`。

## 当前测量结果与后续边界

原样 history 基线为 2 passed，pytest 总计 314.45 秒，退出 0；报告选择 call 210.89 秒，分段容量 call 100.28 秒。bootstrap 外部窄计时包装为 1 passed，总计 3.87 秒，退出 0；call 3.1416 秒，其中两照片准备 0.5281 秒、旧计划受理 0.0145 秒、目标 build 0.0026 秒、execute 2.5939 秒（含同步点内新受理 0.0091 秒）。

history 分阶段诊断为 2 passed，总计 274.68 秒，退出 0。它只包裹原辅助函数与 pytest 阶段，不改变输入、断言、容量或生产设置。报告 call 182.9055 秒，四段准备分别为首三动作 0.2763 秒、128 产物完整处理 135.9108 秒、4200 动作受理 46.1340 秒、末三动作 0.0197 秒；准备合计 182.3407 秒，全部查询、观察、GC 与断言的剩余时间为 0.5648 秒。该剩余值不是独立的纯 SQL 时间，不能进一步拆成没有实际计量的目标与检查数字。

分段 256 成员准备 7.7587 秒，恢复打开 1.4278 秒，读成员 0.0260 秒，读缺口 0.0202 秒，准备快照 0.2095 秒；2048 成员准备 68.6212 秒，恢复打开 11.0454 秒，读成员 0.2022 秒，读缺口 0.1866 秒，准备快照 1.6056 秒。两种规模准备合计 76.3799 秒，目标操作合计 14.7232 秒，GC、块检查、关闭等剩余 0.0890 秒；整项 call 91.1921 秒。计时包装在原分配观察窗口内写少量 JSON，会增加观察活动；诊断数字与原样基线的同期负载、初始化和缓存条件不同，不能把差值当作提速。

原样基线的 256/2048 Python 峰值为 2318015/781643 字节，诊断轮为 782487/778292 字节。较小规模的两轮差异说明该峰值受初始化与缓存条件影响；当前没有进一步采样，不能编造具体分配来源。精确 SQL 工作量与原样基线相同：48381/348413 VM 步、最大源 fetch 7，实际临时磁盘为 610304/4562944 字节。证据不支持把每步 VM 回调断言成主要耗时，也不支持把 Python 峰值当成整个程序预算。

### 128 产物准备的静态核查

`_finish_many` 使用 `_runtime` 的 `_PAGE_EVIDENCE`，其中已经登记正式 `RESULT_PAGE_CONTRACT`。`ResultsDouble.list_page` 第一次调用就把全部 128 项装入同一 v2 `ResultPage`，`cursor` 和 `next_cursor` 都为空；因此当前已经是正式接口允许的最大单页输入。生产 `advance_result_scan` 实际保存该页，随后 `_register_listing` 按保存页取出 batch，`_register_observed` 才逐文件保存发现、归属与完成事实；不是测试替身逐文件调用驱动。仅把替身改成“同样 128 项一批”不会减少这条路径。

可讨论的测试侧候选是保持 128 个真实文件及相同正式产物，把准备输入改为多个合法 v2 页，借助真实 `DirectoryCursor` 逐页提供并由生产保存。这样可能缩小每次文件事实验证所引用的页，但尚未验证收益，也不能保证所有消费都只查单页。它会改变 APPEND/INDEX_FILES 页事件数量、页事件绑定与完整历史边界；即使公开对象 ID 和输出数保持，仍须独立核对原前/末业务水位窗口、报告变化目录、固定 H、文件完成依据及真实事务结果。因此该候选需要用户讨论，不属于无语义变化的局部调整，本次未实施。新增跨页替身与关系应复用已存在正式分页契约，不自行模拟批量数据库端口或直接填 SQL 前态。

本轮没有改动仓库文件、没有提交。全部范围已有一次当前通过与分阶段记录，按协调要求停止继续测试。命令、插件源码、日志、XML、退出码、环境与库计数分别保存在本目录；`phase-summary.json` 汇总阶段原始值与计时范围。

### 无关产物数量的责任复核

未找到报告对象选择必须包含 128 个无关 output 的独立规格依据。4096 候选溢出和每页 17 条的容量边界由 4200 个动作承担；128 个 output 不跨过该候选容量，测试也不核对照片 128 项写入的分页、批量或事务形态。当前只能确认它提供了“存在无关类型及不同父对象”的见证，不能据此把 128 当作报告容量的必要下界。

现有跨类型与父对象过滤的具体承接为：

- `history/test_report_selection.py::test_all_five_targets_keep_pages_parent_filter_and_legal_empty` 在缓存 4096/0 两条路径上，精确断言 plan/action/output/delivery/diagnostic 各自 ID、不同父对象空页和空窗口；它使用两个真实产物与交付。
- `history/test_report_selection.py::test_delivery_target_only_fills_obtain_ancestors_not_source_entities` 在缓存 4096/1 路径上，断言交付只补齐取回动作祖先，来源计划、来源动作与来源产物均不入选。
- `history/test_report_selection.py::test_selection_pages_preserve_targets_and_necessary_ancestors` 用真实照片完成取得一个 output，断言两个计划各自 action，第二计划的 output 为空，第一计划的 delivery 为空，并独立断言 diagnostic。

真实拍摄保存与消费的较窄承接为：

- `capture/test_result_scan_runtime.py::test_photo_consumes_original_pages_and_precise_output_failure[photos]` 经真实两页和生产 capture_handler，断言照片动作成功、没有错误、两个正式 output，并确认驱动列举两次。
- `capture/test_result_scan_runtime.py::test_source_file_consumer_continues_after_its_first_batch` 实际保存并登记 33 个文件，断言来源读取为 32+1、最后游标结束，登记后总数仍为 33。
- `capture/test_baseline_history.py::test_128_entries_form_one_complete_history_chunk` 验证 128 条真实基准保存、固定及按原事件读取；相邻 129 条单块拒绝与 `capture/test_result_page_history.py::test_baseline_comparison_crosses_chunk_boundary_and_persists_next_item` 验证基准块边界和跨块差集。它们不是 128 张普通照片完成发布的等价用例。

本轮未找到现有 capture 测试对 128 张普通照片逐项保存并正式发布的完整等价验收。分段持久化已有 129/513 项真实完成写入与核实，但动作合同与完成入口不同，不能冒称等价承接普通照片路径。因而可向用户提出“保留少量无关类型见证，并保留全部 4200/4096/17 报告边界”的责任划分建议；在是否需要独立保留 128 张照片全流程规模上仍须明确决定，不能宣称覆盖已无损转移。本次未改当前规模，未为该建议重新运行测试。
