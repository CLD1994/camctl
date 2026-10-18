# 设备能力声明与客户端衔接实施计划

> **执行方式：** 使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans` 按任务执行；复选框只在实际实施与验证后勾选。

**目标：** 所有驱动从同一权威定义导出真实设备能力和完整任务说明，客户端通过通用规则读取、展示并隐藏不提供的操作入口。

**架构：** 驱动声明同时进入静态 `DriverDefinition` 与运行 `DriverEntry`，Catalog 保留声明并导出设备层删除支持值。根协议、客户端加载器及页面消费同一字段；拍摄参数与说明继续来自现有 Schema 和任务契约。

**技术栈：** Python 3.11、JSON Schema Draft 2020-12、TypeScript、React、现有 CLI 和客户端发行资源。

**规格：** [能力说明](../../architecture/capabilities.md)、[拍摄能力与参数类型](../../architecture/camera-capabilities.md)、[参数规则](../../architecture/camera-parameters.md)、[客户端加载](../../architecture/client-deployment.md)、[计划编辑](../../architecture/client-editing.md)、[设备指南](../../client/device-guide.md)、[客户端保存](../../client/storage-and-recovery.md)。内部签名、文件拆分及提交边界是实施建议；新增公共字段须先补入责任专题和根协议。

**执行状态：** C1—C3 及 C5 的声明、安装和加载验收可以先行；C4 的页面隐藏范围仍需确定，定稿前不交接该任务实施。C5 的最终页面可见性联合验收依赖 C4 完成。

## 范围与独立交付

本计划负责声明、导出、说明元数据、加载和页面展示。执行参数、拍摄计时、清理执行、数据库及报告继续使用既有契约。声明描述当前已部署驱动的能力，不表示相机硬件永久不支持、当前在线或某次操作一定成功。

[设备文件清理计划](2026-10-18-device-file-cleanup.md)独立负责实际文件操作及保存、恢复。两份计划互不作为实施前置：本计划可以按当前 false 声明交付；文件清理接入后通过同一权威声明自然导出新值。组合部署时核对二者的实际生效值，不另建型号开关或同步清单。

当前正式内置路径是 Action6 普通录像、OSMO 360 II 普通录像／原生延时、Hasselblad 单张 RAW；这些驱动当前设备删除声明均为 false。此处是审查起点，验收以各驱动实际声明为准。Action6 候选延时继续按既有门槛过滤，Hasselblad 保留主机／设备两种正式源。

## 通用契约与评审重点

建议新增公共设备字段 `delete_supported?: boolean`，与 `device_id`、`driver_id`、拍摄 `actions` 同层。它描述设备文件删除支持；主机正式源的本地清理按自己的能力执行。`delete_action_outputs` 继续是无 `device_id` 的独立动作。

| 输入或定义状态 | 导出与加载行为 |
| --- | --- |
| 当前配置引用的驱动有完整声明，值为 true／false | 新 `describe` 逐设备明确导出相同布尔值。 |
| 当前静态定义缺声明、类型错误或与运行登记冲突 | 登记或整份导出失败，不输出部分目录。 |
| 旧公共 JSON 缺少字段 | 客户端加载为未知，不补 false。 |
| 公共 JSON 字段为 null、数字、字符串或其他非法类型 | 整份加载失败；保留旧有效目录的既有规则继续生效。 |
| `actions: []`，但设备声明有效 | 保留设备和删除声明；拍摄动作数量不决定删除能力。 |
| 多个逻辑设备使用同一个 driver_id | 分别按 device_id 导出和匹配，不要求 driver_id 唯一。 |

旧 JSON 的兼容与当前 Python 静态定义的完整性分开处理。当前导出不能用可选构造器掩盖缺失声明；Catalog 不通过运行端口探测或临时查全局运行登记获取值。

重点审查五个接缝，并在相应任务验证：

1. 静态／运行声明漂移与 Catalog 两次重建丢字段：C1、C2。
2. 任意驱动标识、重复 driver_id、空拍摄目录被错误按型号或动作数量判断：C2、C4。
3. 字段缺失、有效空目录、首次加载失败和失败后保留旧目录被混淆：C2、C4。
4. 隐藏新增选项导致旧清理动作被标成非法、分支丢失或原请求改写：C4。
5. 源码说明正确，安装资源、HTTP、表单或设备指南未消费同一份定义：C3、C5。

## C1：将所有驱动的声明传入静态目录

**预计文件：** `apps/camctl/src/camctl/devices/catalog.py`、`definitions_runtime.py`、`drivers/registry.py`、ADB `registration.py`，Hasselblad `definitions.py`、`driver.py`、`registration.py`；必要时新增 Hasselblad 纯声明模块。更新真实构造点及演示／测试夹具。

下文 Python 源码简称相对 `apps/camctl/src/camctl`，组件测试简称相对 `apps/camctl/tests`；客户端测试简称相对 `apps/client`。根 `tests/integration` 用完整路径说明。

**接口建议：** `DriverDefinition(driver_id, actions, declaration: DriverDeclaration)` 要求明确声明。ADB 从 `CameraContract.declaration` 派生；Hasselblad 用一个不访问设备的声明函数或不可变值供静态与运行入口共用。Catalog 的候选过滤、部署任务绑定使用 `replace(definition, actions=...)` 保留声明。这里的同源指一个权威计算及相同值，不要求每次构造具有同一 Python 对象身份。

- [ ] **步骤 1：写失败测试。** 在 `apps/camctl/tests/unit/devices/test_catalog.py`、`test_definitions_runtime.py` 与 integration/devices 的 `test_adb_camera_registration.py`、Hasselblad 注册用例中，覆盖声明缺失／非法／冲突、两次 Catalog 转换、部署任务绑定、无拍摄能力和重复登记。静态值与实际运行登记比较；合成驱动分别给出明确 true 和 false。
- [ ] **步骤 2：运行最窄目标确认失败。** 单元与组件集成分进程执行，失败来自缺少声明传递或一致性检查。
- [ ] **步骤 3：实施同源传递。** 所有当前生产定义及被配置引用的部署适配提供完整声明；登记批次先完成身份、重复与声明检查，再修改登记，避免留下部分结果。受理与 describe 仍共用 Catalog；定义错误按配置／内部错误处理。
- [ ] **步骤 4：验证并提交。** 覆盖真实内置登记、`kind` 拍摄过滤、`deployment_task_factory` 绑定及 `apps/camctl/tests/integration/acceptance/test_adb_camera_definitions.py`。字段不能因没有录像工厂或 `actions: []` 丢失；不增加设备 I/O。

## C2：扩展公共删除声明并接通真实加载

**预计文件：** 根 `protocol/schemas/capabilities.schema.json`、能力样例、`protocol/README.md`、`docs/architecture/capabilities.md`、`camera-capabilities.md`；`Catalog.describe_document`、客户端 `src/shared/types.ts`，按需要调整 `shared/capabilities.ts` 与 HTTP 传递。包资源由现有生成器同步。

**接口：** 消费 C1 的必需静态声明，产出公共可选布尔字段；客户端只从根协议执行整份校验。当前生产导出逐设备有值；旧文件可以缺字段。

- [ ] **步骤 1：写结构和加载失败测试。** 实际 `loadCapabilities` 接受 true、false、旧缺字段，拒绝 null／非布尔／未知外层属性；原始 JSON 的重复键通过实际重载的 `parseJson → loadCapabilities` 链拒绝。夹具使用任意 driver_id、共享 driver_id 的两个设备、`actions: []` 和有效空目录；清理仍不能放进拍摄 `actions[]` 或携带 `device_id`。
- [ ] **步骤 2：运行目标用例确认失败。** 扩展 `apps/client/tests/integration/video-estimate-capabilities.test.ts`、`http.test.ts` 及 Python Catalog 用例；不要用自建简化加载器替代生产路径。
- [ ] **步骤 3：实现协议与导出。** 公共值只读取 C1 的声明，缺静态定义使新导出失败。更新 Schema、样例、类型、责任文档和协议入口；保持预览、估算及精确数字原样传递。
- [ ] **步骤 4：验证重载与交接并提交。** `/api/state` 和重载返回保留字段；成功整体替换，失败保留旧 active 与错误，首次失败仍无 active。仅删除字段变化不改草稿／预设／原请求；预览字段同时变化时继续执行既有自动预览规则。运行 `node scripts/check-protocol.mjs`。

## C3：补齐全部内置正式任务的同源说明

**预计文件：** ADB `definitions.py`、`timelapse_parameters.py`、`contracts.py`、`osmo360ii_capture.py`，Hasselblad `definitions.py`，`docs/architecture/camera-parameters.md`。消费者为客户端 `Fields.tsx`、`Editor.tsx`、`DeviceGuide.tsx`。

**接口：** 原有 `ActionCapability.name/description` 与 Schema `title/description`。任务固定设置和启动后取消限制由与执行任务一致的权威定义提供，不新增公共取消字段，不依型号名称在客户端补文案。

- [ ] **步骤 1：写正式说明失败测试。** 覆盖 Action6 录像、OSMO 录像和原生延时、Hasselblad 单张。所有用户可填写字段及实际分支具有可读名称、含义和单位：秒、EV，固定白平衡用 K。包括 Action6 的分辨率、视野、防抖、可选光圈／码率和曝光成员，不只检查 OSMO。
- [ ] **步骤 2：运行 unit/devices 的 `test_adb_camera_parameters.py`、`test_osmo360ii_capture.py` 及 Hasselblad 能力用例确认失败。** 断言事实和结构，不逐字锁定人类文案。
- [ ] **步骤 3：实施任务与字段说明。** OSMO 录像说明全景、8K／30 fps、D-Log M、高码率请求；Manual 为 ISO 800、1/60 秒快门，两种曝光均请求 5200 K，Auto 为 EV 0。原生延时说明全景、8K／30 fps，只有 Manual 请求 ISO 800、1/60 秒和 5200 K。Action6 说明自身固定设置及省略光圈／码率时保持原设置；Hasselblad 说明单张 RAW 和沿用机身设置。请求设置、独立读回、假定参考码率分别表达。
- [ ] **步骤 4：补启动后取消说明。** 从实际任务停止及取消策略生成说明并核对工厂返回：Action6／OSMO 普通录像支持启动后取消；OSMO 有限原生延时沿原目标结束策略收场，不承诺立即停止；Hasselblad 启动前可取消、启动后不支持取消。不能只用驱动级 stop 布尔值替代所有参数类型的任务策略。
- [ ] **步骤 5：验证真实 UI 并提交。** 扩展 `apps/client/tests/integration/browser-camera-parameters.test.ts`、`browser-editing.test.ts`，加载正式导出，核对表单、任务说明与指南同源且查看不改草稿。保持参数域、命令、默认值、预览、估算与时间组合；参数专题将设备进度统一引用[验证记录](../../camctl/verification.md)和对应完成计划。

## C4：按通用声明控制页面入口

**前置：** C2 完成后，确定下表的展示策略。该选择影响主机文件及混合设备入口，当前尚未决定；不把它变成 C1—C3 或另一份文件清理计划的前置条件。

现有报告能关联来源动作的逻辑 device_id，但不能区分 `original/preview` 的设备／主机存储位置。`kind=repaired` 能识别主机修复成品。页面策略仅决定是否显示准备入口，不改变物理来源事实或主机执行资格。

| 入口 | 策略 A：按关联设备隐藏快捷入口 | 策略 B：存在任一明确 false 时统一隐藏 |
| --- | --- | --- |
| Editor 新动作下拉 | 保留独立通用清理动作。 | 从新候选中隐藏清理。 |
| Records 计划清理 | 保留；计划范围没有唯一设备上下文。 | 隐藏。 |
| ActionResult 动作清理 | 来源动作匹配的当前设备声明为 false 时隐藏。 | 隐藏。 |
| MediaResults 单产物清理 | repaired 保留；其他产物关联设备为 false 时隐藏，主机 original/preview 快捷入口也可能被隐藏。 | 全部隐藏，包括主机产物。 |
| 当前目录含 false，但当前来源／关联设备未知或缺字段 | 相关设备无法明确判 false 时保留该快捷入口，其他入口按上述规则。 | 四类入口仍统一隐藏。 |
| 当前目录没有明确 false，包括全为旧缺字段或有效空目录 | 保留相应原入口。 | 保留相应原入口。 |

两种策略都使用当前启用目录，适用于任意驱动；设备重新绑定后按当前关联作展示，不声称还原历史驱动能力。策略 B 优先判断整份 active 目录是否含 false，存在即统一隐藏，不受当前来源是否已知影响；它也会隐藏其他支持删除设备的快捷入口。

**预计文件与接口：** 前端新增纯可见性函数，消费 active 能力与页面已有来源关系；在 `device-options.ts`、`Editor.tsx`、`Records.tsx`、`ActionResult.tsx`、`MediaResults.tsx` 和上游 `App.tsx` 传入必要数据。`DeviceGuide.tsx` 展示 true／false／未知。`BuiltinFields.tsx` 与 `followup.ts` 保持既有参数编辑和追加职责，只有真实消费缺口才修改。

- [ ] **步骤 1：定稿上述矩阵并写失败测试。** 选择后只保留有效策略。`tests/unit/device-options.test.ts` 与真实浏览器覆盖四类入口、任意驱动、混合设备、来源缺失、repaired 和主机 original/preview 的实际影响；明确混合目录含 false 但当前来源未知或关联设备缺字段时的结果。至少有一个现有报告流程可达的 false 隐藏场景。
- [ ] **步骤 2：测试现有输入保留。** 新选择隐藏与旧动作合法性分别判断；已有清理动作继续正确显示、编辑、保存和按既有规则导出，不能仅因候选被隐藏标成“当前不可用”。停用分支仍可恢复已填写内容，JSON／导入／预设／原请求保持原值。
- [ ] **步骤 3：运行目标用例确认失败后实施过滤。** 仅在各入口生产者判断显示；追加流程和后端校验不新增能力阻止。指南展示的是声明，不表示在线、物理文件位置或操作成功。
- [ ] **步骤 4：验证重载并提交。** true↔false 成功重载即时更新显示；失败且有旧 active 沿用旧值并显示错误，首次失败为未知，有效空目录为明确空集合。核对 busy 状态继续遵守原页面操作锁，不因新增条件放开在途操作。检查旧草稿全部分支、未完成原文与已导出请求不变。

## C5：完成安装版声明到客户端的软件验收

**预计文件：** `apps/camctl/tests/integration/bootstrap/test_distribution.py`，根集成新增 `tests/integration/test_device_capability_declarations.py`，复用真实 CLI 与客户端测试入口；更新两组件验证记录。

**分段门禁：** 安装版 CLI、协议、实际加载、HTTP 和指南验收依赖 C1—C3，可以先行；最终页面可见性联合验收须等待 C4 定稿并实施。两段均不依赖文件清理计划，前段完成不能代替整份计划完成。

- [ ] **步骤 1：写联合失败测试。** 从仓库外安装 wheel，离线按真实配置执行 describe，与源码正式定义比较；先核对根 Schema、包内资源、实际 loader、HTTP 和设备指南。C4 完成后增加选定可见性策略的真实页面联合验收。包含全部内置驱动与任意驱动夹具，声明变化本身不产生拍摄或清理调用。
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

- [ ] 所有已配置驱动的新导出都有同源删除声明；当前定义缺失与旧公共 JSON 缺字段分别处理。
- [ ] 全部内置正式任务及实际可调字段说明完整，取消说明与任务执行策略一致，客户端不维护型号分支。
- [ ] C4 的展示策略已确定并通过真实可达入口验证；旧输入、原请求、主机与混合来源的显示行为符合定稿矩阵。
- [ ] 安装版 CLI、协议资源、HTTP、实际客户端加载和页面一致；未执行设备操作，证据不扩展为真机删除验收。
