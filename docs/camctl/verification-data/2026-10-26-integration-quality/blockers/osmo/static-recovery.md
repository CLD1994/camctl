# OSMO 原结果页恢复停滞的静态证据

本记录调查扫描原页提交后终止旧 CLI、再启动新 CLI 的正常多段场景。静态代码核对基于 `/tmp/camctl-failure-repair-20261026-4f5dfpqh/osmo`，分支 `fix/integration-osmo-4f5dfpqh`，HEAD `1e697c89b311fdc4de032ae7dd25ba22252ce5ae`。调查开始时工作区干净；后续 `apps/client/node_modules` 软链由同组实测使用。本调查未修改仓库文件，也未运行测试。

## 旧现场的确定事实

状态库为 `/tmp/camctl-quality-osmo-runtime/latest83-all-pytest/test_installed_segment_source_0/world/deployment/state.db`；日志为同目录 `camctl.log`。状态库通过 SQLite URI `mode=ro` 读取，未忽略 WAL。

原结果责任是 `operation_runs.id=4`、`responsibility_key=results/1`，已使用一次尝试。尝试的数据库行 ID 为 4，`attempt_no=1`；代码票据的 `attempt_id=1` 表示该尝试序号，不能把数据库行 ID 4 当作第四次尝试。该行仍为 RUNNING，原意图事件为 31，首末原页事件均为 39，`result_event_id` 为空。`segment_sets` 行数为 0。

可复核查询为：

```sql
SELECT id,kind,status,attempts_used,responsibility_key
FROM operation_runs ORDER BY id;

SELECT id,run_id,attempt_no,status,intent_event_id,result_event_id,
       result_first_page_event_id,result_last_page_event_id
FROM operation_attempts WHERE id=4;

SELECT id,transaction_id,event_type,body_json
FROM history_events WHERE id BETWEEN 39 AND 45 ORDER BY id;

SELECT id,identity_key,presence_state,source_action_id,role,
       completion_state,size_bytes,last_event_id
FROM device_files ORDER BY id;

SELECT COUNT(*) FROM segment_sets;
```

原页及重启后的事务按下表推进：

| 历史事件 | 事务 | 已保存内容 |
| --- | --- | --- |
| 39 | 28 | 原 run 4、attempt_no 1 的第一页实际返回；`next_cursor=null`，`set_finalized=true`，四份 LRF／OSV 条目均完成。完成依据引用 STOP 事件 27，要求等待 3000 毫秒，实际等待 3000078469 纳秒。 |
| 40 | 28 | 为原页 39 保存四份文件的索引，文件 ID 为 1–4。扫描观察门闩位于该事务完成之后。 |
| 41 | 29 | 新会话推进可信时间下界。 |
| 42 | 30 | 文件 1，即 `0007_D.LRF`，由未确认在场变为 PRESENT。 |
| 43 | 31 | 文件 1 按原页 39 保存动作 1 的来源归属，角色为 3。 |
| 44 | 32 | 文件 1 保存完成事实及大小 16000，仍引用原页 39 和 STOP 事件 27。 |
| 45 | 33 | 文件 2，即 `0007_D.OSV`，变为 PRESENT；尚未保存来源归属和完成事实。 |

文件 3、4 尚未推进来源归属和完成事实。以上事件证明新进程已执行原页消费，并且数据库 worker 至少成功提交到文件 2 的在场事实；停滞范围是该保存的回执及后续子阶段交接，不是从新进程启动起完全没有调度或数据库执行。

旧 `camctl.log:180`–`:188` 给出更窄的位置。以下为原日志中对应行的字段摘录：

| 日志行 | 内容 |
| --- | --- |
| 180、181 | 文件 1 的 `save_file_presence` 事务完成，`resumed=false` 的 `segment_save` 随后结束。 |
| 182、183 | 文件 1 的 `save_ownership_from_result_page` 事务及原保存阶段结束。 |
| 184、185 | 文件 1 的 `save_file_completion` 事务及原保存阶段结束。 |
| 186 | `save_file_presence`，`resumed=false`，key 为 `b8161c3581795724069bcae9804879a3`；事务 COMPLETED，写入一个事件。 |
| 187 | `save_file_presence` 的 `resumed=true` 保存阶段结束，耗时 5.754 毫秒。 |
| 188 | 相同 key 的 `save_file_presence`，`resumed=true`；事务 COMPLETED，没有新事件，按原操作返回已有结果。 |

日志没有该 key 的 `resumed=false` 保存阶段结束记录。它直接证明同一原申请被正常路径和恢复路径重复提交；“恢复阶段先结束、原保存阶段没有结束”的形态与回执被另一个等待者取得相符。旧现场没有保存 Future 身份和 await 链，单凭这些日志不能宣称已经直接观察到旧进程内的孤儿 Future。

## 原页沿原责任恢复的代码路径

`apps/camctl/src/camctl/capture/handlers.py:5273`–`:5287` 发现原 RESULTS 尝试仍在运行后，取得原票据，核实恢复证据并读取原末页。`:5062` 以 `next_cursor is None` 判断原页已经实际返回；`:5066`–`:5076` 将原票据、原页时刻和 `SavedResultEntries` 放入待收场结果，尚不判定结果处置。该分支直接交付原可靠页，不先执行新的 ADB 目录或元数据调用。

`apps/camctl/src/camctl/capture/segment_completion.py:327`–`:336` 取得该 listing 后，逐份登记原页文件。`:138`–`:155` 对每份文件先保存在场事实，再保存原页来源归属；完成事实随后保存。全部文件登记完成后才到 `:364` 的 RESULTS 收场，再于 `:365`–`:368` 建立结果集合。因此原结果事件为空和 `segment_sets` 为空，是停在文件 2 在场保存阶段的自然结果，不能独立证明恢复丢掉了原结果责任。

该现场走分段保存入口。`apps/camctl/src/camctl/capture/handlers.py:4917`–`:4923` 将 OSMO 分段策略分流到 `register_segment_observations`；上述推进函数也直接调用该函数。它没有走普通文件链的 `_register_observed`／`_save_file_fact`。

## 正常保存与恢复保存可以同时接手同一分段申请

`apps/camctl/src/camctl/capture/segment_completion.py:42`–`:47` 的 `PendingSegmentWrite` 只保存仓储、方法、参数和 key，没有表示在途协程或已应用响应的字段。`:86`–`:95` 先把原申请放入 `pending_segment_writes[action_id]`，再等待数据库回执；`:54`–`:62` 的恢复入口可直接取得同一申请并再次提交同一 key。

这不是两个设备推进任务才会触发的条件。`apps/camctl/src/camctl/bootstrap/flows.py:214` 在每轮调度开始时恢复原事实，`:240`–`:248` 则为设备保留独立的 `BackgroundFlow` 任务。旧设备任务等待数据库期间，下一轮中央恢复入口仍能运行。恢复路径经 `apps/camctl/src/camctl/bootstrap/lifecycle.py:133`–`:143`、`:191`–`:196`，最终在 `:174` 对共享的 `deps.capture_segment_writes` 调用 `resume_segment_writes`；同一字典于 `:831` 注入设备运行时。文件级恢复在设备占用判断之前执行，单个设备已有后台任务不会阻止它接手待存申请。

根代理已独立复现 executor 的同 key 回执覆盖机制。代码对应位置为 `apps/camctl/src/camctl/persistence/executor.py:258`–`:263`：每次提交都新建 Future，并按 key 覆盖 `_results[key]`。`:400`–`:403` 的完成交付再按 key 取出一个 Future。两次同 key 提交重叠时，后提交的 Future 可取得先执行的结果，先提交的 Future 失去交付目标。这一机制能够解释旧日志中恢复阶段结束而原阶段不结束的形态；旧现场自身仍缺 Future 级直接记录。

分段层还存在独立的所有权问题：`:67` 和 `:100` 都不核对当前申请身份，直接删除 `pending_writes[action_id]`。即使两个提交都得到可靠回执，也不代表两个协程能安全应用同一阶段的响应。当前目录的 [segment-owner-race.py](segment-owner-race.py) 使用真实 `_save` 和 `resume_segment_writes`，只把数据库端口替换为可控的延迟响应；[segment-owner-race.json](segment-owner-race.json) 记录两次并发同 key 提交、原保存返回 `original-response`、恢复保存抛出 `KeyError(1)`。该实验未使用实际数据库 worker，不能充当 executor 实测，但已经证明分段层的双重接手及双重删除问题。需保持的不变量是：同一原申请的数据库结果及事件循环状态应用由同一个所有者阶段完成，恢复入口接手原阶段时不能另起一个竞争应用者。

## 普通文件链的静态边界

虽然本现场不走普通文件链，它也有同类在途窗口。`apps/camctl/src/camctl/capture/handlers.py:693`–`:703` 仅根据原发现的 `response is None` 决定提交，`:710` 才应用响应；`:721`–`:741` 先建立子阶段申请并根据其 response 判断，`:748` 才应用子响应。等待期间没有 Task／Future 所有权记录，恢复调用可以再次取得同一 key。

单调用顺序本身正确：`:2132` 等待全部发现响应后，`:2139` 才取原发现的文件 ID，随后保存子事实。`PendingFileObservation` 的 `facts` 是字典；`:710` 的 `dataclasses.replace` 会沿用该字典，没有看到该父响应替换主动抹掉子事实。整批完成后，`:2220`–`:2227` 才给原 `PendingCallResult` 应用 `result_registered_files` 并删除文件待存申请。这里确定的是并发接手窗口，不能把它写成已证实的单调用次序错误或父替换丢失事实。

## 已排除的解释及验证范围

扫描测试观察器位于 `tests/integration/_osmo_segment_observer.py:52`–`:60`，只包裹 `save_result_page`；`:20`–`:21` 在 marker 已存在时立即返回。文件在场、归属和完成保存都不触发这个观察门闩。结合旧原页已经结束且重启后实际推进到事件 45，当前停点没有证据支持“新进程再次卡在扫描观察器”。

重启后没有 ADB 调用符合这条末页恢复路径；它不足以判定调度器未恢复。数据库 worker 在事件 45 前正常执行；旧日志也记录重复请求的无新事件完成。是否在旧运行中发生了 Future 覆盖，需要旧现场缺失的运行时交付记录才能直接确认，不能用“数据库没有新事件”替代这一证明。

根代理随后仅运行一个带外部诊断 hook 的 normal 样本，[diagnostic-normal-result.json](diagnostic-normal-result.json) 记录 exitcode 0、墙钟 107.45810886099935 秒。该样本带诊断开销，且其他隔离测试组可能并行；它证明该次正常链完成，不消除旧失败和受控所有权实验，也不作为性能收益或其他七组场景通过的证据。
