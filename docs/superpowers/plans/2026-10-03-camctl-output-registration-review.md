# 正式产物登记的来源与文件事实

## 范围与契约

本计划连接 [X1 正式产物](2026-09-30-camctl-outputs.md#x1-正式产物登记及文件关系)、采集完成事务与后续取回读取。有效规则来自[文件字段](../../camctl/database/file-fields.md#正式产物与来源)及 `OUTPUT_REGISTERED` 事件登记；下面的模块拆分是实施建议。

登记输入是来源动作、待登记文件及明确的原片引用。状态库中已经保存的动作绑定、文件归属和完成事实是权威输入；调用方的 `ownership_confirmed`、`file_complete` 只能表达请求条件，不能覆盖状态库。登记输出是原文件身份上的正式产物，适用关联、文件提升、动作终态与父计划变化在同一事务中保存。任一校验或写入失败时整组回滚，不执行文件副作用。

### 来源判定

以下各行规则共同适用；任一条件不满足即拒绝整个登记事务。

| 条件维度 | 合法状态 | 其余状态的处理 |
| --- | --- | --- |
| 请求身份 | `catalog_facts.action_id` 等于完成命令的 `action_id` | 拒绝，不能忽略不一致的目录上下文 |
| 来源动作 | 来源存在，且为单张拍摄、录像或延时摄影 | 缺失、其他动作类型均拒绝 |
| 设备承载文件 | 文件存在，归属已确认，`source_action_id` 等于产物来源且有归属依据 | 未知归属、异源或缺少依据均拒绝 |
| 原设备绑定 | 文件观察者是存在的拍摄动作，观察者和来源各自保存非空的 `device_id`、`driver_id`，且两个字段分别相等 | 不能将未知绑定当作相同，也不能以当前配置替代已保存绑定 |
| 观察者与来源的动作身份 | 可以是同一动作，也可以是同一设备及驱动上的不同动作 | 不能以观察者 ID 代替来源证据 |
| 中间文件 | 文件存在，`owner_action_id` 等于来源，且 `owner_delivery_id` 为空 | 异源、交付责任或缺失文件均拒绝 |
| 事件次序 | 使用登记事件之前的当前文件事实；此前事件可建立所需事实 | 事务后续的归属修正不能使当前登记合法 |

### 文件、关联与元信息

| 产物种类 | 文件资格 | 关联要求 |
| --- | --- | --- |
| 原片 | 已完成的设备原片 | 不携带 `output_origins` |
| 预览 | 已完成的设备预览 | 恰有一条同源原片关联，并与设备文件的可靠配对一致 |
| 修复成品 | 来源动作的完整修复输出，提升为正式文件且不再参与自动清理 | 恰有一条同源原片关联 |

同一物理文件至多登记一次；设备文件 ID 与中间文件 ID 属于不同身份空间。同一原片最多一份预览和一份修复成品。关联不能依赖输入顺序；既有产物 ID 和同批设备原片文件 ID 必须保持可区分的含义。

登记计算保留 `original_output_id` 与 `original_batch_file_id` 两种引用；同批引用只指向设备原片。事务为全部新产物分配身份后解析同批引用，先登记原片，再登记派生文件及其关联；返回的产物 ID 仍按输入顺序排列。既有原片须从当前记录读取，不能用未来创建的原片提前授权登记。

动作、文件与原片引用使用合法的正整数对象 ID，布尔值、浮点数和数字字符串不能充当身份。正式登记事件可引用已终态来源的既有原片。`FinishCaptureCommand` 只接受未取消的执行中动作，其成功用例不证明向已完成动作追加产物的能力；终态后的重送与恢复入口由 R4 单独核对。

| 关联状态 | 登记结果 |
| --- | --- |
| 原片没有出向关联 | 允许按原片登记 |
| 原片携带出向关联，或派生没有恰好一条共同登记的关联 | 整组拒绝 |
| 引用目标缺失、不是原片、仍携带派生关联或不属于同一来源 | 整组拒绝 |
| 预览的设备文件配对与所引用原片的设备文件一致，且有配对依据 | 继续核对同源派生唯一性 |
| 配对未知、配对缺少依据、配对指向其他文件 | 整组拒绝，不改写设备观察以迎合请求 |
| 同一原片已有同种派生，或同一事务再次登记同种派生 | 整组拒绝；原有产物即使已清理也保留身份 |
| 同一原片只有另一种派生 | 可登记尚缺的种类 |
| 无法证明出向关联和反向派生集合已经完整读取 | 拒绝，不能把未读取当作空集合 |

出向关联用唯一索引读取；反向集合读取最多三条，第三条即证明超过第一版允许的数量，须拒绝而非截取两条。查询成功且集合合法后才声明读取覆盖；当前事务先前事件产生的关联也参与唯一性判定。关联事件不得夹带不属于本事件新产物的关系。

大小与摘要继续保存在文件记录中。名称与 MIME 类型取自实际观察；媒体未检查时表达 `not_performed` 和未知时长。可用性须由文件存在性和适用清理事实派生，不能因完成状态而猜测存在。

## 实施步骤与门禁

- [x] 用真实完成事务复现来源未知、异源、异设备、异驱动、非拍摄来源及请求身份冲突仍登记成功的情况；另验证合法的不同观察者。
- [x] R1：在正式 `output` 守卫统一校验来源、设备观察者绑定及中间文件责任。完成命令读取相关文件和观察者事实，但不把外部计划的观察者加入本计划状态聚合；核对目录上下文动作 ID。先补正式守卫的失败测试，再实现，覆盖原片、预览、修复三条分支、缺失事实和未来提案不能补足当前事实。真实事务拒绝须保持整个数据库不变。
- [x] R2：在登记计算中保留同批、既有原片的明确引用，通过产物身份映射共同保存 `output_origins`。先分别验证预览关联、修复文件身份及关联写入故障，再实现。守卫核对两端角色、同源、物理配对及每种派生唯一性；集合唯一性须声明并读取完整范围。覆盖输入乱序、表间同号文件、重复派生、引用缺失/错误种类，以及关联写入失败回滚。不得删去事件结构要求或任取第一份候选。
- [x] R3：沿修复处理生产者到登记、文件提升及后续读取追踪完整性和生命周期。先覆盖完整/未完整、所需/可清理/已提升/已交接、自动清理各状态的有效组合，再实现当前事件事实校验和适用共同提升。核对名称、MIME、媒体结构和初始可用性的实际来源；不能用空对象或固定 AVAILABLE 掩盖未知。若处理接口尚缺可靠事实，明确列出缺口，不能补造完成依据。验证记录见下文；重送与恢复入口归 R4。
- [x] R4：核对完成事务原键重送、后续新键及恢复路径，保留第一次保存的结果和文件身份。根据同键核对契约补失败测试，再修改重送接口；不得重新登记或改写既有终态。验证记录见下文。
- [x] R5：审计取回目录读取与正式写入两侧的同类规则，运行相关单元测试及集成测试（分别执行，Python 3.11），核验完整回滚、事件次序和历史事实，更新主路线图及 X1/C3 勾选项。各局部步骤完成不等于正式登记全流程已经验收。验证记录见下文。

R1 可独立完成；R2、R3 的文件提升与关联事件需要一起核对，R4 建立在确定的登记结果结构上。R5 为最终门禁。计划不引入文件移动、设备调用或数据库迁移；若核实后需要改变既定协议，应先说明具体不一致。

R3 的实施前提包括中间文件结果与生命周期事件、录像处理结果的正式守卫和仓储入口。`MediaArtifact` 提供工具成品、大小、摘要及同步事实，`decide_media_processing` 提供处理分支；二者本身不构成数据库中的完成及提升事实。须追踪实际结果保存链，不能注册空守卫放行 `INTERMEDIATE_FILE_CHANGED.LIFECYCLE`，也不能用预置 `PROMOTED` 文件的关联测试替代完整提升验收。

媒体生产者的调用结果、可靠视频时长、文件观察与同步责任按[媒体结果计划](2026-10-03-camctl-media-results-review.md)逐项验证，再接入 R3 的保存和提升链。工具入口局部测试通过不代表完整文件资格已经成立。

## R1 验证记录

Python 3.11、3.12 分别执行以下范围，两个版本各通过 690 项单元测试、340 项集成测试。单元和集成分开运行，集成按版本串行执行；命令从仓库根运行，`PYTHONPATH=apps/camctl/src:apps/camctl/tests`，解释器分别使用开发环境的 3.11、3.12 虚拟环境。

```bash
python -m pytest -q apps/camctl/tests/unit/capture apps/camctl/tests/unit/outputs apps/camctl/tests/unit/history
python -m pytest -q apps/camctl/tests/integration/capture \
  apps/camctl/tests/integration/outputs/test_selection_event_sequence.py \
  apps/camctl/tests/integration/outputs/test_selection_derived_sequence.py \
  apps/camctl/tests/integration/outputs/test_product_event_sequence.py \
  apps/camctl/tests/integration/outputs/test_registration_source_sequence.py \
  apps/camctl/tests/integration/outputs/test_catalog_integrity.py \
  apps/camctl/tests/integration/outputs/test_catalog_read_facts.py \
  apps/camctl/tests/integration/outputs/test_output_family.py
```

新增来源守卫单元测试覆盖合法拍摄类型、三种产物、未知及缺失归属、文件观察者原绑定、中间文件交付责任，以及未来事实不能授权当前登记。完成入口集成测试验证六类冲突整组回滚、同绑定的不同观察者合法，以及外计划观察者不改变计划聚合；派生事件集成测试覆盖预览、修复误用异源文件时完整回滚。独立只读复核未发现 R1 阻断问题。这些结果不证明 R2—R5 已完成。

## R2 验证记录

Python 3.11、3.12 的 `unit/capture`、`unit/outputs`、`unit/history` 各通过 766 项。Python 3.12 的上述集成范围通过 351 项；补充原片字段完整性和已清理派生的用例后，登记与事件组合范围再次通过 146 项。Python 3.11 随后在上述完整集成范围通过 355 项，包括新增的四项已清理/可用派生唯一性用例。测试分别执行，集成按版本串行运行。

Python 3.12 最后复查命令如下，使用与 R1 相同的 `PYTHONPATH`：

```bash
python -m pytest -q apps/camctl/tests/integration/capture/test_registration_relations.py \
  apps/camctl/tests/integration/capture/test_registration_authority.py \
  apps/camctl/tests/integration/capture/test_recording_finish.py \
  apps/camctl/tests/integration/outputs/test_registration_source_sequence.py \
  apps/camctl/tests/integration/outputs/test_selection_derived_sequence.py \
  apps/camctl/tests/integration/outputs/test_product_event_sequence.py \
  apps/camctl/tests/integration/outputs/test_selection_event_sequence.py
```

## R3 验证记录

修复成品的共同提升与产物元信息的实际来源随[媒体计划 M4b](2026-10-03-camctl-media-results-review.md#实施顺序与验收) 落地。`FinishCaptureCommand` 对每份 REPAIRED 草稿先核对资格（承载文件为 `REPAIR_OUTPUT` 用途、归属本动作、仍处 `REQUIRED` 保留、`size_bytes` 与 `sha256` 齐备，且本动作的 `recording_processing` 声明修复成功并指向该文件），再在登记事件之后同事务保存 `INTERMEDIATE_FILE_CHANGED.LIFECYCLE`（`retention_state` 1→3），任一失败整组回滚。`output` 守卫补齐 LIFECYCLE 分支：推进到 `PROMOTED` 的中间文件必须由同事务先行的 REPAIRED 登记行授权（提升只因登记发生）；推进到 `RELEASABLE`/`HANDED_OFF` 的保留变化不经此核对。守卫在 REPAIRED 登记分支按同样事实复核资格，命令层与守卫双层实施。

| 元信息 | 登记时的实际来源 |
| --- | --- |
| `original_name`、`media_type`（原片、预览） | 承载设备文件行的可读观察值；观察未保存时保留空值 |
| `original_name`、`media_type`（修复成品） | 关联原片设备文件行的可读观察值；修复不重新编码，类型与原片一致 |
| `media_json`（三类产物） | 公共 media 结构表达 `not_performed` 与未知时长；登记时刻产物文件本身没有媒体检查，处理观察保存在 `recording_processing`，不冒充产物观察 |
| `availability` | 初始登记固定 `AVAILABLE`，其依据由完整字节与修复成功核对建立；缺任一事实整组拒绝而非降级为未知 |

R2 既有用例的预置 `PROMOTED` 种子全部改为生产路径（`REQUIRED` 起步经登记事务提升）；手工合成 REPAIRED 登记事件的用例补齐事件时刻的修复成功事实，并按登记先于提升的事件次序构造。已提升文件再次登记、`RELEASABLE`/`HANDED_OFF` 文件登记、修复未成功（含取消）、输出指向他处、缺字节、缺处理记录分别拒绝并保持数据库不变。守卫单测覆盖配对正反例与资格反例；集成用例剔除登记事件后提升事件被守卫拒绝。取回读取侧 `_family_members` 原有的"修复产物必须由已提升文件承载"规则与登记前提经同事务提升衔接，两端一致。

Python 3.11 通过组件单元 2793 项（含新增 `unit/capture/test_output_promotion.py` 18 项）、组件集成 2946 项另 7 项跳过（含新增 `integration/capture/test_output_promotion.py` 11 项）、根跨组件 34 项另 342 subtests、`check-protocol`、`check-report-dependencies`、`check-doc-links`（2909 链接）。`check-event-transitions.mjs` 的 history-formats.md 区段为 HEAD 既有失败，与本段无关。缺口：修复成品自身的可靠视频时长依赖媒体计划 M2 的设备适配证据，登记前不补造时长观察；完成事务原键重送与恢复路径按计划归 R4。

同批引用通过真实完成入口验证：先后顺序、跨表同号文件、预览与修复共同登记、缺失及错误引用、重复预览、关联写入故障和整组回滚。既有原片引用通过正式事件组合验证，包含当前事实的先后顺序和已清理派生仍保留身份。未知关系集合、同事件重复派生、未来原片不能提前授权等分支由具名守卫单元测试验证；复核没有发现 R2 阻断问题。R3—R5 保持未完成，尤其不能据此证明修复文件的完整提升链或终态追加入口。

## R4 验证记录（2026-10-05）

`FinishCaptureCommand` 的重送接口按同键核对契约修改：原键重送先核实原事务是完成登记（首事件 ACTION_FINISHED）、事实时刻、动作身份、终态分支（成功/失败与错误编号）与产物集合（种类与文件身份，按输入次序恢复首次 output_ids，派生的显式原片链接一致），任一不同按操作身份冲突拒绝；一致时只读恢复首次结果（`CaptureResult.disposition=ALREADY`），不产生新事件。动作已终态后的新键不重新登记：终态分支与错误编号匹配且既有产物集合与输入一致时同样只读恢复首次身份；追加、改写或分支不符整组拒绝，既有终态与文件身份不变。`CaptureResult` 增加 `disposition`（SAVED/ALREADY）。

先补失败测试再实现；既有"终态不重写"用例按 R4 契约更新为"同输入恢复首次身份、追加被拒"。Python 3.11 全量回归：组件单元 2902 项、组件集成 2979 项另 7 项跳过（新增 `integration/capture/test_finish_reuse.py` 8 项：原键恢复与时刻/产物/分支冲突、终态新键恢复、追加拒绝、失败终态重送与错误码不符拒绝）、根跨组件 34 项另 342 subtests、check-protocol、check-report-dependencies、check-doc-links（2913 链接）通过。重送不逐字核对 `error_details_json` 与批次原片链接（首次保存时已由守卫校验，重送按错误编号与文件身份判定）；如 R5 审计认为需要再补。

## R5 验证记录（2026-10-05）

**两侧同类规则审计**：取回目录读取侧（`_family_members` 及其 `_CatalogReads` 关联查询）与正式写入侧（`validate_output_registration`、`FinishCaptureCommand`、`output`/`action_finish` 守卫）逐类比对，两侧规则一致，未发现单侧缺口：

| 规则 | 写入侧 | 读取侧 |
| --- | --- | --- |
| 种类与承载表 | `OutputDraft` 类型层：REPAIRED⟺中间文件，原片/预览⟺设备文件 | 设备/中间分支互斥；修复必须中间文件承载，设备产物按角色映射（ORIGINAL/PREVIEW） |
| 归属一致 | 命令核对绑定与来源动作；守卫核对 `source_action_id` 与承载文件归属 | 产物与承载文件（`source_action_id`/`owner_action_id`）都须等于原片来源 |
| 关联结构 | 原片不带关联、派生恰一引用、同批引用指向原片文件；守卫核对既有原片与每种类唯一 | 原片无派生关联、派生必有关联且指向无关联原片；同一原片每种类最多一份 |
| 修复承载 | 守卫：REPAIR_OUTPUT 用途、归属、完整字节、提升配对（登记在前提升在后） | 用途、归属、retention=PROMOTED、cleanup=NOT_NEEDED（PROMOTED 不归自动清理，两侧状态空间一致） |
| 预览配对 | 守卫核对设备文件 `original_device_file_id` 与配对证据 | 同两字段与产物原片关联一致；原片文件不得携带配对 |
| 文件完成 | 草稿完成依据与命令核对写完事实 | 已登记设备产物必须保留完成事实 |
| 摘要与长度 | 摘要可未知、已有可靠值保留；字节事实与提升同事务 | 长度可解释性校验，事实原样带回目录条目 |

**R4 遗留核对判断**：原键重送补 `error_details_json` 逐字核对（同码不同详情按操作身份冲突拒绝，防止调用方错误被首次事实掩盖）；批次原片链接不单独核对——同一批草稿的同批解析是确定的，身份级核对（产物集合与显式原片链接）已固定登记事实，诚实输入无法构造批次链接不一致，单独核对不增加可达保护。

**测试核验**（单元与集成分别执行，Python 3.11）：组件单元 2902 项、组件集成 2981 项另 7 项跳过（新增重送详情冲突与同批配对重送恢复 2 项）、根跨组件 34 项另 342 subtests、check-protocol、check-report-dependencies、check-doc-links（2913 链接）通过。完整回滚（身份冲突与追加登记整组拒绝、iterdump 不变）、事件次序（登记在前提升在后、字节在先成功在后、重送不产生新事件）与历史事实（原键恢复首次 output_ids 与终态、既有终态不改写）由 test_finish_reuse、test_output_promotion、test_discard 及既有守卫用例覆盖。

**边界**：本审计不证明真实设备联调、自动预览扩展（X10）或调度接线；正式登记全流程的最终验收仍按路线图阶段 6/8 门禁执行。
