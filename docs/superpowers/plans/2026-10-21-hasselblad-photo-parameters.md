# Hasselblad 单张拍摄参数与中文选项执行计划

> **执行者要求：** 阅读本计划及其规格后，按用户选择使用 `superpowers:executing-plans` 或 `superpowers:subagent-driven-development` 逐项实施。用 `- [ ]` 跟踪实际结果。本文件按任务记录实施结果与待完成工作；软件、部署和真机证据分别核对。

**目标：** 让用户通过同源能力说明选择 Hasselblad 的图片格式、曝光、白平衡和对焦，并使这些明确参数经过客户端、受理、驱动、文件登记、恢复和交付后仍保持一致。

**架构：** 驱动集中维护参数及命令语义，客户端扩展通用 Schema 控件。沿用一次 START、已保存执行定义和双文件原子登记，扩展 RAW／JPEG 的角色、路径及机内身份表达；取消判断明确区分整次 START 入口与照片采集效果。

**技术栈：** Python 3.11、Decimal、JSON Schema Draft 2020-12、SQLite、TypeScript／React、现有精确 JSON 数字支持、C host（主程序接入模块）及 wheel 安装包。

**规格：** [Hasselblad 单张拍摄参数与中文选项设计](../specs/2026-10-20-hasselblad-photo-parameters-design.md)。实现行为以该规格及其引用的公共契约为准。

## 计划的使用方式

规格中的输入、状态、失败及验收要求是硬性要求。本文的任务划分、预计文件、内部类型和函数签名是实施建议；执行者可以根据实际结构调整，但须同步相邻任务及测试，并保持行为契约。建议新增文件和测试在本计划建立时尚不存在。

每项任务都先增加能够暴露缺口的测试，确认其因目标行为缺失而失败，再修改生产代码并验证通过。真实文件、数据库、进程和浏览器协作使用集成测试。每项任务完成后审查差异及验证证据，再按[Git 协作规则](../../../.agents/rules/implementation.md#git-协作与工作区隔离)形成独立阶段提交；本文中的提交名称仅为建议。

各任务同步更新自身验证范围依赖的旧参数夹具和响应夹具，使它们符合完整参数及当前工具契约。H7 再审计全部消费者，避免前面任务必须等待最后一轮夹具更新才能验证。

下文 camctl 源码路径相对于 `apps/camctl/src/camctl/`，客户端源码路径相对于 `apps/client/`，其余路径从仓库根开始。实施前按根 `AGENTS.md` 读取相关规则、目录说明及测试指南，核对实际工作目录、分支、已有改动和解释器。

## 全局约束

- 驱动为 `hasselblad_cfv100c`，动作为 `camera_take_photo`，参数类型为 `hasselblad_photo`，能力名称为“Hasselblad 单张拍照”，`preview_supported=true`。
- 完整参数的五个顶层字段全部必填；互斥分支封闭，MF 的 `relative_move` 可省略且仅允许 `-5`、`5`。域、标题和命令映射由驱动同源生成，完整档位表只在其权威定义维护。
- 参数、Schema、预设、计划、历史及工具参数使用精确数字。快门目录生成时的舍入只应用一次；执行时直接使用已保存值。
- 设置、对焦和拍摄共用一次 START；完整调用预算为首次受理保存的 `180` 秒，正式源位置也使用原执行定义。每次派发仍检查窗口、剩余预算和普通资格。
- 实际进入整次 START 后拒绝取消。照片采集 `NO_EFFECT` 与整次 START 尚未进入分别判断；步骤副作用随原尝试保存，恢复消费原事实。
- 原图和预览各有独立文件身份。JPEG 原图与 JPEG 预览通过角色区分，MIME 用于内容校验；机内路径、机内文件名及主机文件名分别表达。
- 完成事务、未知回执核实、历史回放及报告继续使用原动作、原活动、原完整申请和原操作键。相对移动及已启动或可能启动的 START 至多执行一次。
- 客户端显示现有通用失败摘要。具体步骤属于内部尝试和历史，公共计划、报告及错误继续消费既有通用契约。
- 软件测试、安装包、ARM 部署、真实拍摄、文件字节、客户端媒体导入及 ACK（累计报告确认）分别记录证据。

## 优先审查的五类场景

| 场景 | 用户应得到的结果 | 负责的测试任务 |
| --- | --- | --- |
| 短快门穿过带标题选项、预设、重开及导出 | `0.000667`、`0.000333`、`0.00025` 保持各自精确值；中文标签只影响显示 | H1、H6、H8 |
| 原图及预览同为 JPEG | 两份独立文件分别登记、取回并保持预览关联，归档路径与实际内容一致 | H2、H3、H8 |
| 调焦已经执行，后续拍摄因预算阻止，结果为 `NO_EFFECT` | 内部保存调焦事实及实际失败；取消资格仍以 START 入口判断，恢复沿原结果收场 | H4、H5 |
| 工具明确缺少机内名称，或响应含重复、畸形及相互冲突的名称 | 合法的明确缺名只允许主机源继续；异常响应不形成可靠成功结果 | H2、H3 |
| 完成事务回执丢失，之后媒体只到了一部分 | 原申请核实后只登记一次；各次交付有独立身份，媒体导入和报告 ACK 分别成立 | H3、H5、H8 |

## 实施起点与实施顺序

在实施基线 `61459de` 中，`devices/drivers/hasselblad/definitions.py` 只声明 `type`，`capability()` 还用不完整的示例参数调用任务工厂。`driver.py::control()` 固定设置 RAW，尚未执行曝光、白平衡及对焦参数。因此，先完成权威参数与内部输入表示，再接入真实命令。

文件路径也有独立缺口：`host_files/paths.py` 将原图扩展名固定为 `.3fr`，`capture_sources.py` 按 MIME 推断角色，`capture/host_photo.py` 只接受 3FR 原图，并把主机文件名用于设备源身份。仅增加 JPEG 能力声明会使这些消费者互相矛盾。

此外，`persistence/repositories/capture_facts.py::load_start_facts()` 会使用采集 `NO_EFFECT` 推导未启动，取消资格又消费该结果。加入设置及调焦后，照片尚未采集也可能已经改变相机，必须沿 START 调用入口审计取消、失败和恢复的共同接缝。

| 任务 | 交付内容 | 前置 |
| --- | --- | --- |
| H1 | 权威参数目录、完整校验及离线能力说明 | 无 |
| H2 | 格式明确的响应解析及独立机内定位 | H1 |
| H3 | 格式对应的文件责任、双角色登记及恢复 | H1、H2 |
| H4 | 有限命令序列、共享预算及实际步骤结果 | H1—H3 |
| H5 | START 入口取消规则、失败收场及恢复闭合 | H4 |
| H6 | 通用中文选项、对象分支及精确数字编辑 | H1；可与 H2—H5 并行 |
| H7 | 完整样例、生成器消费者及安装资源 | H1—H6 |
| H8 | 安装版跨组件联合验收及软件门禁 | H7 |
| H9 | ARM 部署及真实相机分层验收 | H8 及可用的目标设备 |

H2—H5 共同修改驱动、拍摄和历史边界，建议顺序实施。H6 的写入范围主要在客户端，可按工作区隔离规则独立推进；H8 集成前统一实际内部接口和样例输入。

## 建议的内部接口与交接内容

| 建议位置或既有入口 | 建议输入与输出 | 必须保持的含义 |
| --- | --- | --- |
| 新增 `devices/drivers/hasselblad/parameters.py` | `parse_photo_settings(params: Mapping[str, JsonValue]) -> PhotoSettings`；`photo_schema() -> dict` | `PhotoSettings` 包含格式及三个已验证分支，数值保持 Decimal／精确整数；Schema 与命令规划消费同一目录 |
| `responses.py`、`files.py` | `parse_capture(output: bytes, *, output_directory: str, capture_format: CaptureFormat) -> CaptureResponse` | 命名的 `original`、`preview` 保留主机信息；`camera_locator` 明确区分合法路径与工具明确缺名。异常响应抛出解析错误 |
| `files.py` | `KnownCameraPath(full_path: str, basename: str)`；`ReportedMissingCameraName` | 路径与 basename 一致，设备源固定完整机内定位和原绑定；下载只传 basename |
| 新增 `devices/drivers/hasselblad/commands.py` | `photo_setup_commands(settings: PhotoSettings) -> tuple[PhotoCommand, ...]` | `PhotoCommand` 至少表达步骤类别、结构化 argv 和副作用类别，消费 H1 的映射，不另建参数允许域 |
| `capture_facts.py` 及取消共同入口 | 建议 `load_photo_invocation_facts(...) -> PhotoInvocationFacts` | 分别表达可靠未进入、已经进入和未知；意图、ticket、采集 `NO_EFFECT` 都不能单独证明入口事实 |
| 建议新增客户端 `src/web/scalar-options.ts` | `scalarChoices(schema: JsonValue, root: JsonValue) -> ScalarChoice[] \| undefined` | 每项保留机器值、标签及精确数字的来源上下文；`undefined` 表达不适合选择器，不能伪装成空允许域 |

`CaptureResponse` 的名称缺失事实应进入原调用的内部结果，使 H3 可以按原保存的源模式判断。工具进程失败及输出不完整先由 H4 处理，H2 只在完整成功输出上解释语法；若明确缺名提示出现在 stderr，H2 与 H4 的实际签名须一致传递该完整事实。

格式对应的扩展名和媒体类型也只维护一份映射。建议在 H1 定义格式描述并通过现有任务／文件责任入口传递；H3 核对模块依赖后确定其归属，避免在通用路径模块按相机标识分支。`PrepareHostPhoto` 与完成申请可从原 action 取得格式，无须再各存一份参数副本。

## 验证命令约定

以下命令供实施时使用。先按[camctl 测试指南](../../../apps/camctl/tests/AGENTS.md)准备 Python 3.11 环境，并在仓库根的同一个 Bash 会话定义简写：

```bash
camctl_pytest() {
  UV_PROJECT_ENVIRONMENT="$PWD/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest "$@"
}
```

组件集成测试按目录分别启动，同一工作区一次只运行一个集成 pytest 进程。每项任务保留失败、修复后通过的命令、日志及退出码；已通过的范围只在后续改动或未解决问题需要时重跑。客户端命令使用已有依赖环境，运行说明见[客户端入口](../../../apps/client/README.md)。

## H1：建立完整参数定义及离线能力说明

**做什么和原因：** 先让驱动、受理及客户端取得同一套参数规则。任务工厂只处理完整合法参数；能力说明使用静态元数据，避免为了导出说明而补造拍摄配置或连接相机。

**预计文件：** 新增建议的 `parameters.py`；修改 `devices/drivers/hasselblad/definitions.py`。扩展 `apps/camctl/tests/unit/devices/test_hasselblad_definitions.py`，建议新增同目录 `test_hasselblad_parameters.py`，并更新 `apps/camctl/tests/integration/acceptance/test_hasselblad_definitions.py`。

**输入与交付：** 消费规格中的完整结构及档位表，交付 `PhotoSettings`、`CaptureFormat`、同源 Schema、格式描述和命令所需元数据。沿用原执行定义保存 `output_storage` 与 `start_call_timeout_s`。

- [x] **步骤 1：增加允许域及分支测试。** 建议用 `test_photo_schema_has_closed_independent_branches` 验证五字段、四种曝光、八种白平衡及两种对焦；以独立的合法／非法样例断言缺项、`null`、错误类型、额外 `tint`、不适用字段均失败。`test_photo_domains_are_exact_and_positive` 核对 10 档 ISO、13 档光圈、34 档快门的目录数量、独立代表值、全部候选可通过及相邻域外值失败；不在测试另抄完整生产表。
- [x] **步骤 2：增加数值及缺省边界测试。** `test_short_shutters_keep_distinct_decimal_values` 精确核对三个短快门，并以 `1/6→0.167`、`1/180→0.00556` 及有限小数代表值独立验证目录生成的精度；域外邻近值不能靠近似匹配通过。`test_manual_white_balance_integer_bounds` 覆盖 2000、10000、区间整数及 1999、10001、小数、缺项；`test_manual_focus_absence_differs_from_zero` 覆盖省略、`-5`、`5`、`0` 和 AF-S 错带字段。
- [x] **步骤 3：确认当前测试失败。** 执行 `camctl_pytest apps/camctl/tests/unit/devices/test_hasselblad_definitions.py apps/camctl/tests/unit/devices/test_hasselblad_parameters.py -q`，确认失败指向缺少完整参数契约。
- [x] **步骤 4：实现同源定义及真实任务校验。** 用一份字段目录生成各曝光分支的共享节点、标题和说明；图片格式及数值档位使用规格要求的带标题常量表示。将同一校验用于 `parse_photo_settings` 与真实任务工厂；`capability()` 从固定任务事实生成说明，离线导出完整能力。手动调焦说明标明临时选项及省略语义。
- [x] **步骤 5：验证受理和保存。** 扩展 `test_photo_acceptance_saves_explicit_params_and_call_budget`：完整参数按原始／生效输入保存，源模式和 180 秒进入执行定义；修改当前配置不改变原动作。`describe` 在无设备及无可执行工具的测试环境仍成功。运行上述单元及 `camctl_pytest apps/camctl/tests/integration/acceptance -q`。
- [x] **步骤 6：审查并形成阶段提交。** 建议提交名为“定义 Hasselblad 完整单张参数与同源能力”；交接实际字段类型、Schema、格式描述及 task factory 行为。

**阶段证据：** 实现提交 `8df61f8`、修复提交 `a0447ac` 已通过独立规格及质量复核，并合入协调提交 `8bea559`。协调工作区以 Python 3.11.16 运行参数／定义及直接受理消费者单元测试，23 项通过；独立运行 acceptance 集成目录，268 项通过。验证包含完整参数原样保存、原正式源模式和 180 秒预算、无工具路径离线 describe，以及低精度首次导入时的精确快门目录。本阶段不包含实际命令执行、双格式产物流程、安装包或真机验收；这些继续由后续任务负责。

## H2：解析 RAW／JPEG 响应并固定机内身份

**做什么和原因：** 按请求格式识别原图及预览，明确主机文件与相机文件分别叫什么。合法的明确缺名、没有任何名称信息和畸形响应会引起不同行为，解析结果必须能够区分。

**预计文件：** `devices/drivers/hasselblad/responses.py`、`files.py`；测试为 `apps/camctl/tests/unit/devices/test_hasselblad_responses.py`、`test_hasselblad_file_identity.py`。同步调整直接消费解析结果的类型引用。

**输入与交付：** 消费 H1 的格式；交付命名双文件、严格的机内路径分类及可持久化 locator。主机源是否可登记由 H3 消费这些事实后决定。

- [x] **步骤 1：增加双格式及名称分离测试。** `test_capture_response_assigns_jpeg_original_and_preview` 验证两份 JPEG 分别归入角色且 `.thumbnail.jpg` 配对；`test_camera_locator_is_independent_from_host_filename` 使用不同的主机名、机内 basename 和完整路径，验证身份序列化往返后全部保持。
- [x] **步骤 2：增加响应分类测试。** `test_capture_response_requires_complete_unambiguous_records` 分别覆盖重复成功标记、重复／缺失 saved、同文件冒充两角色、格式不符、额外不可解释记录；`test_camera_locator_distinguishes_explicit_missing_and_invalid` 覆盖明确缺名、意外缺失、重复／冲突路径、路径与缺名提示同时出现，以及非法 POSIX 路径各分区。
- [x] **步骤 3：运行并确认失败。** 执行 `camctl_pytest apps/camctl/tests/unit/devices/test_hasselblad_responses.py apps/camctl/tests/unit/devices/test_hasselblad_file_identity.py -q`。
- [x] **步骤 4：实现明确的解析与身份模型。** 成功响应严格要求唯一成功标记和两条配对记录；机内路径独立于主机工作目录校验，拒绝空段、`.`、`..`、NUL、换行等非法身份。完整路径进入定位事实，机内 basename 供 `original`／`thumbnail` 下载。更新当前内部 identity／locator 的精确结构与往返校验。
- [x] **步骤 5：核对通过及消费者。** 重跑步骤 3；同步当前解析夹具到完整工具响应，确认没有消费者再用本地文件名补造机内名。进程退出结果仍由 H4 判断，解析成功文本不覆盖进程失败。
- [x] **步骤 6：审查并形成阶段提交。** 建议提交名为“明确 Hasselblad 双格式响应与机内文件定位”；向 H3、H4 交付实际解析签名及内部结果字段。

**阶段证据：** 实现提交 `32320bf`、修复提交 `9e10959` 已通过独立规格及质量复核，并合入协调提交 `3902f78`。协调工作区以 Python 3.11.16 完成 camctl 全部单元测试，6199 项通过、1 项跳过，保留 2 条既有 pytest 标记警告；devices 集成目录 220 项通过，主机源、设备源及完整历史恢复三个直接消费文件共 50 项通过。验证使用拍摄返回的真实 locator 构造两种下载身份，并核对 basename、独立字节及摘要。本阶段不包含完整 JPEG 文件责任或命令序列；明确缺名提示的双流接线由 H4 完成。随后协调工作区完整执行 capture 目录，1923 项通过，耗时 913.19 秒；该次以遇到首个失败即停止的方式运行到全部结束，退出码为 0。H3、H5、H8 的后续改动继续核对各自门禁；安装包、ARM、真机及客户端导入另行验证。

## H3：贯通文件责任、归档、登记及恢复

**做什么和原因：** 在物理文件创建前确定正确用途及扩展名，并让成功守卫、历史和恢复沿用同一依据。JPEG 两角色 MIME 相同，只有显式角色才能避免错放、覆盖或漏登记。

**预计文件：** `host_files/paths.py`、`host_files/capture_sources.py`、`capture/host_photo.py`、`persistence/repositories/host_capture.py`、`persistence/repositories/capture.py`、`history/host_photo_request.py`；按实际结构调整格式描述的共同依赖。测试包括 `apps/camctl/tests/unit/host_files/test_paths.py`、`apps/camctl/tests/unit/history/test_host_photo_request.py`，以及组件集成 `host_files/test_capture_sources.py`、`capture/test_host_photo_storage.py`、`capture/test_host_photo_flow.py`、`capture/test_device_photo_flow.py`、`capture/test_host_photo_complete_history_recovery.py`。

**输入与交付：** 消费原动作生效格式、H2 的双文件及机内定位；交付正确的两份文件责任、正式源及预览关系。沿用 `PrepareHostPhoto`、`HostPhotoComplete`、原操作键核实及完成事务。

- [x] **步骤 1：增加路径及角色测试。** `test_photo_file_responsibilities_follow_saved_capture_format` 验证 RAW／JPEG 原图分别分配对应扩展名，preview 始终为 JPEG，重复 prepare 复用原身份；`test_archive_uses_explicit_role_for_two_jpegs` 验证相同 MIME 下分别归档，并拒绝用途不符、内容不符及同文件引用。
- [x] **步骤 2：增加主机／设备源分类测试。** `test_explicit_missing_camera_name_only_allows_host_source` 覆盖规格的名称分类与两种源模式；设备缺名时保留工作副本及诊断，不能形成伪造设备源。`test_device_source_uses_camera_basename_and_full_locator` 验证登记和随后下载消费原机内身份。
- [x] **步骤 3：确认失败。** 先运行相应纯单元；再分别运行 `camctl_pytest apps/camctl/tests/integration/host_files -q` 与 `camctl_pytest apps/camctl/tests/integration/capture/test_host_photo_storage.py apps/camctl/tests/integration/capture/test_host_photo_flow.py apps/camctl/tests/integration/capture/test_device_photo_flow.py -q`。两条集成命令顺序执行。
- [x] **步骤 4：实现责任创建与共同校验。** `prepare` 从已保存 action 推导格式并在分配时生成正确路径；归档按显式用途选择角色，再验证 MIME／签名。同步 `_files`、成功双文件守卫、晋升守卫及历史申请校验。仅能看事件的解析层检查合法结构；能读取 action 的仓储继续检查格式与路径相符。
- [x] **步骤 5：实现双正式源登记。** `_photo_artifacts` 核对保存格式和显式角色；主机源消费本地结果，设备源消费可靠机内定位。保留工作区边界、完整长度、签名、SHA-256、文件及目录同步；成功双文件、输出关系与适用终态共同提交，设备源可靠登记后才按原规则回收工作文件。
- [x] **步骤 6：固定回执未知及恢复。** `test_jpeg_photo_unknown_complete_reuses_original_request_and_pair` 在事务提交但回执丢失时核实原完整申请，断言只有一对身份和一次输出登记。`test_photo_recovery_uses_saved_format_and_camera_locator` 验证恢复、同历史边界重建、长度及摘要继续匹配原事实。重跑上述范围和相关历史测试。
- [x] **步骤 7：审查并形成阶段提交。** 建议提交名为“贯通 Hasselblad 双格式文件责任与正式源”；交接归档／登记输入、完整申请和恢复不变量。

**阶段证据：** 实现提交 `930870a` 已通过独立规格及质量审查，并合入协调提交 `19e3f73`。协调工作区以 Python 3.11.16 顺序完成全量单元测试，6204 项通过、1 项跳过，保留 2 条既有 pytest 标记警告；拍摄与历史直接组合 135 项、host_files 全目录 120 项、主机照片取回 6 项及实际依赖检查 7 项全部通过。任务分支另完成 devices 全目录 220 项验证。测试覆盖 RAW／JPEG 与 host／device、明确缺名分类、独立角色和机内定位，以及原事务回执未知和合法回收后的同键核实。完整 capture 目录、安装包、ARM、真机、客户端导入及 ACK 尚须由后续任务完成，H2 的全目录结果不替代本阶段新代码的扩大验证。

## H4：按完整参数执行一次有限命令序列

**做什么和原因：** 将用户明确参数变成工具命令，保留每步实际结果。设置和调焦会产生副作用，必须在一个 START、一个预算内按顺序执行，并能说明拍摄前失败时已经发生了什么。

**预计文件：** `devices/drivers/hasselblad/driver.py`、建议新增的 `commands.py`，必要时调整 H1 的同源命令元数据。测试为 `apps/camctl/tests/unit/devices/test_hasselblad_driver.py`，建议新增同目录 `test_hasselblad_commands.py`，及 `apps/camctl/tests/integration/devices/test_hasselblad_managed_capture.py`、`test_hasselblad_download.py`。

**输入与交付：** 消费 H1—H3 的参数、解析和文件角色接口；交付完整 `photo_taken` 内部结果及步骤事实，供原尝试保存和 H5 收场使用。

**内部结果接缝：** 按[独立内部诊断契约](../../camctl/database/operation-fields.md#受管工具的内部诊断)扩展当前尝试结果：原始双流与结构化结果同事务保存，不能将整份输出放入观察或错误步骤来代替独立诊断。预计涉及 `operations/models.py`、`result_format.py`、`validation.py`、`devices/evidence.py`，以及原结果保存和事件守卫；可以增加专责诊断类型与纯编解码模块。命令槽与数量上限同源，历史只消费冻结值，公共协议保持所属业务表示。

先为类型、可逆字节编码、每流上限、数量上限及双流空缺状态增加失败测试，再接入驱动。RAW／JPEG 分别覆盖 stdout 缺名、stderr 缺名、跨流重复／冲突及必要流失败；原始字节包含非法 UTF-8 或 NUL 时仍可作为诊断往返，不代表合法响应语法。增加真实 SQLite 的原结果、历史、同键重送及保存未知测试；改变诊断任一字节必须被原申请核实拒绝。编码失败保留原完整申请，不进入用替代结果覆盖原失败的路径，不重发设置、调焦或拍摄。

- [ ] **步骤 1：增加参数到命令的测试。** `test_photo_setup_commands_follow_selected_branches` 覆盖 M／A／S／P 的适用字段、所有白平衡模式、MF 省略／正负移动及 AF-S。手动白平衡严格为 `wb-mode manual` 后 `wb <温度>,0`；自动分支没有额外色温设置。AF-S 设置后恰执行一次 autofocus；MF 省略移动时只设置模式。
- [ ] **步骤 2：增加真实调用边界的测试。** `test_photo_driver_checks_identity_before_any_setting` 验证缺失、多台、序列号不符时零设置零拍摄；`test_photo_driver_uses_saved_values_in_structured_argv` 核对配置产品标识、原工作目录及三个短快门，确认原始精确值进入十进制 argv。
- [ ] **步骤 3：增加预算和失败测试并确认失败。** `test_photo_commands_share_one_monotonic_start_budget` 使用可控时钟扣减 list、设置、调焦等耗时，断言后续命令使用剩余预算；`test_focus_success_then_capture_blocked_preserves_side_effect` 保存移动成功及照片 `NO_EFFECT`。`test_attempt_status_is_independent_from_capture_effect` 分别覆盖调焦结果未知但 capture 可靠未发、capture 超时且实际错误已取得、原调用结果完全缺失：前两项保存实际失败，后一项保存尝试未知；采集效果独立判断。运行驱动／命令单元和 `camctl_pytest apps/camctl/tests/integration/devices -q`。
- [ ] **步骤 4：实现命令规划与执行。** `_request_config` 接受且验证完整保存参数。`control()` 沿现有受管工具入口依次执行 list、info、单张模式、格式、曝光、白平衡、对焦和一次 capture；每次派发前检查剩余预算、窗口及普通资格。上一条命令正常退出且为 0 才继续，所有 argv 使用结构化列表。
- [ ] **步骤 5：分别构造尝试状态、采集效果与收场。** 实际失败在可靠收场后保存 `FAILED`，采集效果按 capture 事实独立选取 `NO_EFFECT`／`UNKNOWN`／`CONFIRMED`；原结果取得不到才保存 `UNKNOWN`。只有整次没有派发工具且已阻止迟到执行，才使用 `settlement.basis=not_dispatched`。步骤记录保留原错误、设置／镜头事实及未知效果；H5 消费这些事实。
- [ ] **步骤 6：验证输出及下载。** `test_capture_failure_text_cannot_override_nonzero_exit_or_incomplete_output` 覆盖非零、信号、超时、中断及输出不完整；`test_saved_capture_preserves_named_pair_and_locator` 验证解析与双文件核验后的内部结果。下载测试验证 basename、读取种类、原绑定、长度及原摘要，多个候选或内容不符必须失败。重跑步骤 3 范围。
- [ ] **步骤 7：审查并形成阶段提交。** 建议提交名为“按完整参数执行 Hasselblad 单次拍摄”；交接实际内部结果字段、失败分类、步骤证据及 START 入口可观察事实。

## H5：闭合 START 取消边界及失败恢复

**做什么和原因：** “没有采集照片”不能推出“整次驱动调用没有开始”。本任务在执行协调、资格读取、写入守卫和恢复处使用一致的入口事实，确保取消不会在设置或调焦后错误生效。

**预计文件：** `capture/handlers.py`、`capture/host_photo.py`、`persistence/repositories/capture_facts.py`、`cancellation/rules.py`、`cancellation/service.py`、`persistence/repositories/cancellation.py`；按实际接缝核对 `bootstrap/flows.py` 及原尝试结果格式。建议新增 `apps/camctl/tests/integration/cancellation/test_hasselblad_start_boundary.py`，扩展该目录 `test_eligibility.py`、`test_target_types.py`、`test_effects.py`，以及组件集成 `bootstrap/test_capture_cancel_after_call.py`、`capture/test_host_photo_complete_history_recovery.py`。

**输入与交付：** 消费原尝试意图、执行协调事实及 H4 的实际结果；交付共同取消资格判断，以及保留原文件与尝试责任的失败／恢复路径。入口事实使用既有结果及协调机制表达；缺失证据保持未知，不要求每条工具命令单独持久化。

自动预览是否取消，分别核对[直接目标范围与拍摄关联](../../architecture/task-cancellation.md#自动预览取回的取消关联)及[取消联动](../../architecture/preview-obtaining.md#取消联动)。拍摄被拒绝只阻止通过该拍摄扩大的联动范围，不撤销取回本身作为直接目标的取消资格。

| 需要覆盖的状态 | 本项预期 |
| --- | --- |
| 可靠未进入 START，且能防止迟到调用 | 允许取消，原 START 不会进入 |
| 状态库可靠，但只有意图、无法确认是否进入，或原结果缺失 | 拒绝本次逐项取消，继续原责任及有限核实，不重放 START |
| 已进入 START，仍在 list／设置／调焦，capture 未发送 | 拒绝本次逐项取消，目标取消标记保持未设置 |
| START 已结束且照片为 `NO_EFFECT`，动作仍待失败收场 | 保持入口事实，拒绝本次逐项取消，按原失败正常收场 |
| capture 执行中、结果未知，或成功结果待归档／登记 | 拒绝本次逐项取消，继续原结果及文件责任 |
| 原动作已终态或取消已经生效 | 消费原终态或原取消责任，关联项按公共规则处理 |

- [ ] **步骤 1：增加资格矩阵及入口测试。** `test_photo_cancel_uses_whole_start_entry_not_capture_effect` 覆盖上表，分别在驱动入口前、入口后第一条 list 之前、调焦后及返回后登记前设置同步点。即使尚无工具调用，已经进入驱动也须拒绝取消。`test_photo_cancel_before_entry_prevents_late_start` 以实际执行协调制造竞争，验证只有可靠阻止入口后才允许取消；状态库或原提交不可靠时按公共恢复规则核实。
- [ ] **步骤 2：增加各取消入口及预览测试。** `test_photo_cancel_rejection_respects_preview_addressing` 通过单动作、组、计划及原请求目标执行，断言逐项拒绝且原调用有资格时继续；仅通过被拒绝拍摄联动的自动预览意图保留，计划及请求 ID 直接寻址的预览仍独立取消。同一预览同时被直接寻址和联动时去重处理，按直接目标规则执行。拒绝请求及时结束，不等原拍摄自然完成。窗口／预算到期时由原原因阻止后续拍摄。
- [ ] **步骤 3：确认失败。** 执行 `camctl_pytest apps/camctl/tests/integration/cancellation -q`，再单独运行上述 bootstrap 及 capture 测试。失败证据应揭示入口事实被采集效果替代或关联项被错误修改。
- [ ] **步骤 4：实现共同事实读取及守卫。** 在授予、实际调用和取消之间使用同一协调边界；可靠区分未进入、已进入及未知。正面的调用入口事实随原结果保留，结束后仍可判断；崩溃丢失结果时保持未知。取消读取、应用事务与写入守卫消费同一判定，核对 `load_start_facts`、`_settle_unstarted_attempt`、`_failure_after_return` 等消费者，保留其他相机任务原有语义。
- [ ] **步骤 5：补齐收场及恢复回归。** `test_photo_setup_failure_settles_activity_without_claiming_camera_unchanged` 验证真实失败、占用及活动按收场依据处理；`test_photo_restart_after_relative_focus_never_replays_start` 分别在移动后结果缺失、完整结果保存后、完成回执未知时恢复，设置／focus／autofocus／capture 不重放。可靠结果继续登记；缺失结果保持未知并有限收场。最终动作错误映射为已登记的通用错误，不直接复制工具内部错误码；步骤细节只在内部历史。
- [ ] **步骤 6：验证通过并形成阶段提交。** 顺序运行 cancellation、相关 capture、bootstrap 范围；建议提交名为“统一 Hasselblad START 取消与恢复事实”。交接所有取消入口、入口事实保存位置及终态／占用验证记录。

## H6：实现通用中文选项及精确数字编辑

**做什么和原因：** 客户端从 Schema 的常量和标题生成选择器，机器值用于保存，中文标题用于阅读。实现放在通用控件层，使曝光分支在加入带标题数值后仍可级联，并继续保留未完成输入。

**预计文件：** 客户端 `src/web/scalar-schema.ts`、`parameter-options.ts`、`object-branches.ts`、`Fields.tsx`、`ObjectBranchField.tsx`、`JsonFieldRules.tsx`、`DeviceGuide.tsx`，以及建议新增的 `scalar-options.ts`。仅在需要时调整 `src/shared/json.ts`、`object-variants.ts` 现有工具，复用其精确数值身份及私有分支缓存。

**测试位置：** `apps/client/tests/unit/parameter-options.test.ts`、`object-branch-controls.test.ts`、`fields.test.ts`、`json-field-rules.test.ts`；集成为 `browser-camera-parameters.test.ts`、`browser-rule-display.test.ts`、`parameter-variants.test.ts`、`browser-linked-parameters.test.ts`。

**输入与交付：** 消费导出的通用 Schema，交付带原数值上下文的 `ScalarChoice`、合法对象分支及统一标签；选择器、字段说明和设备指南共同消费元数据。

- [x] **步骤 1：增加选项资格测试。** `titled scalar constants preserve exact machine values` 覆盖字符串／数字 const、缺失标题的机器值回退、重复标题的值区分、精确重复 const 的拒绝资格、非法或不可解析分支的 JSON 编辑回退；沿现有候选限制覆盖 4096／4097 边界。修改标题、打开说明和切换表单／JSON 均不改变正文及合法性。
- [x] **步骤 2：增加分支和独立编辑测试。** `incomplete white balance does not disable exposure choices` 验证色温未填不阻断曝光独立选择；`object branches use titles and preserve per-branch drafts` 验证中文模式名、切换后恢复各分支私有值、未选择保持缺省、当前额外字段保留并报错。换设备只改变校验依据，缓存身份沿现有契约。
- [x] **步骤 3：确认失败。** 执行 `pnpm --dir apps/client exec vitest run tests/unit/parameter-options.test.ts tests/unit/object-branch-controls.test.ts tests/unit/fields.test.ts tests/unit/json-field-rules.test.ts`。
- [x] **步骤 4：实现通用解析及控件接入。** 只接受公共编辑契约允许的直接标量 `oneOf` 常量选项，解析本地引用及其原数字上下文；相同机器值比较使用精确 JSON 身份。对象分支资格调用共同标量规则，模式选项使用分支标题；`Field`、规则提示、指南显示使用同一标签来源。非法规则、合法规则下的非法输入分别呈现。
- [x] **步骤 5：增加真实浏览器往返。** `short shutter choices survive preset reopen and export` 在浏览器选择三个短快门，保存并重开预设、应用到独立草稿、导出并比较精确值；曝光／白平衡／对焦切换后子参数保持。验证 ISO／光圈／快门下拉、色温整数输入及 MF 真正未填写状态，并核对曝光四模式的中文标签。预设按现有整体替换规则清理相关缓存，私有缓存不进入公共计划；重新加载能力失败时保留上一份规则与当前输入。
- [x] **步骤 6：验证通过并形成阶段提交。** 运行步骤 3、`pnpm --dir apps/client run typecheck`、`pnpm --dir apps/client run build`，再运行 `VITEST_MAX_WORKERS=2 pnpm --dir apps/client run test:integration`。建议提交名为“支持 Schema 中文常量选项与精确分支编辑”。

**阶段证据：** 实现提交 `83a6d9f`、修复提交 `38fc635` 已通过独立规格及质量复核，并合入协调提交 `126222d`。任务工作区在 Node.js 24.20.0 下完成 2936 项单元、1379 项集成，以及类型检查和构建；协调工作区的六文件单元 302 项、真实 Node 首次导入集成 5 项和类型检查通过。验证覆盖命名常量的精确数字、模式缓存、实际浏览器预设／导出，以及附带局部断言的 4096／4097 常量目录。构建保留主包 858.52 kB 的体积提示。实际安装版 describe、相机拍摄、媒体及 ACK 联合证据继续由 H8、H9 取得。

## H7：更新完整样例及安装资源消费者

**做什么和原因：** 安装后的演示与测试必须提交真实完整参数。唯一生成器当前只校验样例，不补齐参数；源码能力正确但安装样例缺项，现场仍会在受理时失败。

**预计文件：** `docs/camctl/examples/camera-demo/hasselblad-photo.json`、`prepare-plan.py` 的相关说明和测试、`apps/camctl/tests/integration/bootstrap/test_distribution.py`、`test_hasselblad_device_distribution.py`、`test_hasselblad_capture_assembly.py`、`test_package.py`；审计 `tests/integration/_cleanup_roundtrip_support.py`、`tests/integration/test_device_capability_declarations.py`、`apps/camctl/tests/integration/devices/test_hasselblad_registration.py`、`apps/camctl/tests/integration/persistence/test_format_admission.py` 和客户端现有 Hasselblad 夹具。资源映射消费 `apps/camctl/scripts/sync_resources.py`。

**输入与交付：** 消费 H1—H6 的完整规则与执行行为；交付权威文档样例、生成后的包资源及安装版四种格式／源组合的组件证据。

- [ ] **步骤 1：增加安装样例测试并确认失败。** 扩展 `test_installed_hasselblad_sample_uses_actual_unique_capability`，断言安装版 describe 导出完整规则、生成器按实际设备唯一匹配并保留明确参数；缺少必填字段必须失败，示例值不被当成自动默认值。运行 `camctl_pytest apps/camctl/tests/integration/bootstrap/test_distribution.py -q`。
- [ ] **步骤 2：更新权威样例与夹具。** photo 样例显式写入合法格式、曝光、白平衡和对焦；生成器沿用能力查找和 `validate_precise`。用 `rg` 定位各消费者的 type-only 参数及 RAW 专用断言，按各测试目的改为独立合法样例；完整允许域仍从能力消费。
- [ ] **步骤 3：扩展工具边界替身。** `_hasselblad_tool_source` 支持本计划需要的设置 argv、双格式文件和独立机内名称，严格按 `hb_cli` 文本／文件契约返回。参数允许域不在替身另维护；命令顺序与副作用次数由驱动测试及安装链断言。
- [ ] **步骤 4：同步并验证安装资源。** 执行 `apps/camctl/.venv311/bin/python apps/camctl/scripts/sync_resources.py --root .` 和同命令的 `--check`；安装测试从 wheel 读取样例与生成器。将既有 host／device 分发测试参数化为 RAW／JPEG，核对原图／预览各两次取回的身份、长度、摘要及工作副本回收条件。
- [ ] **步骤 5：验证通过并形成阶段提交。** 执行 `camctl_pytest apps/camctl/tests/integration/bootstrap -q`，再运行根 `tests/integration/test_device_capability_declarations.py` 的安装能力测试。建议提交名为“同步 Hasselblad 完整拍摄样例与安装资源”。

## H8：完成安装版跨组件业务验收

**做什么和原因：** 把客户端实际导出的计划交给真实 C host 和安装版 CLI，证明能力、参数、文件及确认在组件接合处一致。组件测试中的工具替身可以隔离相机，但必须保留安装版内置 Hasselblad 驱动及真实交付链。

**预计文件：** 建议新增 `tests/integration/test_hasselblad_photo_parameters.py` 与 `test_hasselblad_photo_roundtrip.py`；扩展 `tests/integration/client_driver.ts` 的 `validate-parameters` 测试接缝，并同步现有调用方 `test_timelapse_time_parameters.py` 和 `tests/integration/README.md`。复用 `camctl_fixtures.py`、`_wsl_host_demo.py` 及 `test_osmo_builtin_timelapse_roundtrip.py` 已有安装链组织，按实际重复情况抽取最少的共同辅助代码。

**输入与交付：** 消费安装 wheel、真实客户端、C HostDemo、H7 的接口受约束工具替身；交付四种组合的独立软件证据和精确参数的三端判定结果。

- [ ] **步骤 1：建立三端参数反例集。** 将 `validate-parameters` 输入改为显式指定 `action_type`、`device_id`、`action_name`，浏览器、客户端后端和导出计划共同使用。新测试 `test_installed_hasselblad_parameter_validation_agrees_across_consumers` 比较浏览器、客户端后端和安装版 camctl 的接受／拒绝：合法分支、短快门、色温边界、错误模式字段、额外 tint、缺项及 `null`。反例集合只定义一次，不复制整份档位目录；更新延时摄影原调用方后确认其原行为保持。
- [ ] **步骤 2：建立四种安装链并确认失败。** `test_installed_hasselblad_photo_delivery_roundtrip` 参数化 RAW×host、RAW×device、JPEG×host、JPEG×device。每个独立请求恰执行一次 capture，从客户端选值、预设重开和导出开始，经 C host 提交、安装版 CLI 执行、SQLite／历史／报告到正式输出；比较三个短快门在草稿、预设、导出正文、受理、生效参数、历史恢复、报告及实际 argv 中的精确数学值，并核对双文件身份。只在工具进程边界替换 hb_cli。
- [ ] **步骤 3：补齐取回及恢复。** 分别明确取回原图和预览并重复取回，验证新 delivery 与原 output／源动作身份。主机正式源在相机不可用时仍可取回；设备正式源使用原机内 basename、绑定及读取种类。注入原结果保存后中断和提交回执丢失，核实同一申请恢复且不重复设置、调焦或拍摄；同一公共请求重送也不重复登记。
- [ ] **步骤 4：验证文件导入与 ACK。** 通过真实 `Files.upload` 导入交付文件，覆盖报告先到、媒体先到及部分文件未到。核对 JPEG 两角色不会因 MIME 或相同内容合并，报告同历史边界重建一致；客户端导出累计 ACK 后，camctl 确认水位符合报告覆盖，媒体缺失单独呈现。单张动作仍省略公共 `result`，按原字段报告设备执行情况；步骤细节保存在内部，失败仍使用现有通用摘要。
- [ ] **步骤 5：执行根组合测试。** 先执行 `apps/camctl/.venv311/bin/python -m pytest tests/integration/test_hasselblad_photo_parameters.py tests/integration/test_hasselblad_photo_roundtrip.py -q`，再按根测试指南运行被本次接缝影响的能力、时间参数及 C host 回归；保留各组合的计划、报告、数据库、工具轨迹、文件摘要和 ACK 证据。
- [ ] **步骤 6：完成软件门禁。** 按下节顺序运行所需完整范围，确认全部通过后更新 `docs/camctl/verification.md`、`docs/client/verification.md`、`docs/camctl/integration-readiness.md` 和硬件交接中的软件状态；建议提交名为“验收 Hasselblad 安装版完整参数与双格式交付链”。

### 软件门禁的范围与顺序

先运行 `camctl_pytest apps/camctl/tests/unit -q`，再按[组件集成测试指南](../../../apps/camctl/tests/AGENTS.md)逐目录、逐进程完成完整组件集成范围。客户端完成 `pnpm --dir apps/client run typecheck`、`pnpm --dir apps/client run build`、`VITEST_MAX_WORKERS=2 pnpm --dir apps/client test`。根跨组件测试遵守[运行入口](../../../tests/integration/README.md)，保留安装链及被修改测试接缝的回归证据。

完成相应代码后运行以下结构检查：

```bash
apps/camctl/.venv311/bin/python apps/camctl/scripts/sync_resources.py --check --root .
node scripts/check-protocol.mjs
apps/camctl/.venv311/bin/python scripts/check-database-spec.py
node --disable-warning=ExperimentalWarning scripts/check-event-transitions.mjs
node --disable-warning=ExperimentalWarning scripts/check-report-dependencies.mjs
node scripts/check-doc-links.mjs
git diff --check
```

数据库检查用于核对实际内部结构与文档一致；公共机器结构按通用协议消费，不因新增相机参数再建立专用公共参数 Schema。构建 wheel 使用 `uv build --project apps/camctl`；安装链核对实际分发内容，不能仅证明源码可导入。每个需要执行的完整范围通过一次即可留证；后续有改动时按影响补验。

## H9：完成 ARM 部署及真实相机验收

**做什么和原因：** 单独验证真实工具、机身和镜头是否接受当前参数，验证主机副本与后续设备下载是否确为同一内容，并取得客户端导入和确认的现场证据。软件替身不能回答这些问题。

**预计文件：** 更新 `docs/hardware/hasselblad/validation.md`，新增有实际日期的该目录验收记录；同步 `docs/hardware/camera-control-handoff.md`、驱动专题及验证入口的状态链接。部署沿用[目标环境与分工](../../camctl/implementation.md#目标环境与部署分工)和 [host 接入说明](../../../apps/host-demo/docs/camctl-integration.md)。

**输入与交付：** 消费 H8 通过的 wheel、源码对应的 C host／客户端、安装资源及可用目标硬件；交付可复现的实际命令、原始结果和逐层验收结论。具体固件、镜头、工具摘要及文件数据以本次观察填写。

- [ ] **步骤 1：准备安装证据。** 记录源码提交、wheel SHA-256、目标 Python／SQLite／ARM 环境、工具二进制摘要和相机／镜头／固件身份。按部署规格导出锁定运行依赖并部署 wheel；从安装资源导出 config、完整 photo、两份 obtain 及唯一生成器，执行 init／describe 并核对完整能力。
- [ ] **步骤 2：编写可复制的现场命令。** 每条命令注明目的、预期、实际输出和证据目录。先完成连接及身份检查，再通过真实客户端和 C host 执行独立计划；取得原动作 ID 后分别生成原图及预览取回。所有拍摄都记录原请求，恢复消费原结果。
- [ ] **步骤 3：按下表完成最小组合。** 四组分别覆盖格式／源模式和四种曝光；WB 与 focus 可按现场条件调整，但实际选择、命令及结果须记录。额外短快门、最长显式曝光及参数域边界按待证明的设备范围补充独立用例；未实测的档位或组合保持未验证。

| 格式与正式源 | 曝光覆盖 | 白平衡及对焦覆盖建议 | 核对重点 |
| --- | --- | --- | --- |
| RAW×host | M，包含一个精确短快门 | manual 色温；MF 省略移动 | 双文件归档；相机离线后的原图与预览取回 |
| RAW×device | A | daylight；MF `5` | 自动快门不被填成要求值；机内身份及两种下载 |
| JPEG×host | S，包含另一个精确短快门 | auto；AF-S | 自动光圈不被补造；autofocus 在 capture 前完成 |
| JPEG×device | P | 另一非手动白平衡；MF `-5` | 两份 JPEG 的独立角色、主机副本与设备下载一致性 |

- [ ] **步骤 4：保存 JPEG 设备源的字节证据。** 在正式源登记回收工作副本前保留本次验收所需的原图／预览验证副本或其可靠摘要，逐一比较随后 `original`、`thumbnail` 下载的长度与 SHA-256，再重复下载核对稳定性。工具缩略图的内嵌提取及生成路径若可分别触发，分别留证；无法识别实际路径时记录未知。内容不同则记录验收未通过，保留原源摘要和相关文件。
- [ ] **步骤 5：完成业务与时间证据。** 各次交付使用真实客户端媒体导入及累计 ACK；分别记录报告、上传结果和 camctl 水位。记录完整 START 各步耗时及 180 秒预算使用，A／P 的实际曝光没有回读依据时保持未知。对焦命令成功和照片清晰度分别记录，不能以命令返回证明镜头距离或图像质量。
- [ ] **步骤 6：记录异常及完成范围。** 某步失败、掉线或结果未知时保留原日志、工作文件、活动及调用事实，按既有有限恢复核实。只将有完整证据的组合标为通过；软件、部署、真实采集、字节一致、导入及 ACK 逐项列出。更新交接链接并形成验收记录提交。

## 完成判据与交接

H1—H8 完成表示软件及安装版联合验收通过；H9 按实际硬件证据单独判断。没有设备或实际路径证据时，H9 对应项保持未完成，软件结论保留自己的范围。

交接时提供阶段提交、实际修改文件、命令及日志、未通过或未验证项，并沿以下链路作一次整体复核：权威参数 → 客户端选值及预设 → 计划受理与执行定义 → 有限命令及副作用 → 角色与机内身份 → 原子登记及恢复 → 原图／预览独立交付 → 客户端媒体导入及报告 ACK。每个箭头均须能指向本计划安排的测试或现场证据。
