# Hasselblad 可用性恢复实施验证

本记录保存[实施计划](../superpowers/plans/2026-10-28-hasselblad-availability-recovery.md)的阶段性软件验证。软件替身和测试环境中的结果不表示真实相机或目标主机已经验收。

## H1：录像启动占用释放测试装配

2026-10-28，在独立工作区 `/tmp/camctl-h1-capture-20261028` 的提交 `50691389` 上，使用 CPython 3.11.17 执行：

```bash
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/capture/test_recording_start_release.py -q --tb=short
```

命令退出码为 0，六个测试通过，原始输出位于 `/tmp/camctl-h1-release-final.log`。该提交已集成为 `afbe3109`。测试使用真实业务数据库与正式会话工厂，保留可靠无效果释放、未知结果保留、降低预算、输出范围限制以及在途调用收场的原有分区。该结果不证明哈苏原责任恢复、报告生成或后续可用性检查；这些项目仍需按计划验证。

在同一工作区的提交 `61f5ece2` 上执行下列命令，退出码为 0，11 项通过；原始输出位于 `/tmp/camctl-h1-focus-4.log`，集成提交为 `d47367fb`。

```bash
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/capture/test_hasselblad_start_recovery.py -q --tb=short
```

该测试通过真实业务数据库和会话工厂覆盖相对调焦后的原调用中断、可靠和缺失的恢复边界、同会话调用仍在执行、已保存结果、提交回执未知及原操作键。它不代替安装版故障门与新检查的联合验证。

在独立工作区 `/tmp/camctl-h1-reporting` 的提交 `e0a9c8ee` 上，使用 CPython 3.11.17 执行 `apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/reporting/test_emergency_error_generation.py -q --tb=short`，退出码为 0，六项通过；前台输出未单独保存。集成提交为 `7fed2961`。该范围验证应急错误报告的三种活动状态、原动作错误和固定报告字节，不证明哈苏新检查报告。

同一工作区的完整 reporting 目录另有一次失败：204 项通过、285 项失败、10 项错误，原始日志 `/tmp/camctl-h1-reporting-full.log` 的 SHA-256 为 `e0dab6096747e94cc3a38dfca0627bc01d26a8503d5b65def41f87dfc98aa2c8`。失败均在旧测试装配阶段，分别涉及旧 `accept_input` 调用、将连接直接交给异步数据库接口、旧 `CaptureRuntime` 和旧 `ObtainRuntime` 装配；随后迁移这些装配并重新运行完整目录。

完成报告目录的装配迁移后，在 `/tmp/camctl-h1-reporting` 的提交 `a4f2d225` 上使用 CPython 3.11.17 执行 `apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/reporting -q --tb=short`，退出码为 0，499 项通过。完整日志 `/tmp/camctl-h1-reporting-final.log` 的 SHA-256 为 `508529ab5aa8fba470283bda5325a0758b9b50c6042d6b2daf5c6f7c8f80903b`，该提交集成为 `d6b55655`。这证明报告组件目录，不代替哈苏新检查与报告的联合验证。

在 `/tmp/camctl-h1-capture-20261028` 的提交 `47259000` 上，使用 CPython 3.11.17 对 `test_hasselblad_start_recovery.py`、`test_recording_start_release.py` 和 `test_capture_contract.py` 一同执行 pytest，退出码为 0，38 项通过；原始输出位于 `/tmp/camctl-h1-capture-final.log`，集成提交为 `ca5db88c`。原节点和参数保持：哈苏原恢复 11 项、录像释放 6 项、拍摄契约 21 项；拍摄契约通过正式处理入口和真实结果页事务保留文件登记回滚及原键恢复覆盖。完整目录仍需验证。

同一工作区在该提交执行 `apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/capture -q --tb=short`，退出码为 1：353 项通过、1718 项失败、119 项错误，日志为 `/tmp/camctl-h1-capture-directory.log`。堆栈中大量失败涉及旧 `CaptureRuntime(owned=...)`、旧数据库连接或未等待的会话工厂；这些堆栈数量不是互斥的节点分类，也不表示目标业务已经运行。原有测试正在按共享装配和业务场景迁移。

安装版验证使用隔离入口和 `/tmp/camctl-h1-installed-20261028` 的未提交快照，完成后提交为 `aefcc589`，集成提交为 `9d811033`。证据目录 `/tmp/camctl-h1-installed-prep-20261028` 的 `summary.json` 记录入口退出码 0，实际收集八项并全部通过：missing START、两组已保存 START 和五个历史观察器反例。missing START 场景在替身真实拍摄形成两份工作文件后中断，正常重启沿原责任保存未知，原拍摄没有重放，文件及报告保留。另一轮正常链的四个任务各记录三项通过和 pytest 退出码 0，但入口主进程已消失而汇总仍为 `running`，没有总退出码；该轮不计为完整门禁通过。

## H2：固定身份和证据类别

2026-10-28，在独立工作区 `/tmp/camctl-h2-availability-storage-20261028`，使用 CPython 3.11.17 执行：

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/devices/test_hasselblad_resource_identity.py apps/camctl/tests/unit/devices/test_hasselblad_configuration.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/devices/test_evidence.py -q
```

两条命令退出码均为 0，分别为 37 项和 15 项通过。验证时的提交分别为 `57d58d7a` 和 `fd2b0e56`，集成提交为 `e7409865` 和 `035016f5`。前台输出未保存原始日志。这些测试覆盖身份值、资源键、配置解析和证据类别；不证明身份已经在活动预留时固定，也不证明可用性事务、历史回放或业务调用已经接通。

在 `/tmp/camctl-h2-doctor` 的提交 `34e49c1e` 上使用 CPython 3.11.17 执行以下命令，退出码均为 0，分别为 78 项和 164 项通过；前台输出未保存原始日志。相应集成提交为 `495b7d00`、`10198813` 和 `01bc4dbf`。

```bash
PYTHONPATH=apps/camctl/src /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/devices/test_hasselblad_availability.py apps/camctl/tests/integration/devices/test_hasselblad_registration.py apps/camctl/tests/integration/devices/test_hasselblad_managed_capture.py apps/camctl/tests/integration/devices/test_hasselblad_download.py -q
PYTHONPATH=apps/camctl/src /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/devices/test_hasselblad_responses.py -q
```

测试覆盖独立检查的身份、超时、取消与实际受管进程收场，以及已观察结构化输出、矛盾和无法解析输出的分类。此范围不证明业务调度、事务、真实诊断消费者和真实相机的联合行为；其他工具版本若产生尚未观察的输出格式会被保守拒绝，须在目标设备上验证。

提交 `7422ad1e` 为检查观察补充本次开始和结束的单调钟事实，使用 CPython 3.11.17 执行 `pytest apps/camctl/tests/unit/devices/test_hasselblad_availability_clock.py -q`，2 项通过。事务端尚未消费这些事实并独立核对总期限。

独立可用性观察校验的单元测试与上述时钟测试一同运行，命令为 `pytest apps/camctl/tests/unit/operations/test_availability_observation.py apps/camctl/tests/unit/devices/test_hasselblad_availability_clock.py -q`，10 项通过。测试覆盖检查身份、设备数量、工具退出、步骤顺序、明确 PASS 和总期限；该校验尚未接入检查结果事务。

提交 `fc003b31` 固定 DEVICE 正式源的机身、原路径、长度与摘要，并在登记及读取时核对原拍摄结果、历史事件与文件记录。在独立工作区 `/tmp/camctl-h2-identity-integrate-20261028` 同步包资源后，使用 CPython 3.11.17 运行文件身份、检查时钟及两个相关消费者单元测试文件，49 项通过；复用 `CameraIdentity` 后重新运行两份身份单元测试文件，44 项通过。首次运行因独立工作区缺少生成的包资源而在收集阶段失败，同步资源后上述测试通过。读取机会前的绑定核对及完整业务联动尚未完成。

提交 `c89e8715` 将固定机身接入活动预留、准备绑定失败和 START 身份核对，并在正式源登记时比较活动身份。在独立工作区 `/tmp/camctl-h2-activity-integrate-20261028` 使用 CPython 3.11.17 运行单元目录，首次为 9722 项通过、15 项失败、1 项跳过；失败涉及测试夹具缺少新增身份参数或历史字段。更新夹具后，受影响的五个单元测试文件共 311 项通过。事件登记静态检查通过；这些结果不证明检查事务、同机互斥或设备联动已经完成。

活动结构校验在 `/workspaces/camctl` 使用 CPython 3.11.17 运行 `PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/persistence/test_output_scope_structure.py -q`，退出码为 0，11 项通过。该文件虽然位于单元目录，但导入阶段读取了真实枚举资源，按项目测试分类涉及集成边界，不能作为纯单元验证；这是用户要求不运行集成测试后的执行偏差。连接替身覆盖了活动表身份字段、配对约束、缺失结构，以及格式 1 和格式 6 打开时拒绝不符结构；未验证真实数据库文件或既有现场库。

随后将新增活动结构和打开入口用例迁移到隔离测试，恢复原输出范围测试的资源隔离；以内存枚举、权威 SQL 和连接替身验证同一分区。在 `/workspaces/camctl` 使用 CPython 3.11.17 执行 `PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/persistence/test_device_activity_structure_isolated.py apps/camctl/tests/unit/persistence/test_output_scope_structure.py -q`，退出码为 0，11 项通过。此结果不证明真实数据库文件的初始化或打开行为。

可用性检查结构原型在独立工作区 `/tmp/camctl-h2-availability-guard-20261028` 尚未合入。该原型增加了意图和结果票据共用的目标身份规则；工作区没有生成包资源。先执行 `test_attempt_inputs.py` 时发现它在导入时尝试读取缺失的真实枚举资源，收集阶段失败，退出码为 2，没有运行用例。随后以持久化模型替身隔离协作者并禁止读取包资源，使用 CPython 3.11.17 运行 `PYTHONPATH=apps/camctl/src /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/operations/test_ticket_target_identity_unit.py -q`，退出码为 0，6 项通过。原型的检查事务和独立守卫仍未完成，此结果不证明持久化或恢复链。

原型还将已保存的可用性成功结果交给独立事实校验，核对检查归属、固定机身、实际期限及三步工具观察。测试最初因驱动身份和响应解析模块的导入链尝试读取缺失的枚举资源而两次在收集阶段失败，退出码均为 2，没有运行用例。改为分别隔离这些协作者，并将检查输入的新增分区从资源依赖测试迁入隔离测试后，使用 CPython 3.11.17 运行 `PYTHONPATH=apps/camctl/src /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/operations/test_saved_availability_unit.py apps/camctl/tests/unit/operations/test_ticket_target_identity_unit.py -q`，最终退出码为 0，25 项通过。测试替身只覆盖事先给定的工具响应；事务守卫与真实解析器的组合没有验证。

## H3：启动与在途读取

在 `/workspaces/camctl` 使用 CPython 3.11.17 执行以下命令，退出码为 0，204 项通过。测试虽然位于单元目录，但导入阶段读取了真实枚举资源，按项目测试分类涉及集成边界，不能作为纯单元验证；这是用户要求不运行集成测试后的执行偏差。此次执行的连接替身检查了同逻辑设备的在途读取与启动查询、双方意图提案等待、独立守卫拒绝冲突及 HOST 读取不受影响。新结构为运行中尝试增加部分索引；该范围没有验证实际查询计划、真实数据库、物理机身别名、检查尝试、读取重试让位或完整业务链。

```bash
PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/scheduling/test_start_read_exclusion.py apps/camctl/tests/unit/scheduling/test_preparation_storage.py apps/camctl/tests/unit/operations/test_preparation_guards.py apps/camctl/tests/unit/operations/test_attempt_inputs.py -q
```

随后将新增测试改为在导入及运行时禁止读取真实枚举资源，并以受控枚举和连接替身验证两项在途查询。在同一环境运行 `PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/scheduling/test_start_read_exclusion.py -q`，退出码为 0，4 项通过。此测试只覆盖局部查询，不证明事务提案及守卫的组合行为。

读取机会事务和独立守卫补充了同逻辑设备的在途 START 核对。在 `/workspaces/camctl` 使用 Python 3.11.17 执行 `PYTHONPATH=apps/camctl/src PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/persistence/test_read_slot_start_isolation.py -q --tb=short -p no:cacheprovider`，退出码为 0，6 项通过。测试以受控枚举、事件、数据库及其他协作者替身覆盖授予等待、已有机会、释放与守卫；不证明真实数据库事务或同事务前序事件投影。

资源守卫接入普通事务的已验证前序投影后，使用同一环境运行 `PYTHONPATH=apps/camctl/src PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/scheduling/test_preparation_flight_overlay.py apps/camctl/tests/unit/scheduling/test_start_read_exclusion.py apps/camctl/tests/unit/operations/test_intent_run_identity_isolated.py apps/camctl/tests/unit/persistence/test_read_slot_start_isolation.py -q --tb=short -p no:cacheprovider`，退出码为 0，31 项通过。首次组合运行因测试模块缓存共享使 2 项失败、29 项通过；隔离模块引用后重跑通过。测试核对前序创建、结束、关联变化、意图行基数和机会守卫；不证明真实数据库或完整设备调度。

在独立工作区 `/tmp/camctl-h3-availability-coordinator-20261028` 的提交 `ae2bab45`，负责人报告定向隔离单元测试为 247 项及 12 个子测试通过，`git diff --check c3bb1b8f HEAD` 通过。该分支接入检查协调、拍摄与 DEVICE 读取、检查事务和取消恢复，但尚未合入当前分支。测试使用受控枚举和协作者替身，未运行集成测试，未读取真实包资源或访问真实数据库及设备；上述结果不能证明组件组合、历史回放、跨会话恢复或真机行为。

内部 READ 耗尽接线在独立工作区 `/tmp/camctl-unknown-read-exhaustion-20261028` 误运行了一次不符合本轮限制的测试：`UV_PROJECT_ENVIRONMENT=/tmp/camctl-unknown-read-exhaustion-20261028/apps/camctl/.venv311 uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/capture/test_unknown_read_exhaustion.py -q`。该命令退出码为 0，输出 3 项通过；但测试直接导入真实 `camctl` 模块并调用真实枚举登记，按项目分类跨越集成边界，违反本轮禁止运行集成测试的要求。它没有访问真实数据库或设备，结果不作为本轮合规的单元验证证据。负责人收到提醒后停止运行该测试并改用隔离替身。

该分支仍需处理完整 READ 已结束后的机会交接、尚未建立活动的拍摄与 READ 的选中竞争、合法 UNKNOWN 耗尽后的有限终结，以及取消与绑定失败的先后语义。顾问已分析前两项读取结束和 UNKNOWN 耗尽的规格边界，相关选择尚待用户答复；这些未完成事项不得以现有局部测试通过替代。

## H4：公共诊断消费者

在 `/tmp/camctl-h4-diagnostics` 的提交 `0316cb2c` 上，使用 CPython 3.11.17 执行日志单元目录和组件集成目录，分别为 366 项和 296 项通过；补充测试后，受影响单元文件 51 项、集成文件 8 项通过。原始输出分别位于 `/tmp/camctl-h4-diagnostics-unit.log`、`/tmp/camctl-h4-diagnostics-integration.log`、`/tmp/camctl-h4-diagnostics-unit-final.log` 和 `/tmp/camctl-h4-diagnostics-integration-final.log`；集成提交为 `cd7282fb`。测试覆盖公共消费者、原双流、重复交付、满队列和非检查媒体输出隐藏。检查的实际结果拥有者尚未接入该入口，真实设备收场和 HA-11 联合验收尚未完成。

四次命令使用 `/workspaces/camctl/apps/camctl/.venv311/bin/python`，退出码均为 0：

```bash
PYTHONPATH=apps/camctl/src /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/logging_runtime -q
PYTHONPATH=apps/camctl/src /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/logging_runtime -q
PYTHONPATH=apps/camctl/src /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/logging_runtime/test_diagnostics.py -q
PYTHONPATH=apps/camctl/src /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/logging_runtime/test_diagnostics.py -q
```

目录测试后只增加了不完整或不可用流的测试分区并调整测试夹具，生产代码未再修改；集成目录有一条 pytest 标记警告。
