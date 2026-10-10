# 拍摄视频大小估算实施计划

> **执行 Agent：** 使用 `superpowers:subagent-driven-development`（建议）或 `superpowers:executing-plans` 逐任务实施。执行者须实际读取本计划、批准设计、根 `AGENTS.md` 及修改目录内适用的规则；使用复选框记录进度。

**目标：** 客户端根据驱动导出的参考码率和当前拍摄参数，显示普通录像与延时摄影最终视频的大致大小。

**架构：** 根能力 Schema 定义可选估算元数据，Python 从驱动参数类型的同一份定义导出，客户端通过现有整份加载流程启用。客户端以纯函数完成作用域选择、参数校验、路径取值和计算；编辑适配层处理未完成输入与能力观察，展示组件只读取派生结果。

**技术栈：** Python 3.11、现有 `jsonschema` 与 `Decimal`；Node.js 24、TypeScript、Ajv Draft 2020-12、React、Vitest、Playwright。复用现有 JSON 解析、词元保留和精确整数判定，不增加依赖。

**规格：** [拍摄视频大小估算设计](../specs/2026-10-10-video-size-estimate-design.md)。字段含义、资格判定顺序和计算规则以该文件为准。

## 全局约束

- 估算用于查看大致大小，覆盖目标拍摄视频及延时摄影最终视频；额外预览视频和独立照片不计入。
- Mbps 为每秒百万比特；`1 MB = 1,000,000 字节`，`1 GB = 1,000 MB`。显示前不舍入，不对采集比例取整，不追加统一开销。
- 缺少整个 `video_size_estimate` 是有效能力说明。已经出现的非法声明使整份导出或加载失败，不删除字段后继续启用。
- 查表允许只覆盖部分合法选项；合法选项没有表项时无法估算，仍按既有规则允许导出。引用当前省略的可选参数时不补 Schema `default`。
- 公共机器格式只在根 `protocol/schemas` 维护，共同交接样例放在根 `protocol/examples`。参数 Schema、唯一性、预览声明和候选能力过滤继续执行既有契约。
- 数值只接受符合契约的 JSON 数字，不转换字符串或布尔值。`frames` 按原始数学值判定整数，不接受浮点舍入后才成为整数的输入。
- 估算不写入计划、`effective_params`、报告或客户端持久化资料，不触发草稿保存，不改变自动预览开关、资料和派生动作。已有用户编辑、保存和能力协调照常执行。
- 重载正在进行或结果尚待核实时撤下估算数值。请求结束后的新状态观察成功返回时，根据实际启用说明和当前内容恢复；观察失败继续暂停。
- Python 验证使用部署版本 3.11。单元测试隔离真实文件、网络、数据库、子进程和用户全局状态；涉及真实协作者的测试放在集成层。Python 组件集成目录分别前台、独占、顺序运行。
- 硬件码率统计口径、有效档位和成片关系仍需设备接入核实。本计划使用明确虚构的驱动与能力资料验收，不为真实候选驱动填入未经核实的数字。
- 各步骤的文件划分、类型、辅助函数和提交拆分是实现建议。实施者可以依据实际数据流调整，但须同步更新调用者、测试和计划中的衔接说明；行为契约、失败语义和验收条件必须保持一致。

## 评审重点

1. **原始数字被复制或 HTTP 交接舍入：** `1.00000000000000000001` 不得作为整数帧数启用；能力克隆和参数克隆保留词元。任务 2、3、4、5分别验证 Python、加载器、浏览器和真实交接。
2. **相关祖先与无法解释的未完成路径：** 根、动作列表、当前动作、选择字段及全部当前 `params` 的未完成输入撤下数值；其他动作和无关公共字段不影响当前估算。任务 4 验证全部分区。
3. **合法参数缺少估算依据：** 查表未覆盖、可选引用省略与非法参数具有不同原因；不选择替代档位，不补默认值，不增加导出门禁。任务 3、5验证。
4. **重载与状态观察次序：** 重载前的观察、保存阶段的观察和请求结束前发出的观察不能解除估算暂停；新的完整观察整体替换依据，失败文件不能混入旧目录。任务 4验证。
5. **路径作用域与展示副作用：** 同名参数类型按设备及动作区分；转义、空成员名和数组位置按 JSON Pointer 解释；只读自身成员。展示不保存、不改变预览或历史事实。任务 3、4、5验证。

## 执行基线与依赖

编制日期为 2026-10-10，工作区为 `C:\Users\84580\workspace\cld\camctl`，分支为 `codex/client-spec-sync`，代码基线为 `8b7de5660d71d86b02d465eb3c766753097ea9f0`。执行时重新观察实际工作区，不以该提交号替代当前文件。

客户端界面交互已在本功能实施前独立提交为 `47ebddd`；2026-10-10 的完整验证为 1628 项通过，见[界面交互验证](../../client/verification.md#2026-10-10-界面交互验证)。该提交后的界面、表单、校验、样式、浏览器测试和文档是本功能的实施基线，执行时结合实际文件继续。

2026-10-10 已核对 Node.js `v24.16.0`、pnpm `11.25.0`、uv `0.12.3` 和 `apps/camctl/.venv311/Scripts/python.exe` 的 Python `3.11.15`。实施前运行以下命令复核；环境不符时按部署规格调整环境，不能改用系统解释器扩大验证范围。

```powershell
# 工作目录：仓库根。
git status --short
git branch --show-current
node --version
pnpm --version
uv --version
& apps/camctl/.venv311/Scripts/python.exe --version
$env:UV_PROJECT_ENVIRONMENT = Join-Path (Get-Location) 'apps/camctl/.venv311'
```

任务依赖为 `1 → {2, 3} → 4 → 5`。任务 2和3在任务 1的格式门禁通过后可以分别实施；任务 4依赖任务 3的输出及整份加载契约，任务 5检查真实双方组合。

每个任务按“先写能证伪契约的测试 → 确认预期失败 → 实现 → 针对性验证 → 独立评审 → 提交”的次序推进。失败必须归因于新增契约尚未实现，而不是导入拼写或环境错误。提交前只暂存本任务变更，检查 `git diff --cached --check` 和暂存内容；不推送远程。

## 实施分工

客户端在 Windows 开发环境中实施，负责任务 3、任务 4，以及任务 5 中的客户端文档、客户端完整门禁和客户端横切评审。公共格式任务 1 已完成，提交为 `9c869208aa2b7cf54136a458c6cab4d67ea4abb5`。

Python 同源导出与跨组件闭环由 Linux 开发环境中的 Agent 实施，负责任务 2，以及任务 5 中的 Python 部署替身、真实 CLI 交接、受理、执行、报告和对应文档门禁。Python 使用部署版本 3.11，并遵守测试目录的独占、顺序运行规则。客户端完成状态与跨组件完成状态分别记录。

客户端任务 3、任务 4及任务 5中的客户端责任文档已经完成独立评审，客户端整体评审通过。客户端完整门禁在 `VITEST_MAX_WORKERS=2` 的命令环境下通过，实际结果与并发范围见[客户端验证记录](../../client/verification.md#2026-10-10-视频大小估算验证)。任务 2及任务 5中的 Linux 实施、部署和跨组件验收继续保留待办。

## 文件责任与建议接口

| 责任 | 现有入口与建议文件 | 提供给后续任务的结果 |
| --- | --- | --- |
| 公共格式和共同输入 | `protocol/schemas/capabilities.schema.json`；新增 `protocol/examples/video-size-estimate/`；`scripts/check-protocol.mjs` | 合法元数据结构、完整虚构能力文件、带原始 JSON 文本的正反例清单。 |
| 驱动同源导出 | `apps/camctl/src/camctl/devices/catalog.py`；现有 CLI、资源与包测试 | `ActionCapability` 可携带估算声明；`describe` 只在整份有效时输出。 |
| 客户端格式、引用和计算 | `apps/client/src/shared/types.ts`、`capabilities.ts`、`protocol-validation.ts`；建议新增 `json-pointer.ts`、`video-size-estimate.ts` | 已整体校验的能力目录；纯路径读取与纯估算结果。 |
| 编辑状态和展示 | `apps/client/src/web/video-estimate-state.ts`、`state-observations.ts`、`VideoSizeEstimate.tsx`；`Editor.tsx`、`App.tsx`、`api.ts`、`style.css` | 从完整草稿和同次能力观察推导的展示状态；全部完整状态读取共用发起资格和接纳次序，业务调用者保留自身原观察；表单与 JSON 共用。 |
| 端到端交接 | 根 `tests/integration` 的 Python 部署替身和客户端驱动 | 真实 describe、客户端加载/计算/导出、CLI 受理和报告消费闭环。 |

下面的类型是协调任务 3和4的建议，不另立机器协议。`CapabilityState` 复用 `src/shared/automatic-previews.ts`，`DraftContent` 复用 `src/server/models.ts`。

```ts
// 建议放在 src/shared/types.ts；运行时校验仍来自根 Schema。
export type EstimateQuantity =
  | { source: "constant"; value: number }
  | { source: "parameter"; path: string }
  | { source: "lookup"; path: string; values: Record<string, number> };
export type VideoSizeEstimateDefinition = {
  bitrate_mbps:
    | number
    | { from: string }
    | { by: string; values: Record<string, number> };
  duration:
    | { method: "direct"; seconds: EstimateQuantity }
    | { method: "timelapse_frames"; frames: EstimateQuantity;
        playback_fps: EstimateQuantity }
    | { method: "timelapse_interval"; capture_seconds: EstimateQuantity;
        interval_seconds: EstimateQuantity; playback_fps: EstimateQuantity };
};
// ParameterType 新增可选 video_size_estimate?: VideoSizeEstimateDefinition。

export type EstimateReason =
  | "capabilities_unavailable" | "rules_updating" | "reload_unconfirmed"
  | "input_unfinished" | "pending_scope_unknown" | "selection_invalid"
  | "not_provided" | "params_invalid" | "value_missing"
  | "value_invalid" | "lookup_missing" | "calculation_invalid";
export type VideoEstimate =
  | { kind: "hidden" }
  | { kind: "unavailable"; reason: EstimateReason;
      path?: string; label?: string; diagnostic?: string; warning?: string }
  | { kind: "ready"; sizeBytes: number; playbackSeconds: number;
      bitrateMbps: number; warning?: string };

// shared/json-pointer.ts；found=false 与显式 null 等值分开。
export type PointerRead =
  | { found: false }
  | { found: true; parent: object; key: string; value: unknown };
export declare function readJsonPointer(root: unknown, pointer: string): PointerRead;

// shared/video-size-estimate.ts；capabilities 已通过 loadCapabilities。
export declare function estimateVideoAction(
  action: unknown, capabilities: Capabilities | null,
): VideoEstimate;
export declare function formatVideoSize(sizeBytes: number): string;

// web/video-estimate-state.ts。
export type EstimateReloadPhase = "idle" | "updating" | "unconfirmed";
export declare function estimateDraftAction(
  content: DraftContent, actionIndex: number,
  capabilityState: CapabilityState, reloadPhase: EstimateReloadPhase,
): VideoEstimate;
```

以上签名用于说明责任，不要求把“原因”和文案混在计算层。计算返回稳定原因及实际路径；展示层优先采用 Schema `title`，无法明确取得标题时仍保留路径，不推测字段单位。

## 任务 1：公共能力格式、共同样例及规格检查

**输入：** 批准设计的三种码率形式、三种时长方法、数值来源和路径规则。

**产出：** 现有能力文件仍有效；新字段的合法和非法分区具有单一机器定义。所有后续组件可以读取同一组完整交接文本。

**建议文件：** 修改 `protocol/schemas/capabilities.schema.json`、`scripts/check-protocol.mjs`、`scripts/check-protocol.test.mjs`、`protocol/README.md`、`docs/architecture/capabilities.md`；新增 `protocol/examples/video-size-estimate/capabilities.json`、`cases.json`、`README.md`。

- [x] 在 `cases.json` 保存数组条目，结构为 `{ "name": string, "json": string, "schema_valid": boolean, "encode_valid": boolean, "load_valid": boolean }`。`json` 为完整能力文档的原始文本，保留精确小数、指数和重复键反例；预期从规格独立给出。`schema_valid` 指普通解析后的外层结构检查；`encode_valid` 指 Python 精确解析与整份编码检查；`load_valid` 指客户端精确解析、外层结构和全部加载语义检查。合法整份演示文件包含批准设计中的录像与延时参数类型，并另外覆盖码率 `from` 和 `timelapse_frames`；所有数字明确用于虚构演示。
- [x] 增加结构规格测试，并先运行以确认新字段在现有 Schema 下被拒绝。结构测试只断言 `schema_valid`；任务 2检查 `encode_valid`，任务 3检查 `load_valid`。参数 Schema 的关联与唯一性在 Python 的 Catalog 导出链检查，在客户端加载链检查，不能把任意外部文档通过 Python 编码函数等同于经过 Catalog。不要把结构检查脚本的通过称为生产加载通过。

```js
// scripts/check-protocol.mjs 中已登记全部根 Schema 的 ajv 可直接使用。
const estimateCases = await json(join(examples, 'video-size-estimate/cases.json'));
for (const entry of estimateCases) {
  let valid = false;
  try {
    valid = Boolean(ajv.getSchema('capabilities.schema.json')(JSON.parse(entry.json)));
  } catch { /* 文本无法解析时，结构判定为无效；测试条目保留原文。 */ }
  assert.equal(valid, entry.schema_valid, `${entry.name}: 能力结构`);
}
```

工作目录为仓库根，失败命令为 `node scripts/check-protocol.mjs`；新增合法条目的结构应失败，已有报告摘要和计划样例不能失败。

- [x] 在既有能力 Schema 的 `$defs` 中定义格式。下面的定义分别表达三个数量来源和三种时长方法，每个对象均填写精确的 `required` 与 `additionalProperties: false`。`frames` 分支中的常量和查表成员引用正整数定义，其他位置引用正数定义。

```json
{
  "positive_number": { "type": "number", "exclusiveMinimum": 0 },
  "positive_integer": { "type": "integer", "exclusiveMinimum": 0 },
  "estimate_pointer": { "type": "string", "pattern": "^/(?:[^~]|~[01])*$" },
  "bitrate_mbps": {
    "oneOf": [
      { "$ref": "#/$defs/positive_number" },
      { "type": "object", "properties": {
          "from": { "$ref": "#/$defs/estimate_pointer" }
        }, "required": ["from"], "additionalProperties": false },
      { "type": "object", "properties": {
          "by": { "$ref": "#/$defs/estimate_pointer" },
          "values": { "type": "object", "minProperties": 1,
            "additionalProperties": { "$ref": "#/$defs/positive_number" } }
        }, "required": ["by", "values"], "additionalProperties": false }
    ]
  },
  "estimate_quantity": {
    "oneOf": [
      { "type": "object", "properties": {
          "source": { "const": "constant" },
          "value": { "$ref": "#/$defs/positive_number" }
        }, "required": ["source", "value"], "additionalProperties": false },
      { "type": "object", "properties": {
          "source": { "const": "parameter" },
          "path": { "$ref": "#/$defs/estimate_pointer" }
        }, "required": ["source", "path"], "additionalProperties": false },
      { "type": "object", "properties": {
          "source": { "const": "lookup" },
          "path": { "$ref": "#/$defs/estimate_pointer" },
          "values": { "type": "object", "minProperties": 1,
            "additionalProperties": { "$ref": "#/$defs/positive_number" } }
        }, "required": ["source", "path", "values"], "additionalProperties": false }
    ]
  },
  "estimate_frame_quantity": {
    "oneOf": [
      { "type": "object", "properties": {
          "source": { "const": "constant" },
          "value": { "$ref": "#/$defs/positive_integer" }
        }, "required": ["source", "value"], "additionalProperties": false },
      { "type": "object", "properties": {
          "source": { "const": "parameter" },
          "path": { "$ref": "#/$defs/estimate_pointer" }
        }, "required": ["source", "path"], "additionalProperties": false },
      { "type": "object", "properties": {
          "source": { "const": "lookup" },
          "path": { "$ref": "#/$defs/estimate_pointer" },
          "values": { "type": "object", "minProperties": 1,
            "additionalProperties": { "$ref": "#/$defs/positive_integer" } }
        }, "required": ["source", "path", "values"], "additionalProperties": false }
    ]
  },
  "estimate_duration": {
    "oneOf": [
      { "type": "object", "properties": {
          "method": { "const": "direct" },
          "seconds": { "$ref": "#/$defs/estimate_quantity" }
        }, "required": ["method", "seconds"], "additionalProperties": false },
      { "type": "object", "properties": {
          "method": { "const": "timelapse_frames" },
          "frames": { "$ref": "#/$defs/estimate_frame_quantity" },
          "playback_fps": { "$ref": "#/$defs/estimate_quantity" }
        }, "required": ["method", "frames", "playback_fps"], "additionalProperties": false },
      { "type": "object", "properties": {
          "method": { "const": "timelapse_interval" },
          "capture_seconds": { "$ref": "#/$defs/estimate_quantity" },
          "interval_seconds": { "$ref": "#/$defs/estimate_quantity" },
          "playback_fps": { "$ref": "#/$defs/estimate_quantity" }
        }, "required": ["method", "capture_seconds", "interval_seconds", "playback_fps"],
        "additionalProperties": false }
    ]
  },
  "video_size_estimate": {
    "type": "object", "properties": {
      "bitrate_mbps": { "$ref": "#/$defs/bitrate_mbps" },
      "duration": { "$ref": "#/$defs/estimate_duration" }
    }, "required": ["bitrate_mbps", "duration"], "additionalProperties": false
  }
}
```

`estimate_duration` 是本步骤新增的 `$defs`，完整分支如下，不接受组合以外的成员：

| `method` | 必填数值成员及所用定义 |
| --- | --- |
| `direct` | `seconds` 使用正数数量来源。 |
| `timelapse_frames` | `frames` 使用正整数数量来源；`playback_fps` 使用正数数量来源。 |
| `timelapse_interval` | `capture_seconds`、`interval_seconds`、`playback_fps` 均使用正数数量来源。 |

每个数量来源的三个分支分别为 `{source: "constant", value}`、`{source: "parameter", path}`、`{source: "lookup", path, values}`；`source` 使用 `const`，`path` 复用指针定义，查表为非空对象。参数引用的取值类型由任务 3在当前 `params` 上检查。不能要求路径命中 Schema `required`，不能要求查表覆盖全部合法选项。

- [x] 将字段作为 `parameter_type.properties` 的可选成员添加，不加入该对象的 `required`。在动作层限制只有 `camera_record` 和 `camera_timelapse` 可以声明；`camera_take_photo` 的各参数类型出现该字段时拒绝整份文档。保留既有公共 `$ref`、必填字段和空目录语义。

```json
{
  "if": { "properties": { "type": { "const": "camera_take_photo" } },
          "required": ["type"] },
  "then": { "properties": { "parameter_types": { "items": {
    "not": { "required": ["video_size_estimate"] }
  } } } }
}
```

- [x] 共同用例至少覆盖以下完整分区。所有出现的非法常量放入所有相关来源位置，不只验证固定码率。

| 分区 | 预期 |
| --- | --- |
| 字段完全省略；三种码率形式；三种时长方法；三种数量来源；整数 `240`、`240.0`、`2.4e2` | 有效；整数按数学值判断。 |
| `null`、空对象、少成员、多成员、混合形式、未知方法或来源、空查表 | 无效，整份拒绝。 |
| 常量或表值为字符串、布尔值、零、负数；小数帧数 `240.5` | 无效，整份拒绝。 |
| `frames` 原文为 `1.00000000000000000001` | 结构检查可能因浮点舍入通过，但真实编码和加载必须拒绝；记录 `schema_valid: true`、`encode_valid: false`、`load_valid: false`。 |
| 路径为 `""`、`"duration_s"`、`"#/duration_s"`、`"/a~2b"`、`"/a~"` | 无效，整份拒绝。 |
| 路径为 `"/"`、`"/a~1b/~0x/0"`；部分查表；合法路径引用省略的可选参数 | 声明有效，当前参数能否估算另判。 |
| 照片动作声明估算；重复对象成员；同一作用域重复标识 | 真实加载拒绝；不同设备或动作的同名类型有效。 |

- [x] 更新能力说明责任专题，定义可选字段、整份失败、用途和适用动作，并引用批准设计的公式与完整状态分类。其他专题只引用对应规则；共同样例 README 明确结构检查与真实加载的边界。
- [x] 从仓库根依次运行 `node scripts/check-protocol.mjs`、`node --test scripts/check-protocol.test.mjs`、`node scripts/check-doc-links.mjs`。预期结构正反例与已有协议检查通过，链接检查无新增缺失路径。
- [x] 独立评审 Schema 与共同预期的每个分区，提交本任务文件，建议提交信息为 `feat: 定义视频大小估算能力格式`。

## 任务 2：Python 同源导出和完整失败边界

**输入：** 任务 1的根 Schema 与共同原始文本。

**产出：** 驱动声明可选估算信息；`camctl describe` 保留数值并整体校验，估算信息不进入执行定义。

**建议文件：** 修改 `apps/camctl/src/camctl/devices/catalog.py`、`apps/camctl/tests/unit/devices/test_catalog.py`、`apps/camctl/tests/unit/bootstrap/test_command_output.py`、`apps/camctl/tests/integration/contracts/test_schemas.py`、`apps/camctl/tests/integration/devices/test_catalog.py`、`apps/camctl/tests/integration/bootstrap/test_command_output.py`、`docs/architecture/camera-capabilities.md`；新增纯构造辅助模块 `apps/camctl/tests/video_estimate_helpers.py`。发行验证复用 `apps/camctl/tests/integration/bootstrap/test_package.py`。

- [ ] 先给 `ActionCapability` 的导出行为增加单元测试。测试使用 `load_config`、`ConfigDefaults` 和内存 `DriverDefinitions`；参数 Schema 校验协作者使用受真实函数签名约束的替身。下列构造器放在 `tests/video_estimate_helpers.py`，按现有 `tests/media_helpers.py` 的导入方式供单元和集成测试复用；辅助模块自身不安装 mock。`estimate` 使用 `Decimal` 而非 Python `float`。

```python
from decimal import Decimal
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.devices.catalog import (
    ActionCapability, DriverDefinition, DriverDefinitions, build_catalog,
)

def record_catalog(estimate):
    def task_factory(params):
        raise AssertionError("能力导出不能构造设备执行任务")

    capability = ActionCapability(
        action_type="camera_record", parameter_type="estimate_record",
        name="演示录像", description="虚构任务，参考码率来自同源声明。",
        preview_supported=False, defaults={}, task_factory=task_factory,
        schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"type": {"const": "estimate_record"}},
            "required": ["type"], "additionalProperties": False,
        },
        video_size_estimate=estimate,
    )
    config = load_config({"devices": {
        "cam-1": {"kind": "camera", "driver": "estimate_demo"},
    }}, ConfigDefaults())
    return build_catalog(config, DriverDefinitions({
        "estimate_demo": DriverDefinition("estimate_demo", {
            "camera_record": (capability,),
        }),
    }))
```

```python
# tests/unit/devices/test_catalog.py 中的新增用例。
from decimal import Decimal
from video_estimate_helpers import record_catalog

def test_describe_exports_estimate_as_metadata(monkeypatch):
    from unittest.mock import create_autospec
    from camctl.devices import catalog as module
    monkeypatch.setattr(module, "validate_parameter_schema",
                       create_autospec(module.validate_parameter_schema))
    estimate = {"bitrate_mbps": Decimal("130.125"), "duration": {
        "method": "direct", "seconds": {"source": "constant", "value": 60},
    }}
    parameter = record_catalog(estimate).describe_document()[
        "devices"][0]["actions"][0]["parameter_types"][0]
    assert parameter["video_size_estimate"] == estimate
    assert "video_size_estimate" not in parameter["schema"]["properties"]
```

- [ ] 从仓库根运行上述单元文件，确认新增字段尚未实现导致预期失败：

```powershell
uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/devices/test_catalog.py -q
```

- [ ] 在 `ActionCapability` 最后追加可选声明，不插入已有位置参数之间。可用 `None` 表示内部未声明，或采用现有缺失哨兵；这是内部实现选择。导出已声明值时保留原类型和结构，未声明时省略整个字段；已经进入交接文档的显式 `null` 必须保留并拒绝。建议导出代码在现有参数条目生成处添加：

```python
# parameter 为同源 ActionCapability，entry 为该参数类型的导出条目。
if parameter.video_size_estimate is not None:
    entry["video_size_estimate"] = deepcopy(parameter.video_size_estimate)
```

不得把辅助元数据传给 `acceptance.ports.ParameterDefinition`、默认值应用、任务工厂或状态库。驱动开放参数才进入 `params`；固定码率和播放帧率留在声明中。真实驱动需要接入时，说明中的数值和估算元数据必须从同一份定义产生。

- [ ] 增加单元分支：字段未声明时省略；`parameter_definition()` 仍只含现有执行端口字段；无可调用 `task_factory` 的录像/延时候选继续被过滤；能力导出不调用任务工厂。保持 `Catalog` 的设备、动作及参数类型精确关联。
- [ ] 在组件集成层读取任务 1的共同文本，以 `parse_exact_json()` → `encode_describe_document()` 验证 `encode_valid`。同时以真实 Catalog 构造验证源定义的参数 Schema 关联、同作用域重复类型及整份导出失败；不要以编码函数替代这段语义检查。真实参数 Schema 编译和根包 Schema 不作 mock。另在内存文档中覆盖 `Decimal("NaN")`、`Decimal("Infinity")`、布尔值及 Python `float`，证明非法数值不会被转成合法 JSON。

```python
import pytest
from decimal import Decimal
from video_estimate_helpers import record_catalog
from camctl.cli import encode_describe_document
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.schemas import SchemaValidationError

def test_fractional_frames_cannot_be_rounded_into_integer():
    document = record_catalog({"bitrate_mbps": 130, "duration": {
        "method": "timelapse_frames",
        "frames": {"source": "constant", "value": Decimal("1.00000000000000000001")},
        "playback_fps": {"source": "constant", "value": 30},
    }}).describe_document()
    with pytest.raises(SchemaValidationError):
        encode_describe_document(document)

def test_describe_preserves_decimal_reference():
    estimate = {"bitrate_mbps": Decimal("130.125"), "duration": {
        "method": "direct", "seconds": {"source": "constant", "value": 60},
    }}
    parsed = parse_exact_json(encode_describe_document(
        record_catalog(estimate).describe_document()).decode("utf-8"))
    value = parsed["devices"][0]["actions"][0]["parameter_types"][0]["video_size_estimate"]
    assert value["bitrate_mbps"] == Decimal("130.125")
```

集成文件中的 `record_catalog` 使用纯构造辅助模块，不继承单元测试的 mock。CLI 单元测试复用现有校验失败边界，断言退出码为1、stdout 为空、stderr 有事实诊断；任务 5再验证非法真实声明走到同一结果。

- [ ] 复用现有 `schemas.py` 精确数值、`encode_json_value()` 和 `encode_describe_document()` 的整份校验。新增定义放在既有能力 Schema 时，无需新增资源映射；若文件划分调整出同级 Schema，须同时更新 `apps/camctl/scripts/sync_resources.py` 与 `contracts/schemas.py` 的资源注册。不得手改生成的 `_resources`。
- [ ] 在 `docs/architecture/camera-capabilities.md` 说明驱动的同源声明与候选过滤责任，并引用能力格式和估算设计，不复制字段清单。
- [ ] 从仓库根依次运行以下命令。前三个集成命令分别启动进程，不合并目录或并发运行。包测试需证明安装 wheel 中的根能力 Schema 与权威资源一致。

```powershell
uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/devices/test_catalog.py apps/camctl/tests/unit/bootstrap/test_command_output.py -q
uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/contracts -q
uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/devices -q
uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap/test_command_output.py apps/camctl/tests/integration/bootstrap/test_package.py -q
```

- [ ] 审计全部 `ActionCapability` 构造及导出入口，独立评审缺失/非法声明和执行端口边界，提交本任务变更，建议提交信息为 `feat: 从驱动定义导出视频估算说明`。

## 任务 3：客户端整份加载、精确引用和纯计算

**输入：** 任务 1的公共格式；现有 `parseJson`、`cloneClientJson`、`originalNumberToken`、`mathematicalInteger`、`createValidator` 和 `validateParams`。

**产出：** 客户端后端与浏览器可启用同一合法目录；纯估算器返回明确的有效或无法估算分区。

**建议文件：** 修改 `apps/client/src/shared/types.ts`、`capabilities.ts`、`protocol-validation.ts`；新增 `shared/json-pointer.ts`、`shared/video-size-estimate.ts`、`tests/unit/video-estimate-fixtures.ts`、`tests/unit/video-size-estimate.test.ts`、`tests/unit/json-pointer.test.ts`、`tests/integration/video-estimate-capabilities.test.ts`；复用或扩展 `tests/unit/protocol.test.ts` 中的既有加载回归及 `capture.test.ts` 中的拍摄回归。

- [x] 先用完整内存能力文档写单元测试，不在单元测试读取共同文件。下列测试构造器放在 `tests/unit/video-estimate-fixtures.ts` 并导出；它只为本函数提供输入，不维护另一份协议字段规则。

```ts
import { loadCapabilities } from "../../src/shared/capabilities";
import type { VideoSizeEstimateDefinition } from "../../src/shared/types";

export function capsFor(estimate: VideoSizeEstimateDefinition) {
  return loadCapabilities({ devices: [{ device_id: "cam-1", driver_id: "demo",
    actions: [{ type: "camera_record", parameter_types: [{
      type: "record", name: "演示录像", description: "虚构参数类型",
      preview_supported: false, video_size_estimate: estimate,
      schema: { $schema: "https://json-schema.org/draft/2020-12/schema",
        type: "object", properties: {
          type: { const: "record" },
          duration_s: { type: "number", exclusiveMinimum: 0 },
          bitrate_mode: { type: "string", enum: ["standard", "high"] },
        }, required: ["type"], additionalProperties: false },
    }] }],
  }] });
}
export const record = (params: Record<string, unknown>) => ({
  type: "camera_record", device_id: "cam-1", params: { type: "record", ...params },
});
```

```ts
// tests/unit/video-size-estimate.test.ts。
import { expect, it } from "vitest";
import { loadCapabilities } from "../../src/shared/capabilities";
import { parseJson } from "../../src/shared/json";
import { estimateVideoAction } from "../../src/shared/video-size-estimate";
import { capsFor, record } from "./video-estimate-fixtures";
it("录像依据参考码率和播放秒数返回十进制字节数", () => {
  const caps = capsFor({ bitrate_mbps: { by: "/bitrate_mode",
    values: { standard: 95, high: 130 } }, duration: {
    method: "direct", seconds: { source: "parameter", path: "/duration_s" },
  } });
  expect(estimateVideoAction(record({ bitrate_mode: "high", duration_s: 60 }), caps))
    .toMatchObject({ kind: "ready", sizeBytes: 975_000_000,
      playbackSeconds: 60, bitrateMbps: 130 });
});
it("合法选项未提供查表依据时返回 lookup_missing", () => {
  const caps = capsFor({ bitrate_mbps: { by: "/bitrate_mode",
    values: { standard: 95 } }, duration: {
    method: "direct", seconds: { source: "constant", value: 60 },
  } });
  expect(estimateVideoAction(record({ bitrate_mode: "high" }), caps))
    .toMatchObject({ kind: "unavailable", reason: "lookup_missing",
      path: "/bitrate_mode" });
});
it("能力复制保留非整数帧数的原始词元并整份拒绝", () => {
  const document = JSON.stringify(capsFor({ bitrate_mbps: 130, duration: {
    method: "timelapse_frames", frames: { source: "constant", value: 1 },
    playback_fps: { source: "constant", value: 30 },
  } })).replace('"value":1', '"value":1.00000000000000000001');
  expect(() => loadCapabilities(parseJson(document))).toThrow();
});
```

- [x] 工作目录为 `apps/client`，运行 `pnpm exec vitest run tests/unit/video-size-estimate.test.ts`，确认现有加载拒绝新字段或新增估算器尚未实现产生预期失败。
- [x] 注册根能力 Schema 到 `createProtocolValidator()`，使用该 Schema 执行完整外层结构校验，再做现有唯一性、参数 Schema 版本/引用/类型关联检查。移除被根 Schema 覆盖的手写外层完整字段清单；不要放宽其他字段。用 `cloneClientJson` 代替会丢词元的 `structuredClone`，或在克隆前完成等价精确校验并保留后续计算证据。

```ts
// protocol-validation.ts，先登记既有资源再编译能力入口。
import capabilitySchema from "../../../../protocol/schemas/capabilities.schema.json";
// createProtocolValidator 的注册过程新增：
ajv.addSchema(capabilitySchema, "capabilities.schema.json");

// capabilities.ts 的加载入口建议顺序：
const copy = cloneClientJson(value);
const validator = createProtocolValidator();
const check = validator.getSchema("capabilities.schema.json")!;
if (!check(copy)) throw new Error(validator.errorsText(check.errors));
// 随后沿 copy 的每个设备/动作/参数类型执行现有语义检查和 compile。
// 所有检查成功才返回 Capabilities，失败不返回部分结果。
```

复用现有精确 `integer` 关键字，因此常量与查表中的 `frames` 校验仍有原父容器。非法路径由根 Schema 拒绝；不把参数路径缺失或表覆盖不全作为能力定义错误。

- [x] 实现标准指针解码与自身成员读取。可以将 `web/editing.ts` 的解码放入共享边界后重新导出；若不移动，仍须复用同一解码规则。不要直接以现有 `valueAt` 读取数组，因为它会读到 `length`。建议读取核心如下，解码还须先拒绝空路径、片段和非法 `~` 转义：

```ts
// segments 为已经验证并解码的 string[]；root 与 pointer 来自函数输入。
let current: unknown = root;
for (const [index, key] of segments.entries()) {
  if (current === null || typeof current !== "object") return { found: false };
  if (Array.isArray(current) && (
    !/^(0|[1-9][0-9]*)$/.test(key) || !Number.isSafeInteger(Number(key)) ||
    Number(key) >= current.length
  )) return { found: false };
  if (!Object.hasOwn(current, key)) return { found: false };
  const value = Reflect.get(current, key);
  if (index === segments.length - 1)
    return { found: true, parent: current, key, value };
  current = value;
}
```

单元测试分别验证 `/`、`/a~1b/~0x/0`、对象的 `"01"` 成员、数组合法0及非法 `01`/`-`/`length`/越界/空洞、自身 `__proto__` 成员、原型成员不可读取。合法语法不要求当前值存在；缺失与显式 `null` 分开。

- [x] 实现 `estimateVideoAction`：先判断视频动作，再按设备/动作/参数类型精确选择；无元数据先返回 `not_provided`；随后 `validateParams` 校验完整当前 `params`，不注入默认值；最后读取码率和数量。查表只接受字符串并执行 `Object.hasOwn(values, option)`。不存在、类型非法、表项缺失和计算失败分别返回对应原因及参数路径。
- [x] 为三种方法实现计算。普通时长为 `seconds`，预计帧数方法为 `frames / playback_fps`，间隔方法为 `capture_seconds / interval_seconds / playback_fps`；后者不取整。检查每个必要数值及输出为正且有限；读取帧数时同时检查 `originalNumberToken(parent, key, value)` 和 `mathematicalInteger`，无词元时才使用已知内存数字的整数判定。显示格式化独立于计算，采用 MB/GB，极小正数可显示为 `<0.01 MB`，不能显示为 `0 MB`。

```ts
// 经引用与数值检查后才运行；seconds 和 mb 均必须通过正的有限数检查。
const mb = (bitrateMbps * playbackSeconds) / 8;
const sizeBytes = mb * 1_000_000;
if (![playbackSeconds, mb, sizeBytes].every(v => Number.isFinite(v) && v > 0))
  return { kind: "unavailable", reason: "calculation_invalid" };
return { kind: "ready", bitrateMbps, playbackSeconds, sizeBytes };
```

- [x] 补齐纯函数分区，不用被测函数生成期望：录像 `130 × 60 / 8 = 975 MB`；延时 `6000 / 25 / 30 = 8 秒、175 MB`；非整除 `10 / 3 / 30 = 1/9 秒`；帧数 `240 / 30 = 8 秒`；小数 Mbps；直接成片时长的延时；全部数量来源。覆盖完整参数 Schema 的条件分支、可选值省略且带 `default`、显式 `null`/零/空字符串、查表大小写/空白/原型名、同名类型跨设备/动作、有限输入计算溢出或下溢。结果无效时不能携带可渲染的旧数字。
- [x] 在 `tests/integration/video-estimate-capabilities.test.ts` 读取共同 `cases.json`，通过真实 `parseJson` → `loadCapabilities` 验证每个 `load_valid`；合法演示的真实参数校验与估算结果也须通过。无效条目必须拒绝整份目录，后端 `Application` 保留旧说明和诊断的行为复用既有整份重载规则。
- [x] 在 `apps/client` 运行以下命令，结构与旧功能回归一并通过：

```powershell
pnpm exec vitest run tests/unit/video-size-estimate.test.ts tests/unit/json-pointer.test.ts tests/unit/protocol.test.ts tests/unit/capture.test.ts
pnpm exec vitest run tests/integration/video-estimate-capabilities.test.ts tests/integration/protocol-examples.test.ts
pnpm run typecheck
```

- [x] 审计所有元数据读取点、复制点和共享路径调用者；独立评审精度、作用域和无默认值分区，提交本任务变更，建议提交信息为 `feat: 计算拍摄视频大小估算`。

## 任务 4：编辑状态、能力观察和界面展示

**输入：** 任务 3的 `VideoEstimate` 和纯估算器；完整 `DraftContent`；既有 `CapabilityState` 的同次观察。

**产出：** 表单和参数 JSON 模式显示相同估算；相关未完成输入和重载撤下旧数值；恢复后按实际当前输入重算。

**建议文件：** 新增 `apps/client/src/web/video-estimate-state.ts`、`VideoSizeEstimate.tsx`、`tests/unit/video-estimate-state.test.ts`、`tests/integration/browser-video-estimate.test.ts`；修改 `Editor.tsx`、`App.tsx`、`style.css`。共享路径抽取确有需要时修改 `editing.ts` 并保持已有调用语义；不借此重构整个编辑器。

- [x] 先把批准设计的有序表转成适配层测试。下面的决策表自上而下，后一行只在此前未命中时适用；非视频动作隐藏优先于其他状态，能力不可用优先于相关未完成输入。

| 条件 | 结果和下一步 |
| --- | --- |
| 当前动作是已知的非录像/延时动作 | `hidden`。 |
| 没有实际启用说明 | `capabilities_unavailable`，呈现既有诊断。 |
| 有旧启用说明，但重载阶段为 `updating` 或 `unconfirmed` | `rules_updating` 或 `reload_unconfirmed`，不展示数值。 |
| 未完成输入覆盖根、动作集合、当前动作、`device_id`、`type`、`params.type` 或当前 `params` 的任意成员 | `input_unfinished`，保留原文。 |
| 路径无法可靠解码或数组/动作位置无法判定影响范围 | `pending_scope_unknown`，保留原文。 |
| 选择无精确匹配、动作位置不存在或当前 `params` 不是对象 | `selection_invalid`。 |
| 选择匹配但整个估算对象缺失 | `not_provided`。 |
| 完整当前 `params` 不符合所选 Schema | `params_invalid`。 |
| 引用缺失、引用非法或选项无表项 | 使用纯估算器的具体原因和路径。 |
| 计算不产生正的有限结果 | `calculation_invalid`。 |
| 所有依据有效 | `ready`；若启用说明带加载错误，附既有“仍使用此前说明”提示。 |

无关公共字段的问题不单独阻止估算。未完成路径必须按解码后的段和实际结构分类，不能只做字符串前缀判断：对象键 `"01"` 与数组位置不同；`/actions/0/params2` 不属于 `/actions/0/params`；当前参数内任何未完成输入都阻止估算，即使没有直接被公式引用。

- [x] 在内存草稿测试中给出相关与无关输入的最小反例，先运行确认适配层尚未实现的失败。测试能力复用任务 3产出的 `tests/unit/video-estimate-fixtures.ts`，只读内存输入。

```ts
import type { DraftContent } from "../../src/server/models";
import { expect, it } from "vitest";
import type { CapabilityState } from "../../src/shared/automatic-previews";
import { estimateDraftAction } from "../../src/web/video-estimate-state";
import { capsFor, record } from "./video-estimate-fixtures";

// observed 为 capsFor 构造的有效能力观察，两个动作均可独立估算。
const content: DraftContent = { text: JSON.stringify({ actions: [
  record({ duration_s: 60 }), record({ duration_s: 30 }),
] }) };
const observed: CapabilityState = { active: capsFor({ bitrate_mbps: 130,
  duration: { method: "direct", seconds: { source: "parameter", path: "/duration_s" } },
}), error: null, generation: 1, version: "v1" };
it.each(["", "/actions", "/actions/0", "/actions/0/device_id",
  "/actions/0/type", "/actions/0/params", "/actions/0/params/type",
  "/actions/0/params/unrelated"])("相关未完成输入 %s 撤下数值", path => {
  const draft = { ...content, pending: { [path]: { kind: "json" as const, text: "{" } } };
  expect(estimateDraftAction(draft, 0, observed, "idle"))
    .toMatchObject({ kind: "unavailable", reason: "input_unfinished" });
});
it.each(["/name", "/actions/0/scheduled_at", "/actions/0/policy",
  "/actions/1/params"])("无关未完成输入 %s 不遮蔽估算", path => {
  const draft = { ...content, pending: { [path]: { kind: "json" as const, text: "{" } } };
  expect(estimateDraftAction(draft, 0, observed, "idle").kind).toBe("ready");
});
```

增加非法转义、非规范数组位置、已删除动作位置和无法解析正文分支，分别断言输入原文保持、状态确定且没有数值。组合条件验证优先级：无元数据与参数非法同时出现、能力不可用与 pending 同时出现、相关与无法解释路径同时出现、明确非视频动作与全局重载同时出现。

- [x] 工作目录 `apps/client`，运行 `pnpm exec vitest run tests/unit/video-estimate-state.test.ts`。随后实现只读适配层：从一次调用的内容和能力观察读取，不缓存上次成功结果，不调用 `edit`、`save`、预览协调或持久化。非法完整正文返回明确输入原因并保留原文，不抛错击穿页面。
- [x] 在 `App.tsx` 增加可触发渲染的估算重载阶段，传给 Editor；不能以现有 `busy` 代替，因为它覆盖所有操作，也不能只用不触发渲染的 `reloadPaused` ref。保持现有“结束已发观察 → 保存所有会话 → 发重载 → 新观察”的顺序。

| 事件 | 估算阶段 |
| --- | --- |
| 开始准备重载 | `updating`，先撤下旧数值。 |
| 从 `idle` 开始准备后失败，尚未发 POST | 回到 `idle`，按原观察与当前保留输入重算；保存错误照常呈现。 |
| 从 `unconfirmed` 再次准备后失败，尚未发新 POST | 保留前次未核实责任和请求结束事实，回到 `unconfirmed`；后续新发的合格观察仍可确认，保存错误照常呈现。 |
| POST 已结束且新的 `/state` 观察成功 | 接受完整观察，再进入 `idle`；按其 `active` 和 `error` 判定。 |
| POST 或之后的观察丢失，请求结果待核实 | `unconfirmed`，保留暂停。 |
| 待核实期间新的 `/state` 观察失败 | 保持 `unconfirmed`。 |
| 待核实期间请求结束后新发的 `/state` 观察成功 | 整体接受该观察，进入 `idle`。 |
| 重载前、保存期间或请求尚未结束时发出的观察返回 | 可以按既有流程协调，但不能解除估算暂停。 |

后端 `reloadCapabilities()` 和 `/state` 当前均同步执行；新观察确认的是实际启用快照，不为未知 POST 补造成功回执。失败不一定改变 `generation/version`，因此不能要求版本变化才恢复。最终客户端由 `state-observations.ts` 统一记录重载阶段、持续核实责任、请求结束事实、观察发起代次及成功接纳次序；`App.tsx` 的全部完整状态读取共用该边界，`api.ts` 的草稿读取从调用者原观察提取结果。较旧成功不覆盖较新成功，更晚失败不推进成功接纳次序，已经确认后不重新制造核实责任。目录、诊断及草稿始终来自同次成功观察，业务仍使用自身读取返回的原事实判定操作结果；没有新增服务器状态机或公共字段。以上模块划分记录最终实现，不将内部接口规定为正式协议。

- [x] 实现 `VideoSizeEstimate` 只读组件，参数为 `VideoEstimate`；在 CameraFields 参数表单与参数 JSON 的共同外层展示。`ready` 显示“预计视频大小”、约数、十进制单位、预计成片秒数、参考 Mbps 和近似说明；无法估算显示原因及相应 Schema 标题/路径。更改尺寸或编辑模式不触发保存。

```tsx
// 展示骨架；原因文案由结果事实生成，不暴露 duration/source 内部结构。
if (result.kind === "hidden") return null;
if (result.kind === "ready") return <div role="status">
  <div>预计视频大小：约 {formatVideoSize(result.sizeBytes)}</div>
  <div>预计成片时长：约 {result.playbackSeconds} 秒</div>
  <div>参考码率：{result.bitrateMbps} Mbps</div>
  <p>根据参考码率估算，实际大小可能有所变化。</p>
  {result.warning && <p>{result.warning}</p>}
</div>;
```

秒数和 Mbps 同样在显示时合理舍入，不改计算值。浏览器断言事实、结构和单位，不在多个层逐字绑定阅读文案。

- [x] 新建浏览器集成测试，复用 `browser-validation.test.ts` 的真实 Application、临时目录、HTTP 服务、RequestLifecycle 和 Playwright 装配模式，使用任务 1的虚构能力文件；临时文件只属于当前测试并在结束时关闭服务后清理。使用条件、事件或响应门同步，不用随机 sleep。以下完整装配与用例放在 `browser-video-estimate.test.ts`；`video-size-estimate` 定位展示区域，`video-size-value` 只在有效数值存在时出现。

```ts
import { beforeAll, afterAll, afterEach, it, expect } from "vitest";
import { chromium, expect as check, type Browser } from "@playwright/test";
import { mkdtempSync, rmSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";

let browser: Browser;
const clean: Array<() => Promise<void>> = [];
beforeAll(async () => { browser = await chromium.launch({ headless: true }); });
afterAll(async () => { await browser.close(); });
afterEach(async () => { for (const fn of clean.splice(0)) await fn(); });
async function setup(capabilityText: string) {
  const directory = mkdtempSync(join(tmpdir(), "camctl-video-estimate-"));
  writeFileSync(join(directory, "device-capabilities.json"), capabilityText);
  const app = new Application(directory), files = new Files(app);
  const requests = new RequestLifecycle();
  const server = createHttpApp(app, files, requests).listen(0, "127.0.0.1");
  await new Promise<void>(resolve => server.once("listening", resolve));
  const context = await browser.newContext(), page = await context.newPage();
  page.setDefaultTimeout(5000);
  const stop = createStop(
    () => new Promise<void>((resolve, reject) =>
      server.close(error => error ? reject(error) : resolve())),
    requests, () => files.idle(), () => app.store.close(),
  );
  clean.push(async () => {
    await context.close();
    await stop();
    rmSync(directory, { recursive: true, force: true });
  });
  await page.goto(`http://127.0.0.1:${(server.address() as { port: number }).port}`);
  await page.getByTestId("initialize-button").click();
  return { page, app, directory };
}
it("重载响应丢失时暂停，后续观察核实保留的旧说明后恢复", async () => {
  const text = readFileSync("../../protocol/examples/video-size-estimate/capabilities.json", "utf8");
  const document = JSON.parse(text);
  const device = document.devices[0];
  const action = device.actions.find((item: { type: string }) => item.type === "camera_record");
  const parameter = action.parameter_types.find((item: { type: string }) => item.type === "example_record");
  const { page, app, directory } = await setup(text);
  const draft = app.createDraft({ text: JSON.stringify({ name: "估算恢复", actions: [{
    name: "录像", type: "camera_record", device_id: device.device_id,
    params: { type: parameter.type, bitrate_mode: "high", duration_s: 60 },
  }] }) });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  const before = app.draft(draft.id);
  let stateUnavailable = false, backendHandled = false;
  await page.route("**/api/state", async route => {
    if (stateUnavailable) await route.abort(); else await route.continue();
  });
  parameter.video_size_estimate = null;
  writeFileSync(join(directory, "device-capabilities.json"), JSON.stringify(document));
  await page.route("**/api/capabilities/reload", async route => {
    await route.fetch();
    backendHandled = true;
    stateUnavailable = true;
    await route.abort();
  });
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await check.poll(() => backendHandled).toBe(true);
  await check(page.getByRole("button", { name: "重新加载能力说明" })).toBeEnabled();
  await page.getByTestId("nav-plans").click();
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  await check(page.getByTestId("video-size-estimate")).toContainText(/核实|更新/);
  stateUnavailable = false;
  await check(page.getByTestId("video-size-value")).toContainText("975");
  await check(page.getByTestId("video-size-estimate")).toContainText(/此前|旧说明/);
  expect(app.capabilities.error).toBeTruthy();
  expect(app.draft(draft.id)).toEqual(before);
});
```

- [x] 浏览器回归按以下入口分别验证：表单填写；参数 JSON 有效→无效→修正；应用预设；设备/参数类型/动作类型切换；动作插入与删除后的身份位置；草稿关闭重开；能力首次不可用、成功替换、失败保留旧说明、响应丢失后观察恢复。为重载前观察设置延迟门，证明晚到的旧观察不解除暂停。
- [x] 从 Browser 层拦截 `/api/drafts/:id` 写请求，并比较真实 `app.draft(id)` 的 `revision/content`：计算、显示、折叠和切换表单/JSON本身无新增写入，自动预览资料保持原事实。用户真实编辑和既有能力协调仍可按原规则写入，测试必须分别计数，不能把所有保存都禁止。
- [x] 通过真实 HTTP 发送带原始数字词元的能力和参数：非法小数帧常量不能启用；合法声明引用当前非整数帧参数时无法估算；修正为数学整数后恢复。审计 server JSON replacer 与 `api.ts` 的 `parseClientJson`，不在中途用 `response.json()` 或普通 `JSON.stringify()` 丢失证据。
- [x] 在 `apps/client` 运行以下命令：

```powershell
pnpm exec vitest run tests/unit/video-estimate-state.test.ts tests/unit/preview-reload.test.ts tests/unit/web-session.test.ts
pnpm run build
pnpm exec vitest run tests/integration/browser-video-estimate.test.ts tests/integration/browser-editing.test.ts tests/integration/browser-editor-identity.test.ts tests/integration/browser-previews.test.ts tests/integration/browser-validation.test.ts
```

- [x] 逐项审计决定表、事件表、所有刷新与切换入口及未完成输入路径；独立评审数值撤下、恢复和写入次数，提交本任务变更，建议提交信息为 `feat: 在拍摄参数编辑中展示视频大小估算`。

## 任务 5：真实交接闭环、文档和最终门禁

**输入：** 前四个任务的有效协议与组件行为。

**产出：** 软件集成证据证明驱动定义能够经过真实交接进入浏览器与导出链，而估算不改变受理、执行和报告事实。

**建议文件：** 新增 `tests/integration/test_video_size_estimate.py`；扩展 `tests/integration/camctl_fixtures.py`、`client_driver.ts`；修改 `tests/integration/README.md`、`docs/client/page-interactions.md`、`docs/client/verification.md`、`docs/architecture/client-editing.md`、`docs/superpowers/specs/2026-10-10-video-size-estimate-design.md` 的实施状态。

- [ ] 在根测试替身的驱动剧本中允许对录像和延时参数类型显式提供虚构估算声明，默认剧本继续省略。建议剧本以 `video_size_estimate_json` 映射动作类型到估算对象的原始 JSON 文本，避免现有普通剧本解析先把小数变为 `float`。在 `_stub_definition()` 中使用 `parse_exact_json` 解析对应文本后传给同源 `ActionCapability.video_size_estimate`，不改变其他剧本字段和执行任务。

```python
from camctl.contracts.json_values import parse_exact_json

# _stub_definition(spec) 内；两个变量分别交给对应 ActionCapability。
texts = spec.get("video_size_estimate_json", {})
record_estimate = (parse_exact_json(texts["camera_record"])
                   if "camera_record" in texts else None)
timelapse_estimate = (parse_exact_json(texts["camera_timelapse"])
                     if "camera_timelapse" in texts else None)
```

只改变替身静态定义，不改真实驱动能力、不改变任务工厂和完成判定。`camctl_fixtures.py` 现有 `Deployment.install_client_capabilities()` 已执行真实 describe 并写交接文件，应继续复用。这里的原文映射只是测试装配，不是新增公共协议字段。
- [ ] 在 `client_driver.ts` 增加仅用于集成测试的 `estimate` 命令，建议调用约定为 `estimate <client-store> <action-json-path> <output-path>`。用真实 Application 初始化后的 `app.capabilities.active`、真实 JSON 解析与任务 3的纯估算器计算，然后输出测试凭据；不新增产品 API 或落库字段。

```ts
// tests/integration/client_driver.ts 的新增导入。
import { parseJson, stringifyJson } from "../../apps/client/src/shared/json";
import { estimateVideoAction } from "../../apps/client/src/shared/video-size-estimate";
// 接入现有测试脚本命令分派；storeDirectory/actionFile/outputFile 来自 argv。
const app = new Application(storeDirectory);
try {
  const action = parseJson(readFileSync(actionFile, "utf8"));
  const result = estimateVideoAction(action, app.capabilities.active);
  writeFileSync(outputFile, stringifyJson(result));
} finally {
  app.store.close();
}
```

- [ ] 写真实跨组件测试，先确认 describe 尚无声明或 estimate 命令尚未接入导致预期失败。下列录像场景使用已有一秒替身任务和明确虚构的成片时长常量，避免把估算资料当作设备测量。所有实际执行仍由现有 CaptureTask 契约决定。

```python
import json
from camctl_fixtures import Deployment, future_schedule, stub_driver_spec, video_file

def test_estimate_survives_describe_and_stays_out_of_plan(tmp_path):
    deployment = Deployment(tmp_path)
    driver = stub_driver_spec({"1": [video_file("record-1")]})
    driver["video_size_estimate_json"] = {"camera_record": json.dumps({
        "bitrate_mbps": 130,
        "duration": {"method": "direct",
                     "seconds": {"source": "constant", "value": 1}},
    })}
    deployment.install_client_capabilities(driver)
    action = {"name": "record", "type": "camera_record", "device_id": "cam-1",
              "scheduled_at": future_schedule(3), "policy": {"max_delay_ms": 5000},
              "params": {"type": "video"}}
    action_file = tmp_path / "action.json"
    action_file.write_text(json.dumps(action), encoding="utf-8")
    result = deployment.run_client_driver(
        "estimate", str(tmp_path / "client-store"), str(action_file),
        output=tmp_path / "estimate.json")
    assert result["kind"] == "ready"
    assert result["sizeBytes"] == 16_250_000
    plan_path, receipt = deployment.export_plan_with_client({
        "name": "录像估算闭环", "actions": [action],
    })
    body = json.loads(plan_path.read_text(encoding="utf-8"))
    assert body["actions"][0]["params"] == {"type": "video"}
    assert set(body["actions"][0]) == set(action)
```

继续在同一场景使用 `deployment.camctl("init", ...)`、`submit`、`run` 和已有真实报告导入入口，断言生效参数仍为 `{ "type": "video" }`，报告产物大小仍为替身文件事实8192字节，而不是16,250,000估算字节；报告 Schema 不出现辅助估算字段。报告定位复用现有 ready 文件与摘要规则，不硬写报告文件名。

- [ ] 增加延时场景，以 `timelapse_interval` 的虚构元数据经相同真实 describe 与客户端路径取得结果。可以使用采集持续时间3秒、间隔1秒、播放帧率30、码率175 Mbps，独立期望为0.1秒、2,187,500字节；同源虚构 Schema 与任务工厂明确接受和执行这组输入，不能声明6000秒而仍执行固定3秒。估算依据与最终文件事实分别断言。还需验证声明完全缺失、合法但未覆盖的查表选项都能按原规则导出；非法真实声明使 CLI 退出1、stdout为空，并使新的客户端加载失败而保留已启用说明。根替身的参数 Schema 当前只接受 `type`；需要查表参数或采集参数的场景，必须在同一虚构定义中显式添加对应 Schema，不能仅放宽 `additionalProperties`。
- [ ] 经 `video_size_estimate_json` 的原文入口分别传入合法小数码率 `130.125`、合法帧数 `240.0`/`2.4e2` 及非法帧数 `1.00000000000000000001`。前者经真实 describe、客户端和 HTTP 仍保持正确判定；后者在 describe 退出1且 stdout为空，证明测试装配没有先舍入或清洗。
- [ ] 从仓库根执行 `uv run --project apps/camctl --group test --python 3.11 pytest tests/integration/test_video_size_estimate.py -q`。本文件不申请 `host_demo` 夹具，不连接设备，不要求 WSL/C 编译；真实组件、真实文件、子进程与数据库都属于此集成层。
- [x] 更新客户端交互专题和设计实施状态，说明位置、近似单位、无法估算原因与重载暂停。把正式字段规则保留在能力格式专题，把实施进度留在本计划；不把虚构参考值记为真实设备依据。验证记录注明实际执行日期、环境与版本、命令、范围及结果，并明确 Linux 组件的待验边界。
- [ ] 按以下顺序执行最终门禁；仅在新修改、失败或未解决问题出现时扩大或重复。Python 集成目录继续分别前台运行。

```powershell
# 工作目录：仓库根。
node scripts/check-protocol.mjs
node --test scripts/check-protocol.test.mjs
node scripts/check-doc-links.mjs
uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit -q
uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/contracts -q
uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/devices -q
uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap/test_command_output.py apps/camctl/tests/integration/bootstrap/test_package.py -q
uv run --project apps/camctl --group test --python 3.11 pytest tests/integration/test_video_size_estimate.py -q
```

```powershell
# 工作目录：apps/client。
pnpm run test:unit
pnpm run test:integration
```

`test:integration` 的前置脚本会执行 `build`。记录构建与测试各自结果；构建若仍有既有的大包体积警告，应如实记录，不能写为“无警告”。本门禁不验证真实设备码率、现场采集次数或目标主机性能。

- [ ] 完成一次横跨全部任务的独立评审：沿驱动权威定义 → describe 整体校验与包资源 → 客户端后端加载 → HTTP 词元交接 → 当前草稿/pending → 计算 → 展示 → 重载失败/观察恢复 → 导出 → 受理/报告逐段核对。审计已发现风险的所有同类入口，而不是只检查测试中的例子。
- [ ] 检查 `git diff --check`、暂存范围和所有复选框，提交本任务测试与文档，建议提交信息为 `test: 验证视频估算的跨组件交接`。只在所有验收条件满足后记录功能完成。

## 最终验收与实施边界

| 验收对象 | 可证伪的完成条件 |
| --- | --- |
| 公共格式 | 三种码率、三种方法和数值来源同源定义；旧能力文件仍合法；非法声明整份拒绝。 |
| Python 与发行物 | 同一驱动定义导出可选字段；精确数值保留；候选过滤不被估算字段改变；安装物资源可独立加载。 |
| 计算与引用 | 两个批准示例得到975 MB和175 MB；非整除比例不取整；路径不跨作用域、不读取原型、不补默认值。 |
| 编辑与恢复 | 全部状态及组合优先级有测试；有效→未完成/非法立即撤下数值；修正、重开和新观察后使用当前依据恢复。 |
| 重载 | 进行中和待核实均暂停；无旧说明失败为不可用；保留旧说明失败明确来源；失败内容不部分启用。 |
| 副作用和业务事实 | 展示无新增保存；预览意图与派生动作按原契约保持；合法的无法估算状态不增加导出限制；计划、生效参数与报告不携带估算资料。 |
| 文档和证据 | 责任专题引用一致；日期、环境、范围和实际门禁结果完整；真实码率统计口径仍归设备接入验证。 |

公共格式与重载待核实的行为契约由设计文件定义。实施者若发现现有数据流不能保持这些契约、出现表中未定义的状态，或需要改变公共字段或加载语义，须停止相关任务，报告输入、发生步骤、已完成操作及冲突依据；不能自行选择新的业务语义。
