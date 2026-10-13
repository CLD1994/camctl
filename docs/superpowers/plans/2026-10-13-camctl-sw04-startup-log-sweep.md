# SW-04：默认启动日志遗留清理实施计划

> 供执行 Agent 使用：按 `superpowers:executing-plans` 或已经明确指派的子任务执行。先验证默认入口未清理的失败，再接入生命周期；每项都核对实际文件操作、锁持有和诊断结果。

**目标：** 每次取得会话锁的 `run` 安排一次日志遗留检查，清理符合条件的旧副本与旧标记临时文件；设备推进不等待清理完成，状态库不可用仍可独立检查。

**组织建议：** 在会话锁取得之后、业务文件创建之前登记本次一次性清理拥有者。使用本地文件工作线程执行检查，运行内文件归属登记排除本次文件；会话收尾等待实际操作交付后再释放会话锁。

**技术基础：** Python 3.11、既有 `startup_sweep`、文件执行器、本地责任收场和会话锁。

**设计依据：** [启动遗留日志副本检查](../../architecture/log-delivery.md#run-启动时清理遗留日志副本)、[标记临时文件](../../architecture/log-failure-marker.md#故障标记的临时文件)、[实际文件完成](../../camctl/file-runtime.md#本地文件操作的完成与故障边界)、[会话启动](../../architecture/cli-commands.md#run-的启动顺序)。本计划承接[路线图 SW-04](2026-09-30-camctl-implementation-roadmap.md#文档复核发现的实现缺口)，消费[SW-02 诊断出口](2026-10-13-camctl-sw02-log-copy-diagnostics.md)及[SW-03 保存归属](2026-10-13-camctl-sw03-marker-temp-cleanup.md)。

## 当前入口与文件责任

2026-10-13：`logging_runtime/copies.py::startup_sweep(logs_dir: Path, *, active_names=()) -> SweepResult` 只有定义与直接测试，未接入生产启动。它按专用名称删除非活动文件，但 `active_names` 在调用时冻结，不能自行证明并发中新建文件属于本次运行。

`session/service.py::_run_session` 取得会话锁后打开状态库；`on_session_open` 只在数据库打开成功后调用。另一方面，`bootstrap/lifecycle.py::build_runtime` 的预检查可设置 `startup_error`，`execute_command` 当前在此情况下提前返回，尚未取得会话锁。因此，仅在 `on_session_open` 或正常报告装配中调用清理不能覆盖状态库不可用的启动。

下列路径相对于 `apps/camctl`，新增模块与内部接口是建议；实施调整必须同时核对生产者、收场消费者与命令。

| 文件 | 责任 |
| --- | --- |
| `src/camctl/logging_runtime/copies.py` | 保留专用命名、`SweepResult` 和分类清理；增加实时的本次文件归属核对。 |
| `src/camctl/logging_runtime/startup.py`（建议新增） | 一次性检查拥有者、活动归属登记、文件线程结果及诊断交付。 |
| `src/camctl/session/service.py` | 会话锁内启动检查；状态库错误返回和取消时完成本地清理收场。 |
| `src/camctl/bootstrap/lifecycle.py`、`background_flow.py` | 装配拥有者；修正 `run` 的状态库预检查早退接缝，组合实际本地责任。 |
| `tests/unit/logging_runtime/test_startup_owner.py`（建议新增） | 用端口替身核对只安排一次、分类和归属登记。 |
| `tests/integration/bootstrap/test_startup_log_sweep.py`（新增） | 默认 `run`、受限 `run`、数据库失效、并发文件和锁释放的组合验证。 |
| `tests/integration/logging_runtime/test_copies.py` | 补齐实时排除本次副本及临时文件的文件分类测试。 |

## 全局约束与阶段接口

- `submit`、`init`、`describe` 不取得本任务的启动清理责任；`run` 未取得会话锁时不能开始扫描或删除。
- 检查只针对配置已合法的实际 `staging/logs`，不扩大到其他目录。配置或目录绑定已明确不合法时，不绕过错误进行删除；状态库本身不可用不能单独阻止合法日志路径的检查。
- 检查一次，单个文件删除一次；失败残留可在下一次实际 `run` 检查，不增加本运行的轮询、重扫、重投或报告责任。
- 本次创建的文件即使已经关闭，也不是“旧文件”。登记在路径创建前完成，并保留到本次检查结束；副本和每次标记保存临时路径使用同一个本次归属提供者。
- 旧操作的完成资格使用已有 `RecoveryBoundary` 等实际拥有者证据：正式 CLI 已传入 `HOST_LOCAL_SETTLED`，表示调用方完成旧进程组收场；直接装配默认 `UNCONFIRMED` 不能仅因取得锁就假称旧操作结束。未确认的旧文件保留；默认入口测试显式提供对应真实前提，不伪造已关闭事实。
- 建议为 `startup_sweep` 增加默认兼容的 `is_current: Callable[[str], bool] | None` 参数；删除前实时核对，不能把调用时的一份 `active_names` 快照作为全部并发保护。
- 建议一次性拥有者提供 `start() -> None`、`required_settlements() -> int`、`stop_new_work() -> None`、`async settle() -> None`。保存原 `SweepResult`，通过 SW-02 诊断出口呈现错误，不把辅助清理错误抛成状态库失败。
- 建议在 `SessionContext` 增加 `on_session_locked: Callable[[], None] | None` 与 `startup_outcome: SessionOutcome | None` 默认字段。取得锁后启动拥有者；若已有状态库预检查错误，保留该结果，不进入普通数据库及设备工作，仍完成清理收场。
- `run` 的数据库预检查错误走上述同一锁及收场路径；配置错误仍按原规则退出。不得新增第二套会话锁取得／释放逻辑。锁异常的分类由[SW-05](2026-10-13-camctl-sw05-session-lock-errors.md)核对。
- 上述路径只适用于已经可靠确认目标状态库存在的启动。按 CLI 的先行存在性规则，状态库明确缺失或存在性无法判断时在取得锁前退出，不为清理重新取得锁。预检查必须保留失败阶段，不能只凭最终 `state_db_error` 字符串决定是否进入锁内路径。
- 复用项目本地文件线程与实际结果跟踪机制，不让主事件循环执行目录扫描；会话锁保留至检查、删除、句柄关闭及必要诊断交付完成。等待协程取消不代表工作线程停止。
- 各入口、诊断与测试运行按[测试指南](../../../apps/camctl/tests/AGENTS.md)核验，不需要真实设备或物理断电。

## 启动及文件分类

| 启动条件 | 本次检查安排与业务结果 |
| --- | --- |
| 正常 `run`，会话锁已取得 | 安排一次，随后普通业务可独立推进；清理不是设备执行前提。 |
| 时钟检查失败的受限 `run`，会话锁已取得 | 仍安排一次；不取得普通接纳资格，不增加报告机会。 |
| 目标状态库可靠确认存在，但预检查或锁内打开失败；日志路径合法且会话锁已取得 | 独立检查并收场，保留 `state_db_error`，不查询失效数据库来确认日志归属。 |
| 先行存在性检查明确状态库缺失或无法判断存在性 | 按既有 CLI 规则在锁前退出，不安排清理，不自动建库。 |
| 配置／目录绑定明确错误，或会话锁未取得 | 不删除；保留对应实际错误。 |
| `submit` 或其他命令 | 不安排本任务检查，保持原职责。 |

以下文件分类按先后条件判断。取得会话锁只建立本项目会话的排他资格；还须遵守文件拥有者的实际完成规则，不能据此猜测未知用途文件或外部操作已经停止。

| 文件与拥有者事实 | 本次处理 |
| --- | --- |
| 本次创建或正在使用的副本／标记临时文件 | 排除，即使被扫描枚举到也不删除。 |
| 旧副本／旧标记临时文件，专用用途及归属明确，相关操作已结束 | 各尝试删除一次；半成品与完整文件处理一致。 |
| 旧文件仍有实际拥有者操作或完成情况无法确认 | 保留，不等待新的删除机会或增加轮询。 |
| 正式标记，包括有效、不可读或无效内容 | 本次清理保留；不以内容损坏授权复制或删除。 |
| `ready`／`processing` 文件、普通日志、业务报告、未知用途文件 | 不纳入本任务，保持既有所有权。 |
| 目录不存在或无合格旧文件，且可可靠确认 | 结束一次检查，不创建副本、标记或状态库。 |
| 检查失败，或某个删除失败／未知 | 保留错误及残留；单个失败不阻止其他已确认对象，本次不重试。 |

重点复核：数据库预检查提前退出、本次路径在扫描期间出现、取消时工作线程继续、未取得锁零删除、正式标记内容异常。以下任务提供对应反例。

## 任务一：一次性拥有者与并发归属

**消费接口：** `startup_sweep`、专用命名判断、项目文件执行器与 SW-02 诊断回调。

**交付接口：** 一次性拥有者的启动／收场端口与共享本次路径登记。登记／删除核对须具有明确同步边界；新保存不得复用已列入旧文件删除的路径。

- [ ] 新增 `test_start_is_idempotent`、`test_current_names_are_checked_at_deletion_time`、`test_current_temp_remains_current_after_close`、`test_sweep_errors_are_diagnostics_not_business_failures`。以端口替身控制枚举后再登记本次路径，断言排除资格不是冻结快照。
- [ ] 扩展文件分类测试，覆盖正式标记有效／坏内容、旧副本、旧临时文件、未知名称、已不存在、检查失败、删除失败／未知及已知仍活动的对象。旧文件资格不能只靠“不是当前活动”推导。
- [ ] 新增 `test_unconfirmed_previous_owner_keeps_old_files`，对比 `HOST_LOCAL_SETTLED` 下旧文件可清理；归属无法确认时保留，不新增独立探测或杀进程机制。
- [ ] 运行新增专项并保留当前缺少拥有者或并发保护的失败；随后实现一次性拥有者和实时归属核对。用共享短同步边界协调登记及删除资格，不在主事件循环持锁执行慢文件操作。
- [ ] 在日志副本分配身份及 `MarkerStore` 创建临时路径之前登记本次路径，记录本次曾创建集合；SW-03 清理本次文件仍由保存拥有者完成，启动清理不接管它。
- [ ] 把 `SweepResult.error` 和逐文件失败通过 SW-02 出口交付；拥有者结束后不再重扫。复跑单元与 logging_runtime 集成专项。

## 任务二：默认启动、早期错误与实际收场

**消费接口：** 任务一的拥有者、`SessionContext` 锁内回调、本地责任组合及默认装配。

**交付接口：** 正常、受限和状态库错误启动共用一次锁内清理路径；锁持有覆盖所有已开始的实际清理责任。

- [ ] 在新增 bootstrap 文件加入 `test_default_run_sweeps_old_log_files_once`、`test_restricted_run_also_sweeps_once`、`test_state_db_startup_error_still_sweeps_under_session_lock`、`test_no_session_lock_means_no_sweep`。不得用直接调用 `startup_sweep` 代替默认入口测试。
- [ ] 加入 `test_missing_or_uncheckable_database_stops_before_lock_and_sweep`，与可靠存在但数据库内容损坏的对照分别构造；验证不会为了清理越过先行存在性规则。
- [ ] 加入 `test_capture_progresses_while_sweep_worker_is_paused`、`test_new_copy_and_temp_survive_overlapping_sweep`、`test_cancel_wait_keeps_session_lock_until_sweep_ends`。真实目录与受控线程同时出现新文件，断言设备流程已推进、文件未误删、第二个 `run` 在真实收场前不能取得锁。
- [ ] 先运行专项，确认旧文件在默认入口实际残留。装配锁取得回调，把清理拥有者加入本地责任；保证该回调不等待检查完成，后续设备推进与清理相互独立。
- [ ] 改造 `execute_command` 中目标库可靠存在但预检查失败的 `run` 早退：使用最小会话装配进入同一锁内启动及收场，保留预检查阶段和错误，不启动报告进程、普通流程或重复打开已知失效数据库。`submit` 早退、配置错误及锁前存在性失败职责保持。
- [ ] 审计 `_run_session` 的成功、受限、受理失败、打开失败、取消及异常出口：每次只启动一次拥有者；全部已开始的文件操作结束并交付诊断后才释放会话锁。清理失败不得改写原退出分类。
- [ ] 覆盖第一次删除失败、下一次 `run` 清理成功，期间没有复制或补投旧日志。扩展真实 CLI 场景核对 stdout 单条会话结果及 stderr 诊断，再分别执行 session、bootstrap、logging_runtime 目录。

## 验收命令与收口

从仓库根执行，新增文件将在实施阶段创建：

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/logging_runtime/test_startup_owner.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap/test_startup_log_sweep.py -q
```

完整门禁分别执行 `unit`、`integration/logging_runtime`、`integration/session`、`integration/bootstrap`，复验 SW-02 诊断及 SW-03 保存临时清理；不得以辅助函数测试替代默认接线证据。

- [ ] 独立核对默认启动、所有早退和收场入口，实际扫描／删除次数、文件集合、设备推进及锁持有证据符合两张分类表。
- [ ] 记录解释器、环境、命令、日志、退出码和实际结果，复核后更新路线图 SW-04，形成独立修复提交。
- [ ] 将最终锁内启动回调和早期错误路径交给[SW-05 会话锁错误分类](2026-10-13-camctl-sw05-session-lock-errors.md)，逐入口核对分类及句柄释放。

2026-10-13：计划已编写，生产修复与业务测试尚未执行；不需要真实相机或目标主机。
