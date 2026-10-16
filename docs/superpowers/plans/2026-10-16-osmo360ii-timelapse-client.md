# Action6 与 OSMO 360 II 延时客户端执行计划

> **执行者要求：** 使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans` 实施；先读[统筹计划](2026-10-16-osmo360ii-timelapse.md)。本领域的数字校验与表单资格必须分别验证。

**目标：** 消费两款相机从共同时间定义生成的标准 Schema，提供时间参数关联选择，保留各型号独立的曝光、输出编辑、原始数值和编辑内容，并使浏览器与后端作出相同完整校验结论。

**架构：** 保留现有整份有限目录优先路径；其不能生成时，只对受限标准根合取结构作可证明的分解，产生一个有限关联组及其余独立字段。候选仍交现有校验器过滤，不实现设备专用合法性规则。

**技术栈：** TypeScript、React、Ajv 2020、现有精确 JSON 适配、Vitest、Playwright；版本遵守客户端声明与锁文件。

**规格：** [参数选项联动](../../architecture/client-editing.md#参数选项联动)、[曝光对象编辑](../../architecture/client-editing.md#对象参数级联编辑)、[草稿与原请求](../../client/storage-and-recovery.md#草稿预设与原请求的保存)、[能力加载](../../architecture/client-deployment.md)、[同源 Schema](../../architecture/camera-capabilities.md#拍摄参数的-json-schema)。

## 全局约束

遵守[统筹计划](2026-10-16-osmo360ii-timelapse.md#全局约束)。客户端不保存第二份设备枚举，不使用私有 Schema 关键字，不新增 OSMO 专用后端入口。派生选项只证明其实际覆盖范围可补全；完整参数是否合法始终由原完整 Schema 判断。

## 当前上下文与建议文件

- `apps/client/src/shared/validation.ts`、`json.ts`：精确整数、范围及步进已有原词元适配；`enum` / `const` 仍存在浮点投影相等的问题。
- `src/web/parameter-options.ts`：`parameterOptions`、`compatibleValues`、`parameterChoiceBlocked`、`parameterRequired`；整份有限目录和独立分类尚未支持所需混合情况。
- `src/web/object-branches.ts`：`objectBranches` 依赖整份目录为独立字段，需改成核对当前对象的独立性证明。
- `src/web/Editor.tsx` 的 `SchemaFields`、`Fields.tsx`、`DeviceGuide.tsx`、`validation-presentation.ts` 消费同一派生分类，不能各自再分析条件。
- `src/server/application.ts` 的预设与导出校验、既有草稿模型及对象分支存储原则上复用；测试暴露实际缺口时才修改。
- 正式规格需同步 `docs/architecture/client-editing.md` 和 `docs/client/device-guide.md`，使局部组与整份目录的保证范围明确。

## C0 共享校验器的精确枚举与常量

**输入：** 现有精确 JSON 身份和父容器数字词元机制。

**交付接口：** 保留 `createValidator()`、标准 `enum` / `const` 及现有诊断结构。浏览器、Node.js 后端和公共协议校验继续复用同一入口。

2026-10-16 在 Node.js v24.20.0 上以只读探针核对：`parseJson` 保留了 `0.50000000000000001` 的原词元，但 `enum: [0.5, 1]` 和 `const: 0.5` 均判定通过；同一输入的 `maximum: 0.5` 则被精确范围适配拒绝。根因是前两项尚未使用精确身份，不能仅在选项显示层修补。

- [x] 新增 `tests/unit/numeric-equality.test.ts`。独立断言：`0.5`、`0.50`、`5e-1` 等价；`0.50000000000000001` 不等于半秒，`1.0000000000000001` 不等于一，`1e-999` 不等于零；数字、字符串、布尔和空值不混同。
- [x] 覆盖对象键序、数组顺序、嵌套数字、根标量显式 `numberToken` 上下文、对象属性、数组成员、本地引用和 `if` 中的 `const`。校验前后输入与 Schema 数值事实不变，`schema_enum` / `schema_const` 诊断保留精确允许值。
- [x] 从 `apps/client` 运行以下命令对应的测试范围，确认近邻错误接受及条件分支错误的目标失败。

```bash
pnpm exec vitest run tests/unit/numeric-equality.test.ts tests/unit/numeric-boundaries.test.ts tests/unit/numeric-multiples.test.ts
```

- [x] 在共享校验器统一接入精确身份比较，复用 `exactJsonValueIdentity`、父容器词元及既有根标量上下文；保持 JSON 值相等语义，不修改源 Schema 或增加另一套 UI 校验。现有 `hasNumericLoss` 仍可保守限制控件资格，不顺带扩大高精度控件范围。
- [x] 重跑上述命令及 `pnpm exec vitest run tests/unit` 对应的范围，核对其他公共协议消费者未退化；检查 diff 后提交。P3 在此项通过后验证跨语言一致性。

2026-10-16 在 Linux 开发容器、Node.js `v24.20.0`、锁定 Vitest `5.0.1` 上验证。现有数字边界和步进基线为 2 个文件、48 项通过；新增测试先得到 30 项目标失败，最终三个聚焦文件的 126 项及全部客户端单元 59 个文件、2798 项通过，`tsc --noEmit` 通过，退出码均为 0。依赖由本工作区的 `package-lock.json` 独立安装；预装 `pnpm exec` 会先触发额外安装，因此实际使用本工作区的 `node node_modules/vitest/vitest.mjs run …` 和 `node node_modules/typescript/bin/tsc --noEmit`。本项保留 Ajv 对空枚举的规则错误；关键字规则声明在全部精确数值适配后注册，避免提前编译并缓存浮点规则校验。此处证明客户端共享校验及单元范围，跨语言组合、浏览器交互和设备验收由后续任务分别验证。

## C1 受限根合取分解与查询接口

**输入：** C0；与 P1 对齐的标准 Schema 形状。可以先用通用合成规则开发，不依赖相机可执行能力。

**交付接口：** 保留 `parameterOptions(parameter)` 统一入口。结果要分别表达整份目录、一个关联组及其枚举/不可枚举状态、其余字段的独立性依据；不得把局部组冒充原 `finite` 整份目录。内部类型名与文件拆分属于建议。

### 明确支持的子集

完整有限目录仍先尝试，已有的引用及组合支持不收窄。只有其不能生成时，才尝试以下分解：

结构|分解资格
-|-
根对象|封闭的直接 `object`，包含 `properties`、`required`、`additionalProperties: false`、版本/说明元数据及可选根 `allOf`。
`allOf` 成员|只包含 `if`、`then`、可选 `else` 及说明元数据；不递归分析任意组合表达式。
条件叶节点|布尔 Schema，或只含对象类型、直接 `properties`、`required` 及说明元数据的对象。
条件涉及属性|名称必须对应根直接声明的属性；属性规则只使用现有局部标量断言子集。条件里的类型、必填和未覆盖关键词都参与资格判断，不能删掉后继续推导。
关联范围|所有条件实际引用的属性名称取并集，形成唯一关联组；不分析一般依赖图，也不按设备或字段名称判断语义。
条件集合不引用任何字段|不建立空成员的局部目录，沿原安全回退及完整规则诊断；不把恒假条件当成所有字段独立且可导出。
关联组候选|从根字段有限标量域及适用的缺省候选构造，先核对候选积不超过现有 4096 上限，再分配候选行。
其余属性|因条件不读取这些字段，使用其原有局部标量、对象级联或 JSON 编辑资格。

证明分解后，组 Schema 精确复制组内属性的原局部规则、适用根必填项及全部原条件。根结构约束、该组全部断言与其余字段自身规则共同满足，才等价于原完整 Schema。组 Schema 用 `createValidator()` 编译，客户端不另写时间比较。

分类结果|行为
-|-
根引用、作用域、其他根组合/依赖断言或未知根关键词，或条件含引用/嵌套结构等未覆盖语法|不能证明分解，沿原安全回退；不把未知断言当成无约束。
分解已证明，但组无有限域、候选超限或参与断言的数字事实不能可靠处理|该组不能提供有限候选；其余已证明独立的字段保留其合格控件，组字段使用原普通输入或 JSON 修正入口。
组可完整枚举，过滤后没有合法行|明确报告没有合法组合，不混同为不能推导。
Schema 无效、引用缺失或必要语义不可执行|按能力加载/规则错误处理，不能降级为普通表单回退。

- [x] 先在 `parameter-options.test.ts` 增加通用合成 Schema：两个改名的有限标量、一个独立对象分支及一个普通字符串，直接给出少量预期合法行。覆盖 `if/then/else`、条件必填、矛盾条件及多个条件共同过滤；不复制设备允许值表。
- [x] 覆盖每类不支持语法、无引用字段的条件集合、分解成功但无法枚举、空合法集合、4096 边界和超限前拒绝；验证可选缺省、空值、false、零、空字符串及精确数字身份，且不修改规则或输入。
- [x] 先运行 `pnpm exec vitest run tests/unit/parameter-options.test.ts` 观察失败，再实现受限分解。标量资格规则复用既有辅助逻辑；多个消费者不各自维护关键词完整清单。
- [x] 将候选、必填和 pending 查询改为按当前字段的证明范围消费：整份目录依赖完整参数，关联组依赖组成员，独立字段依赖自身。兼容值按其他已填组成员精确过滤，再投影当前字段。
- [x] 重跑同一单元文件，检查 diff、接口调用点及语法拒绝完整性后提交。

2026-10-16 在 Linux 开发容器、Node.js `v24.20.0`、锁定 Vitest `5.0.1` 上验证。参数选项基线 100 项通过；通用合成规则先得到 29 项目标失败，独立字段存在性和必填查询补充测试先得到 2 项目标失败。最终参数选项 166 项、全部客户端单元 59 个文件 2864 项及 `tsc --noEmit` 均通过，退出码 0。实际通过本工作树的 `node node_modules/vitest/vitest.mjs run …` 与 `node node_modules/typescript/bin/tsc --noEmit` 执行，未触发依赖安装。`grouped` 分类分别保留关联组的有限／不可枚举状态与其余直接字段的独立性证明；组候选使用全部原条件过滤，4096 候选积上限及精确数字身份沿既有实现。局部标量资格由同一辅助函数维护。`Editor` 只将两处 pending 查询改传统一分类；字段控件、对象分支及说明页的完整消费由 C2 接续。本项仅证明通用分类与查询的软件范围，不代表浏览器接通、生产相机参数或设备验收通过。

## C2 字段控件、曝光与说明页

**输入：** C1 的统一分类及查询接口；既有曝光编辑和单字段写入。

**交付接口：** `SchemaFields`、`Fields`、`objectBranches`、`DeviceGuide` 及错误呈现共用分类；对象分支只要求当前对象具有独立性证明。候选复制、再次核对和写入继续保留原数字词元。

输入或操作|目标行为
-|-
时间字段缺失，且没有相关 pending|显示可补全候选和未填写状态，不选择首项或默认值；完整参数仍不允许导出。
合法组合下选择一个字段|按组内另一字段双向过滤，只写用户操作的字段；不自动调整时长或间隔。
自身值非法或时间组合失效|保留原值及错误；计算当前字段候选时不把其旧值作为限制，非法原值不成为可选项。
其他组成员域外、类型错误或显式空值导致无兼容候选|保留全部输入并解释冲突，提供参数 JSON 修正入口；不提示用户执行界面不存在的必填字段清空操作。
时间组内有 pending|暂停时间组选择并保留原文，不使用旧投影；独立曝光仍按自己的资格操作。
独立曝光有 pending 或不完整|时间组仍可修正；曝光仍按原身份、成员阻塞和停用模式恢复规则处理，完整参数校验继续失败。
参数祖先或设备/动作/参数类型身份有 pending，或归属无法可靠解释|暂停依赖这些事实的选择，不使用旧身份猜测规则。
其他动作或不相关公共字段有 pending|不遮蔽当前组的合法修正机会，但完整计划导出继续检查全部事实。
曝光模式切换|使用原 `switchObjectBranch` / `replaceObjectValue`，只处理曝光及其模式资料；首次选择不补 ISO，延时 Auto 不带入普通录像 EV。
根额外字段、时间条件与当前曝光字段同时有错误|允许具备资格的局部修正，但完整诊断同时保留三类错误；只抑制其他曝光模式的无关分支错误。
能力重载、查看说明、切换视图或折叠|重新派生分类但不改原值、pending 或分支资料；纯查看不产生草稿保存版本。

- [ ] 先扩展 `object-branch-controls.test.ts`、`object-branch-validation.test.ts`、`fields.test.ts`，并建议新增 `tests/integration/browser-linked-parameters.test.ts`。红灯应来自条件与独立曝光共存时控件缺失或范围错误，不能是夹具加载失败。
- [ ] 同步[客户端编辑规格](../../architecture/client-editing.md#参数选项联动)：整份目录按完整参数补全，局部关联组只证明组内全部断言可补全；说明独立字段非法时仍可修正该组。对象级联、pending 范围及设备说明职责一起更新。
- [ ] 接通各消费者及 `validation-presentation.ts`：增加根额外字段错误、时间条件错误及当前曝光分支错误同时存在的回归，完整诊断均保留；不泄漏其他模式必填错误，不因局部组可编辑而吞掉根或时间错误。
- [ ] 说明页仅展示实际组成员与时间组合数量；沿现有展示上限，不把局部时间组合数量描述为整份任务组合数量，不铺开大型组合表。由现有字段标签解释关联，不向产品用户展示内部目录类型。
- [ ] 从客户端目录顺序运行以下命令。验证参数 JSON 明确修正后可恢复选择、选择只改一个字段、曝光往返恢复和 pending 保留，以及查看前后完整草稿与版本不变。

```bash
pnpm exec vitest run tests/unit/parameter-options.test.ts tests/unit/object-branch-controls.test.ts tests/unit/object-branch-validation.test.ts tests/unit/fields.test.ts
pnpm run build
pnpm exec vitest run tests/integration/browser-linked-parameters.test.ts tests/integration/browser-camera-parameters.test.ts tests/integration/browser-rule-display.test.ts
```

- [ ] 检查 diff、统一分类消费及数值身份，复核后提交。

## C3 保存恢复、完整校验及 P/C 接缝

**前置：** C0—C2、P1/P2；P3 的根接缝测试共同消费双方交付。

**交付接口：** 草稿、预设、固定请求和服务端 `validateParams` 沿原模型。关联组及候选不成为新增持久化数据，后端仍校验原完整 Schema。

- [ ] 先扩展 `tests/integration/parameter-variants.test.ts` 和新联动浏览器文件：真实 HTTP、SQLite 及重启后，最短参数保持数值；非法草稿可以保存和重开，非法预设不能覆盖原记录，固定请求正文不随能力更新而改变。
- [ ] 对两款覆盖能力收窄/移除、两个曝光模式、组内与组外 pending 及应用预设的整体替换；Action6 另覆盖三种输出选择及不向 OSMO 扩散。新规则下的旧 Action6 `5400/6000` 秒草稿和预设保留原值并显示错误，不自动替换为合法时长；旧固定请求正文不改写。时间组可修正不能绕过后端预设和导出校验。普通录像保持自身时长规则，Hasselblad 不新增时间字段。
- [ ] 运行相关客户端集成目标确认实际接缝失败，再只修复暴露的保存/消费问题；不重写现有分支存储或请求协议。回归同一集成目标。
- [ ] 与 P3 共同运行 `P tests/integration/test_timelapse_time_parameters.py -q`：两款生产候选 Schema 分别经真实客户端加载和后端校验，各时间对按独立精确数学比较核对，每款合法数均为 142；再覆盖各自曝光与输出结构、错误类型、等价词元和邻近非等值。TypeScript 不手写完整设备域，不按型号加入专用时间判断。
- [ ] 从客户端目录执行 `pnpm run typecheck`、`pnpm test`，公共共享校验器消费者全部回归。检查 diff、记录实际环境和退出码后提交；正式安装版 `describe` 的整链接口交由 E2 重验。

## 审查重点与完成条件

C0 关闭数学相等缺口；C1 证明分解及拒绝边界；C2 保留关联控件、独立曝光和原值；C3 闭合保存、导出和跨语言判定。参数仍未进入生产能力时，客户端软件验收必须注明测试合同范围，不将其解释为相机接入通过。
