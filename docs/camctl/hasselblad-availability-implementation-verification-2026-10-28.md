# Hasselblad 可用性恢复实施验证

本记录保存[实施计划](../superpowers/plans/2026-10-28-hasselblad-availability-recovery.md)的阶段性软件验证。软件替身和测试环境中的结果不表示真实相机或目标主机已经验收。

## H1：录像启动占用释放测试装配

2026-10-28，在基线 `de455595` 的独立工作区 `/tmp/camctl-h1-start-recovery-20261028` 使用 CPython 3.11.17 执行：

```bash
PYTHONDONTWRITEBYTECODE=1 /workspaces/camctl/apps/camctl/.venv311/bin/python -m unittest discover -s apps/camctl/tests/unit/capture -p 'test_h1_recovery_boundaries_isolated.py' -v
```

命令退出码为 0，11 项通过，前台输出未另存。测试加载目标源码前阻断真实 `camctl` 导入，其他协作者和报告登记均使用内存替身；未访问真实包资源、数据库、设备或现场数据。

`capture.recovery.recovery_block_reason` 覆盖旧本地收场未确认、固定历史范围缺失或不包含原意图、证据查找缺失、本地控制仍在途、原驱动证据缺失和合格边界；生产 `_attempt_recovery_evidence` 按原顺序消费该判定。`recovery_registry` 覆盖原驱动声明及其恢复证据契约，通用 `operations.recovery.recover_attempt` 的分类另作独立单元验证，该通用函数不直接驱动拍摄恢复。原结果拥有者的 `save` 入口由生产保存路径调用；测试覆盖派生申请保留原票据、操作键和同一保存任务，两个等待者共同消费结果且只应用一次，回执未知后显式按已冻结的最新原申请重送；原结果已应用并移除时，旧快照不能重开保存。活动释放判定覆盖可靠无效果、准备未收场、可能启动以及输出范围未确定；报告投影解释器在登记替身下保持原错误与 `end_unconfirmed`，不把占用变化解释为设备结束。

静态核对显示，`session_capture_assembly` 固定本会话恢复边界与历史上界；`recover_attempt` 优先处理同会话已持有结果，已保存结果不重新生成，原调用结果缺失且上述前提成立时形成原票据的 `UNKNOWN/result_not_saved`，没有重新派发旧 START。调用结果及派生申请沿 `PendingCallResult` 的原键提交；文件及报告事实仍由原生产责任处理。上述隔离测试不构成真实报告登记、完整会话组装、数据库事务或跨组件组合证明；安装版、跨组件和真机验收仍按计划单独跟踪。

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

2026-10-28，在独立工作区 `/tmp/camctl-h2-stage-audit-20261028` 的基线 `a0174202`，使用 CPython 3.11.17 定向核对 H2。以下命令退出码均为 0，前台输出未另存：

```bash
PYTHONDONTWRITEBYTECODE=1 /workspaces/camctl/apps/camctl/.venv311/bin/python -m unittest discover -s apps/camctl/tests/unit/devices -p 'test_h2_availability_port_isolated.py' -v
PYTHONDONTWRITEBYTECODE=1 /workspaces/camctl/apps/camctl/.venv311/bin/python -m unittest discover -s apps/camctl/tests/unit/history -p 'test_h2_availability_history_isolated.py' -v
PYTHONDONTWRITEBYTECODE=1 /workspaces/camctl/apps/camctl/.venv311/bin/python -m unittest discover -s apps/camctl/tests/unit/operations -p 'test_h2_target_guard_isolated.py' -v
PYTHONPATH=apps/camctl/src PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /workspaces/camctl/apps/camctl/.venv311/bin/python -c 'import camctl.resources as r; r.resource_bytes = lambda *a, **k: (_ for _ in ()).throw(AssertionError("real resource blocked")); r.available_resources = r.resource_bytes; import camctl.contracts.enums as e; e.resource_bytes = r.resource_bytes; import pytest; raise SystemExit(pytest.main(["apps/camctl/tests/unit/persistence/test_device_activity_structure_isolated.py", "-q", "--tb=short", "-p", "no:cacheprovider"]))'
```

四条命令分别通过 5、2、2、6 项。前三组在导入前阻断真实 `camctl` 包并以源码抽取及内存协作者执行；最后一组在测试导入前阻断真实资源读取，连接与结构检查使用替身。活动结构测试恢复对旧活动定义的原断言，新增的读取结束结构和重试索引检查只作为非被测协作者隔离。

| H2 项目 | 实现核对与隔离证据 |
| --- | --- |
| 1. 检查结构与登记 | 当前 SQL 登记检查、固定目标、唯一关系、原流程及释放引用；旧活动结构拒绝分区由 6 项结构测试覆盖。真实初始化及 SQL 约束执行未在本次运行。 |
| 2. 历史与归属 | 历史仓储包含检查和目标的动作归属；2 项隔离测试对触发动作与旧动作分别检查正向、快照起点及逆向行值，拒绝不一致的释放引用。未执行真实数据库历史回放。 |
| 3. 释放合法性 | 原活动资格与目标页守卫已实现；隔离测试覆盖活动仍在执行、公共范围、已有引用、缺失身份、跨逻辑设备、机身不一致及伪造或不完整目标页。检查结果与真实事务的联合守卫另行验证。 |
| 4. 创建、意图与结果事务 | 创建、意图、真实结果、目标释放、无调用终结和失败交付已有生产命令；目标页测试验证完整固定集合，其他提交、原键和全批原子性按代码及既有专项记录核对，本次未执行真实事务。 |
| 5. 固定身份与别名 | 活动和 DEVICE 源保存原身份，`physical_alias` 核对未完成责任；此次目标和驱动入口测试分别覆盖缺失、错误机身与绑定不符，不以当前配置补造旧身份。 |
| 6. 检查驱动 | 5 项隔离测试执行公开检查方法，覆盖 `list → info → doctor`、共享期限、绑定不符、失败和取消，并检查结果转换保留原步骤输出；解析器及受管工具使用替身，未验证真实进程。 |
| 7. DEVICE 正式源 | `CameraFileIdentity` 固定原绑定、路径、种类、长度和摘要；`open_read` 核对来源与固定机身、长度及偏移。此项本次为静态入口核对，文件来源登记与实际读取组合另行验证。 |
| 8. 错误与报告 | 公共登记包含拍摄检查失败、来源设备不可用及物理绑定原因，失败事务消费登记的错误与原检查次数；此项本次为静态核对，真实报告登记与交付组合未运行。 |

这些结果不证明真实资源、数据库事务、受管进程、安装版或设备行为。公开检查和事务的跨组件验证、历史持久化回放及真实设备验收分别按计划跟踪。

将 H2 交付 `cb02382f` 合入包含 H1 的 `d30fda8f` 后，在主工作区 `/workspaces/camctl` 用同一 Python 3.11.17 及 `PYTHONDONTWRITEBYTECODE=1`，运行上述三个 unittest 文件和 H1 的 `test_h1_recovery_boundaries_isolated.py`，将 `-v` 改为 `-q`；四条命令退出码均为 0，分别为 5、2、2、11 项通过。此轮用于核对合并后的实际源码，未扩大到真实资源、数据库或设备。Sol 对固定 H2 源码的八项责任审查未发现生产阻断，所指出的结构测试前置协作者隔离已在交付中修正。

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

完整 READ 结束后的机会交接、拍摄与 READ 的共同选择、UNKNOWN 耗尽及取消与绑定失败的先后处理已在后续提交中合入；当前入口及验证范围如下。

### H3 当前入口与隔离验证

2026-10-28，在独立工作区 `/tmp/camctl-read-end-persist-20261028` 的源码 `ffa3157e` 核对 H3。STOP 分支合并提交为 `88c5effc`；该合并保留 H1 的原保存拥有者。以下命令使用 Python 3.11，退出码均为 0，前台输出未另存：

```bash
env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /workspaces/camctl/apps/camctl/.venv311/bin/python -B -m pytest --noconftest -p no:cacheprovider -q apps/camctl/tests/unit/capture/test_normal_recording_stop_isolated.py apps/camctl/tests/unit/capture/test_segment_completion_recovery_isolated.py
python3.11 -B -m unittest discover -s apps/camctl/tests/unit/capture -p test_h1_recovery_boundaries_isolated.py -q
python3.11 -B apps/camctl/tests/unit/capture/test_saved_internal_binding_recovery_isolated.py
python3.11 -B apps/camctl/tests/unit/capture/test_internal_inspection_scope_isolated.py
python3.11 -B apps/camctl/tests/unit/capture/test_canceled_processing_read_isolated.py
```

上述命令分别通过 62、11、5、1、7 项。测试从源码抽取目标行为并替换协作者，不导入实际包资源或连接数据库。最后两项分别核对原 READ 与检查保存的共享责任、原键和 UNKNOWN 重送，以及取消后的文件拥有者、事务提案、独立守卫和终态分页发现。原 READY 或 FAILED 等状态均由受控替身提供，不能据此推断真实数据库组合。

在合并 H1、H2 的 `ebf3b336` 后，以下三条命令在导入受测模块之前将 `camctl.resources` 替换为禁止实际读取的替身，并禁用自动插件加载；各通过 7 项。测试数据库、驱动和其余协作者均使用替身：

```bash
env PYTHONPATH=apps/camctl/src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /workspaces/camctl/apps/camctl/.venv311/bin/python -B -c 'import sys, types; r=types.ModuleType("camctl.resources"); r.resource_bytes=lambda *a, **k: (_ for _ in ()).throw(AssertionError("禁止真实包资源")); r.available_resources=r.resource_bytes; r.ResourceError=RuntimeError; r.ResourceName=str; sys.modules["camctl.resources"]=r; import pytest; raise SystemExit(pytest.main(["--noconftest", "-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin", "-q", "apps/camctl/tests/unit/operations/test_availability_assembly_isolated.py"]))'
env PYTHONPATH=apps/camctl/src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /workspaces/camctl/apps/camctl/.venv311/bin/python -B -c 'import sys, types; r=types.ModuleType("camctl.resources"); r.resource_bytes=lambda *a, **k: (_ for _ in ()).throw(AssertionError("禁止真实包资源")); r.available_resources=r.resource_bytes; r.ResourceError=RuntimeError; r.ResourceName=str; sys.modules["camctl.resources"]=r; import pytest; raise SystemExit(pytest.main(["--noconftest", "-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin", "-q", "apps/camctl/tests/unit/operations/test_availability_coordinator_isolated.py"]))'
env PYTHONPATH=apps/camctl/src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /workspaces/camctl/apps/camctl/.venv311/bin/python -B -c 'import sys, types; r=types.ModuleType("camctl.resources"); r.resource_bytes=lambda *a, **k: (_ for _ in ()).throw(AssertionError("禁止真实包资源")); r.available_resources=r.resource_bytes; r.ResourceError=RuntimeError; r.ResourceName=str; sys.modules["camctl.resources"]=r; import pytest; raise SystemExit(pytest.main(["--noconftest", "-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin", "-q", "apps/camctl/tests/unit/outputs/test_availability_dispatch_isolated.py"]))'
```

在相同隔离环境补充成功结果的两个参数分区，退出码为 0，2 项通过。测试实际执行检查协调器的派发复核和成功结果保存回调，分别验证拍摄与 DEVICE 读取都把原票据交给完整释放事务；驱动和事务均为替身。

```bash
env PYTHONPATH=apps/camctl/src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /workspaces/camctl/apps/camctl/.venv311/bin/python -B -c 'import sys, types; r=types.ModuleType("camctl.resources"); r.resource_bytes=lambda *a, **k: (_ for _ in ()).throw(AssertionError("禁止真实包资源")); r.available_resources=r.resource_bytes; r.ResourceError=RuntimeError; r.ResourceName=str; sys.modules["camctl.resources"]=r; import pytest; raise SystemExit(pytest.main(["--noconftest", "-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin", "-q", "apps/camctl/tests/unit/operations/test_availability_coordinator_isolated.py::test_successful_check_delivers_original_result_to_release_transaction"]))'
```

| H3 责任 | 当前生产入口与验证范围 |
| --- | --- |
| 1. 共同检查与预算 | `AvailabilityCoordinator` 使用目标设备 query 配置、独立检查流程和共享原调用；协调单元覆盖共同等待、原键重送、耗尽及恢复。 |
| 2. 正常拍摄 | `CaptureFlow` 在原准备流程前调用 `AvailabilityAssembly.capture`；装配单元覆盖未建活动的检查申请。 |
| 3. DEVICE 读取 | `advance_obtain` 按候选调用同一 `AvailabilityAssembly.read`，再进入原机会与实际派发；装配和派发守卫单元覆盖原来源、持槽与在途限制。 |
| 4. 资源和派发资格 | 准备、检查、READ 意图和实际派发分别核对资源事实；上述派发测试和 H2 目标守卫使用连接替身。 |
| 5. 让位、等待与恢复 | 已结束 READ 保存当前轮原 `ReadEnd`；等待计时、原预算和文件顺序由既有重试与共同选择代码持有。相关阶段提交及隔离测试分别核对等待清除、UNKNOWN 耗尽和原结束恢复。 |
| 6. 唤醒和关闭 | 协调器持有检查调用、待保存结果和重试截止；会话装配恢复终态后的原检查，并接入关闭收场。协调单元覆盖等待与原结果拥有者，不替代真实会话组合。 |
| 7. 取消和竞争 | 检查及 READ 保留原实际结果和保存键；分段检查与普通录像分别恢复原所属责任。取消单元覆盖原结果、检查 UNKNOWN、文件拥有者、slot 与原键。 |
| 8. 错误和诊断 | 检查耗尽分别交给拍摄或读取失败事务；原绑定、调用结果和诊断保持独立。公共诊断消费者与报告组合仍按 H4 和联合验收单独核对。 |
| 9. HOST 和 DEVICE | HOST 本地拷贝不申请相机机会；DEVICE 读取按原固定绑定和驱动证据恢复。HOST、DEVICE 的真实归档、下载及报告组合未在本轮执行。 |

Sol 分别复核了 STOP 原事实、检查 UNKNOWN 交接和普通录像取消后处理，未发现各自范围内需要继续修复的问题。此记录不证明真实初始化、状态库事务、工具进程、跨组件报告、安装版或相机验收；本轮没有新增或运行集成测试，也没有访问真实数据库、设备或现场环境。

## H4：公共诊断消费者

2026-10-28，在独立工作区 `/tmp/camctl-h4-diagnostics-history-20261028`、基线 `6d483acd`，使用 CPython 3.11.17 执行以下定向验证。四条命令最终退出码均为 0，分别通过 5、2、6、9 项；前台输出未另存。

```bash
PYTHONDONTWRITEBYTECODE=1 /workspaces/camctl/apps/camctl/.venv311/bin/python -m unittest discover -s apps/camctl/tests/unit/operations -p 'test_h4_diagnostic_consumer_isolated.py' -v
PYTHONDONTWRITEBYTECODE=1 /workspaces/camctl/apps/camctl/.venv311/bin/python -m unittest discover -s apps/camctl/tests/unit/history -p 'test_h4_availability_ownership_isolated.py' -v
PYTHONDONTWRITEBYTECODE=1 /workspaces/camctl/apps/camctl/.venv311/bin/python -m unittest discover -s apps/camctl/tests/unit/devices -p 'test_h2_availability_port_isolated.py' -v
PYTHONPATH=apps/camctl/src PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /workspaces/camctl/apps/camctl/.venv311/bin/python -c 'import camctl.resources as r; r.resource_bytes = lambda *a, **k: (_ for _ in ()).throw(AssertionError("real resource blocked")); r.available_resources = r.resource_bytes; import camctl.contracts.enums as e; e.resource_bytes = r.resource_bytes; import pytest; raise SystemExit(pytest.main(["apps/camctl/tests/unit/operations/test_availability_coordinator_isolated.py", "-q", "--tb=short", "-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider"]))'
```

前三组只抽取目标源码或阻断真实包导入，使用受控登记、日志通道、数据库与设备协作者；第四组在测试收集前阻断资源读取。协调器测试首次执行有两项装配失败，原因是测试仓储替身缺少真实接口的 `finish_attempt`；补齐后另有两项因结果替身缺少正式 `error` 字段失败。补齐替身并保留原断言后，最终 9 项通过。

| H4 责任 | 生产入口与单元证据 |
| --- | --- |
| 创建、意图、派发与结果交接 | `AvailabilityCoordinator`、`AvailabilityAttemptOwner` 和独立事务守卫沿原检查、票据和键推进；H3 已记录协调器的原键重送、共同等待和成功交付测试，本轮对受影响协调器重新验证。 |
| HA-05 交错及取消/窗口 | 创建和意图守卫核对选中拍摄、原读取资格及在途持有者；目标页和释放守卫处理已解除、部分不合格及新限制。H2/H3 已记录目标、资源、读取等待和取消单元，真实调度交错留待独立组合验收。 |
| 分页、预算与历史 | 目标和结果事务沿固定页与原责任推进，等待终结按取消、窗口和当前预算分类；本轮直接核对检查和目标归触发动作、释放归原动作，补充 H2 的正逆行值恢复测试。 |
| 原诊断与关闭 | 已收场错误在动作分类读库之前交给原诊断状态；实际收场未确认时只交付原异常、步骤与双流，保留未保存责任，不构造结束结果。隔离单元覆盖读库失败、原申请重送、诊断去重、部分字节、通道失败降级及取消后原结果继续持有。 |
| DEVICE 原责任 | 检查、来源身份、读取预算和原票据沿 H2/H3 的现有实现；本轮受影响驱动单元补充未收场传输时保留原步骤字节并拒绝生成已收场结果。 |

Sol 对当前 H4 生产差异作只读复核，未发现新的必须修复问题。本轮没有运行集成测试、真实日志线程或进程、真实资源、数据库、设备或现场数据；跨组件历史回放、真实报告和安装版验证仍按各自验收项跟踪。

在 `/tmp/camctl-h4-diagnostics` 的提交 `0316cb2c` 上，使用 CPython 3.11.17 执行日志单元目录和组件集成目录，分别为 366 项和 296 项通过；补充测试后，受影响单元文件 51 项、集成文件 8 项通过。原始输出分别位于 `/tmp/camctl-h4-diagnostics-unit.log`、`/tmp/camctl-h4-diagnostics-integration.log`、`/tmp/camctl-h4-diagnostics-unit-final.log` 和 `/tmp/camctl-h4-diagnostics-integration-final.log`；集成提交为 `cd7282fb`。测试覆盖公共消费者、原双流、重复交付、满队列和非检查媒体输出隐藏。检查的实际结果拥有者尚未接入该入口，真实设备收场和 HA-11 联合验收尚未完成。

四次命令使用 `/workspaces/camctl/apps/camctl/.venv311/bin/python`，退出码均为 0：

```bash
PYTHONPATH=apps/camctl/src /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/logging_runtime -q
PYTHONPATH=apps/camctl/src /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/logging_runtime -q
PYTHONPATH=apps/camctl/src /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/logging_runtime/test_diagnostics.py -q
PYTHONPATH=apps/camctl/src /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/logging_runtime/test_diagnostics.py -q
```

目录测试后只增加了不完整或不可用流的测试分区并调整测试夹具，生产代码未再修改；集成目录有一条 pytest 标记警告。
