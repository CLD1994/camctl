# 设备能力声明与客户端衔接实施计划

> **执行方式：** 使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans` 按任务执行；复选框只在实际实施与验证后勾选。

**目标：** 所有驱动从同一权威定义及当前配置导出正式产物源的有效删除支持和完整任务说明，客户端通过通用规则读取、展示并隐藏不提供的操作入口。

**架构：** 内部设备文件声明同时进入静态 `DriverDefinition` 与运行 `DriverEntry`；驱动提供不访问设备的正式源解析器，供任务绑定与 Catalog 共用。Catalog 按每个逻辑设备的当前配置选择主机或设备文件能力，导出有效删除支持。根协议、客户端加载器及页面消费同一字段；拍摄参数与说明继续来自现有 Schema 和任务契约。

**技术栈：** Python 3.11、JSON Schema Draft 2020-12、TypeScript、React、现有 CLI 和客户端发行资源。

**规格：** [能力说明](../../architecture/capabilities.md)、[拍摄能力与参数类型](../../architecture/camera-capabilities.md)、[参数规则](../../architecture/camera-parameters.md)、[客户端加载](../../architecture/client-deployment.md)、[计划编辑](../../architecture/client-editing.md)、[设备指南](../../client/device-guide.md)、[客户端保存](../../client/storage-and-recovery.md)。内部签名、文件拆分及提交边界是实施建议；新增公共字段须先补入责任专题和根协议。

**执行状态：** 按任务依赖实施。C4 按每个设备当前选定正式源的有效删除声明隐藏快捷入口；C5 的最终页面可见性联合验收依赖 C4 完成。

## 范围与独立交付

本计划负责声明、导出、说明元数据、加载和页面展示。执行参数、拍摄计时、清理执行、数据库及报告继续使用既有契约。公共声明描述当前配置选定的正式产物源是否支持删除，不表示相机硬件永久不支持、当前在线或某次操作一定成功。

[设备文件清理计划](2026-10-18-device-file-cleanup.md)独立负责实际文件操作及保存、恢复。两份计划互不作为实施前置：本计划按现有主机／设备源能力交付；新增文件清理支持后通过原能力与源解析规则自然导出新值。组合部署时核对实际配置和生效值，不另建型号开关或同步清单。

当前正式内置路径是 Action6 普通录像、OSMO 360 II 普通录像／原生延时、Hasselblad 单张 RAW。Action6 与 OSMO 当前配置使用设备源，设备文件删除声明均为 false；Hasselblad 设备文件删除也是 false，但配置选主机源时公共删除支持为 true，选设备源时为 false。此处是审查起点，验收以实际配置及协作者声明为准。Action6 候选延时继续按既有门槛过滤，Hasselblad 保留主机／设备两种正式源。

## 通用契约与评审重点

建议新增公共设备字段 `delete_supported?: boolean`，与 `device_id`、`driver_id`、拍摄 `actions` 同层。它描述该逻辑设备按当前配置选定的正式产物源是否可删除；内部 `DriverDeclaration.delete_supported` 继续描述设备文件协作。`delete_action_outputs` 继续是无 `device_id` 的独立动作。

| 当前选定的正式源 | 公共删除支持的权威输入 |
| --- | --- |
| 主机 | 主机文件协作者的删除能力；不受设备文件删除布尔值限制。 |
| 设备 | 内部驱动设备文件删除声明。 |
| 当前定义缺少源解析器，或解析失败／返回非法值 | 当前目录构建或整份导出失败，不能猜为设备源或补 false。 |

正式源解析由驱动的纯配置规则提供，供任务绑定与 Catalog 共用；不按型号分支，不调用任务工厂并用默认参数猜来源，不以 actions 数量判断来源。当前设备配置确定一种拍摄正式源；内部解析使用既有 `CaptureStorage`，主机修复成品保持独立生命周期。原任务在受理时固定来源，配置更新不搬迁旧文件；公共声明服务当前配置，不能改写旧动作、文件身份或已保存结果。历史清理仍按原文件引用及绑定执行。

| 输入或定义状态 | 导出与加载行为 |
| --- | --- |
| 当前配置引用的驱动有完整内部声明与正式源解析器 | 新 `describe` 逐设备解析当前源，再导出该源协作者的明确布尔值。 |
| 当前静态定义缺声明、类型错误或与运行登记冲突 | 登记或整份导出失败，不输出部分目录。 |
| 旧公共 JSON 缺少字段 | 客户端加载为未知，不补 false。 |
| 公共 JSON 字段为 null、数字、字符串或其他非法类型 | 整份加载失败；保留旧有效目录的既有规则继续生效。 |
| `actions: []`，但设备声明有效 | 保留设备和删除声明；拍摄动作数量不决定删除能力。 |
| 多个逻辑设备使用同一个 driver_id | 分别按 device_id 的配置解析及导出；主机／设备源不同可以得到不同值。 |

旧 JSON 的兼容与当前 Python 静态定义的完整性分开处理。当前导出不能用可选构造器掩盖缺失声明；Catalog 不通过运行端口探测或临时查全局运行登记获取值。

重点审查五个接缝，并在相应任务验证：

1. 静态／运行声明漂移与 Catalog 两次重建丢字段：C1、C2。
2. 任意驱动标识、重复 driver_id、空拍摄目录被错误按型号或动作数量判断：C2、C4。
3. 字段缺失、有效空目录、首次加载失败和失败后保留旧目录被混淆：C2、C4。
4. 隐藏新增选项导致旧清理动作被标成非法、分支丢失或原请求改写：C4。
5. 源码说明正确，安装资源、HTTP、表单或设备指南未消费同一份定义：C3、C5。

## C1：将所有驱动的声明传入静态目录

**预计文件：** `apps/camctl/src/camctl/devices/catalog.py`、`definitions_runtime.py`、`tasks.py`、`drivers/registry.py`、ADB `contracts.py`／`registration.py`，Hasselblad `configuration.py`、`definitions.py`、`driver.py`、`registration.py`；必要时新增纯声明模块与主机文件能力模块。同步 `docs/architecture/camera-capabilities.md`、`capabilities.md` 的源解析职责。更新真实构造点及演示／测试夹具。

下文 Python 源码简称相对 `apps/camctl/src/camctl`，组件测试简称相对 `apps/camctl/tests`；客户端测试简称相对 `apps/client`。根 `tests/integration` 用完整路径说明。

**接口建议：** `DriverDefinition` 必须提供 `declaration: DriverDeclaration` 和纯 `source_storage_resolver(device_declaration) -> CaptureStorage`。ADB 合同明确固定 DEVICE；Hasselblad 复用现有配置读取的 output_storage，同时供部署任务绑定使用。主机文件支持由一个纯能力定义维护，实际本地协作者与导出共同引用，避免第二份硬编码支持表。Catalog 的两次变换使用 `replace(definition, actions=...)` 保留内部声明与解析器，再按每个 device_id 的配置解析源及其能力；不访问运行登记或设备。内部声明的同源指一个权威计算及相同值，不要求同一 Python 对象身份。

任务绑定与导出复用同一纯配置规则；对已冻结设备声明重复求值仍属同源，不要求只有一次函数调用，也不要求迁移现有 `DeploymentTaskFactory` 签名。Hasselblad 配置与定义模块保留局部导入或使用纯模块避免循环；主机纯能力模块不依赖 bootstrap 或 Catalog。ADB 录像／延时的固定设备源通过合同表达，不向既有执行定义新增存储字段。解析器应保持可重复装配，同源定义重复构造不因新建闭包身份不同产生假冲突。

- [x] **步骤 1：写失败测试。** 在 `apps/camctl/tests/unit/devices/test_catalog.py`、`test_definitions_runtime.py` 与 integration/devices 的 `test_adb_camera_registration.py`、Hasselblad 注册用例中，覆盖内部声明与源解析器缺失／非法／冲突、两次 Catalog 转换、部署任务绑定、无拍摄能力和重复登记。内部声明与运行登记比较；验证同一驱动两个不同 serial 的逻辑设备分别选 HOST／DEVICE，源能力计算不同但内部声明不变。HB 默认及显式 host 得 true、device 得 false，非法配置整份失败；合成驱动按相同规则验证。
- [x] **步骤 2：运行最窄目标确认失败。** 单元与组件集成分进程执行，失败来自缺少声明传递或一致性检查。
- [x] **步骤 3：实施同源传递。** 先同步责任规格，再为所有当前定义及部署适配提供完整内部声明和源解析器；首次登记与重复装配刷新都核对静态／运行一致性，批次先完成身份、重复与内部声明检查，再修改登记。批次后部拒绝后两个登记点均保持原快照。受理与 describe 共用 Catalog 的配置规则；所选源与首次受理固定的任务来源一致。定义错误按配置／内部错误处理；源变化不改旧执行定义。
- [x] **步骤 4：验证并提交。** 覆盖真实内置登记、`kind` 拍摄过滤、`deployment_task_factory` 绑定及 `apps/camctl/tests/integration/acceptance/test_adb_camera_definitions.py`。字段不能因没有录像工厂或 `actions: []` 丢失；不增加设备 I/O。

## C2：扩展公共删除声明并接通真实加载

**预计文件：** 根 `protocol/schemas/capabilities.schema.json`、能力样例、`protocol/README.md`、`docs/architecture/capabilities.md`、`camera-capabilities.md`；`Catalog.describe_document`、客户端 `src/shared/types.ts`，按需要调整 `shared/capabilities.ts` 与 HTTP 传递。包资源由现有生成器同步。

**接口：** 消费 C1 按当前设备配置解析的正式源能力，产出公共可选布尔字段；客户端只从根协议执行整份校验。当前生产导出逐设备有值；旧文件可以缺字段。

- [ ] **步骤 1：写结构和加载失败测试。** 实际 `loadCapabilities` 接受 true、false、旧缺字段，拒绝 null／非布尔／未知外层属性；原始 JSON 的重复键通过实际重载的 `parseJson → loadCapabilities` 链拒绝。夹具使用任意 driver_id、共享 driver_id 的两个设备、`actions: []` 和有效空目录；清理仍不能放进拍摄 `actions[]` 或携带 `device_id`。
- [ ] **步骤 2：运行目标用例确认失败。** 扩展 `apps/client/tests/integration/video-estimate-capabilities.test.ts`、`http.test.ts` 及 Python Catalog 用例；不要用自建简化加载器替代生产路径。
- [ ] **步骤 3：实现协议与导出。** 公共值只读取 C1 的当前正式源能力，缺静态定义或源解析失败使新导出失败。更新 Schema、样例、类型、责任文档和协议入口；保持预览、估算及精确数字原样传递。真实导出验证 HB host／device 及同驱动不同配置，不能只用合成布尔对象证明派生正确。
- [ ] **步骤 4：验证重载与交接并提交。** `/api/state` 和重载返回保留字段；成功整体替换，失败保留旧 active 与错误，首次失败仍无 active。仅删除字段变化不改草稿／预设／原请求；预览字段同时变化时继续执行既有自动预览规则。运行 `node scripts/check-protocol.mjs`。

## C3：补齐全部内置正式任务的同源说明

**预计文件：** ADB `definitions.py`、`timelapse_parameters.py`、`contracts.py`、`osmo360ii_capture.py`，Hasselblad `definitions.py`，`docs/architecture/camera-parameters.md`。消费者为客户端 `Fields.tsx`、`Editor.tsx`、`DeviceGuide.tsx`。

**接口：** 原有 `ActionCapability.name/description` 与 Schema `title/description`。任务固定设置和启动后取消限制由与执行任务一致的权威定义提供，不新增公共取消字段，不依型号名称在客户端补文案。

- [ ] **步骤 1：写正式说明失败测试。** 覆盖 Action6 录像、OSMO 录像和原生延时、Hasselblad 单张。所有用户可填写字段及实际分支具有可读名称、含义和单位：秒、EV，固定白平衡用 K。包括 Action6 的分辨率、视野、防抖、可选光圈／码率和曝光成员，不只检查 OSMO。
- [ ] **步骤 2：运行 unit/devices 的 `test_adb_camera_parameters.py`、`test_osmo360ii_capture.py` 及 Hasselblad 能力用例确认失败。** 断言事实和结构，不逐字锁定人类文案。
- [ ] **步骤 3：实施任务与字段说明。** OSMO 录像说明全景、8K／30 fps、D-Log M、高码率请求；Manual 为 ISO 800、1/60 秒快门，两种曝光均请求 5200 K，Auto 为 EV 0。原生延时说明全景、8K／30 fps，只有 Manual 请求 ISO 800、1/60 秒和 5200 K。Action6 说明自身固定设置及省略光圈／码率时保持原设置；Hasselblad 说明单张 RAW 和沿用机身设置。请求设置、独立读回、假定参考码率分别表达。
- [ ] **步骤 4：补启动后取消说明。** 从实际任务停止及取消策略生成说明并核对工厂返回：Action6／OSMO 普通录像支持启动后取消；OSMO 有限原生延时沿原目标结束策略收场，不承诺立即停止；Hasselblad 启动前可取消、启动后不支持取消。不能只用驱动级 stop 布尔值替代所有参数类型的任务策略。
- [ ] **步骤 5：验证 UI 消费并提交。** 扩展 `apps/client/tests/integration/browser-camera-parameters.test.ts`、`browser-editing.test.ts`，用经公共协议校验的元数据夹具核对表单、任务说明与指南同源且查看不改草稿；组件测试不启动 Python CLI。保留测试输入注入方式，供 C5 根跨组件验收加载真实安装版导出，避免复制生产参数和说明清单。保持参数域、命令、默认值、预览、估算与时间组合；参数专题将设备进度统一引用[验证记录](../../camctl/verification.md)和对应完成计划。

## C4：按通用声明控制页面入口

**前置：** C2 完成，提供每个设备当前选定正式源的有效删除支持；本任务不依赖另一份文件清理计划。

现有报告能关联来源动作的逻辑 device_id；页面使用该设备当前配置选定正式源的有效删除声明。`kind=repaired` 能识别主机修复成品。历史原图／预览的位置不能从当前配置还原，配置变化后的旧产物由后端按原文件事实执行；页面不推断迁移，也不改变执行资格。

| 入口或输入 | 展示行为 |
| --- | --- |
| Editor 新动作下拉 | 保留独立通用清理动作。 |
| Records 计划清理 | 保留；计划范围可以混合设备与主机文件。 |
| ActionResult 动作清理 | 来源动作匹配的当前设备有效删除声明为 false 时隐藏；true 或未知时保留。 |
| MediaResults 单产物清理 | repaired 保留；其他产物关联设备当前有效声明为 false 时隐藏，true 或未知时保留。 |
| 旧公共字段缺失、来源／设备未知、有效空目录 | 保留相应原入口，其他设备的 false 不影响该入口。 |
| Hasselblad 当前选择主机源／设备源 | 按源能力显示：当前主机源为 true；当前设备源且无设备删除接口时为 false。 |

规则使用当前启用目录，适用于任意驱动；设备重新配置后按当前关联和当前正式源作展示，不还原历史存储位置。历史主机 original／preview 在当前切换为设备源且有效声明 false 后，快捷按钮也会隐藏；后端仍能通过独立通用请求按原主机引用清理它们。

**预计文件与接口：** 前端新增纯可见性函数，消费 active 能力与页面已有来源关系；在 `device-options.ts`、`Editor.tsx`、`Records.tsx`、`ActionResult.tsx`、`MediaResults.tsx` 和上游 `App.tsx` 传入必要数据。`DeviceGuide.tsx` 展示 true／false／未知。`BuiltinFields.tsx` 与 `followup.ts` 保持既有参数编辑和追加职责，只有真实消费缺口才修改。

- [ ] **步骤 1：按矩阵写失败测试。** `tests/unit/device-options.test.ts` 与真实浏览器覆盖四类入口、任意驱动、混合设备、来源缺失、repaired 和配置切换后的历史主机 original/preview。验证 HB 主机源保留、设备源 false 隐藏，以及其他设备或未知来源入口保留；至少有一个现有报告流程可达的 false 隐藏场景。
- [ ] **步骤 2：测试现有输入保留。** 新选择隐藏与旧动作合法性分别判断；已有清理动作继续正确显示、编辑、保存和按既有规则导出，不能仅因候选被隐藏标成“当前不可用”。停用分支仍可恢复已填写内容，JSON／导入／预设／原请求保持原值。
- [ ] **步骤 3：运行目标用例确认失败后实施过滤。** 仅在各入口生产者判断显示；追加流程和后端校验不新增能力阻止。指南展示的是声明，不表示在线、物理文件位置或操作成功。
- [ ] **步骤 4：验证重载并提交。** true↔false 成功重载即时更新显示；失败且有旧 active 沿用旧值并显示错误，首次失败为未知，有效空目录为明确空集合。核对 busy 状态继续遵守原页面操作锁，不因新增条件放开在途操作。检查旧草稿全部分支、未完成原文与已导出请求不变。

## C5：完成安装版声明到客户端的软件验收

**预计文件：** `apps/camctl/tests/integration/bootstrap/test_distribution.py`，根集成新增 `tests/integration/test_device_capability_declarations.py`，复用真实 CLI 与客户端测试入口；更新两组件验证记录。

**分段门禁：** 安装版 CLI、协议、实际加载、HTTP 和指南验收依赖 C1—C3，可以先行；最终页面可见性联合验收依赖 C4 实施。两段均不依赖文件清理计划，前段完成不能代替整份计划完成。

根联合用例负责 CLI 与客户端的真实组合，可将安装版导出的 JSON 文件通过测试专用输入交给 C3 的浏览器用例，例如 `CAMCTL_TEST_CAPABILITIES_FILE`。客户端组件套件默认使用协议夹具，不增加 Python CLI／虚拟环境前置；正式固定设置与取消事实由驱动测试验证，联合验收再确认实际导出进入页面。

- [ ] **步骤 1：写联合失败测试。** 从仓库外安装 wheel，离线按真实配置执行 describe，与源码按当前配置解析的正式源能力比较；先核对根 Schema、包内资源、实际 loader、HTTP 和设备指南。覆盖 HB host／device、同驱动不同逻辑设备及源模式改变后旧执行定义保持。C4 完成后增加按当前源分别显示的真实页面联合验收。包含全部内置驱动与任意驱动夹具，声明变化本身不产生拍摄或清理调用。
- [ ] **步骤 2：运行新目标确认失败，再补齐发行与消费接缝。** 安装环境不依赖源码目录；新导出包含明确值，旧 JSON 仍可加载。文件系统、SQLite 和进程组合放集成测试。
- [ ] **步骤 3：运行整合门禁并独立复核。** 完成下列命令及本计划窄目标；只对后续修改、失败或新风险扩展复测。审查从权威定义到每个实际消费者的链路，记录环境、日志、退出码和未验范围后提交。

```bash
node scripts/check-protocol.mjs
PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/devices -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/devices -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/acceptance/test_adb_camera_definitions.py -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/bootstrap/test_distribution.py -q
pnpm --dir apps/client run test:unit
pnpm --dir apps/client run test:integration
apps/camctl/.venv311/bin/python -m pytest tests/integration/test_device_capability_declarations.py -q
node scripts/check-doc-links.mjs
git diff --check
```

命令从任务工作树根运行；先核对 Python 3.11 和客户端规定的 Node.js。环境缺失时按[camctl 测试指南](../../../apps/camctl/tests/AGENTS.md)准备，显式使用当前源码。相同工作树的组件集成命令串行、按目录分别启动；客户端集成入口先构建。实施遵守[工作区隔离与阶段提交](../../../.agents/rules/implementation.md#git-协作与工作区隔离)。文档检查另行核对新增锚点。

## 完成判据

- [ ] 所有已配置设备按当前正式源导出删除支持；HB host 为 true、device 为 false，内部设备文件声明保持真实值。当前定义缺失与旧公共 JSON 缺字段分别处理。
- [ ] 全部内置正式任务及实际可调字段说明完整，取消说明与任务执行策略一致，客户端不维护型号分支。
- [ ] C4 按每个设备当前选定的源分别显示，通过真实可达入口验证；旧输入、原请求、主机与混合来源符合矩阵。
- [ ] 安装版 CLI、协议资源、HTTP、实际客户端加载和页面一致；未执行设备操作，证据不扩展为真机删除验收。
