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
- [ ] R3：沿修复处理生产者到登记、文件提升及后续读取追踪完整性和生命周期。先覆盖完整/未完整、所需/可清理/已提升/已交接、自动清理各状态的有效组合，再实现当前事件事实校验和适用共同提升。核对名称、MIME、媒体结构和初始可用性的实际来源；不能用空对象或固定 AVAILABLE 掩盖未知。若处理接口尚缺可靠事实，明确列出缺口，不能补造完成依据。
- [ ] R4：核对完成事务原键重送、后续新键及恢复路径，保留第一次保存的结果和文件身份。根据同键核对契约补失败测试，再修改重送接口；不得重新登记或改写既有终态。
- [ ] R5：审计取回目录读取与正式写入两侧的同类规则，运行相关单元测试及集成测试（分别执行，Python 3.11/3.12），核验完整回滚、事件次序和历史事实，更新主路线图及 X1/C3 勾选项。各局部步骤完成不等于正式登记全流程已经验收。

R1 可独立完成；R2、R3 的文件提升与关联事件需要一起核对，R4 建立在确定的登记结果结构上。R5 为最终门禁。计划不引入文件移动、设备调用或数据库迁移；若核实后需要改变既定协议，应先说明具体不一致。

R3 的实施前提包括中间文件结果与生命周期事件、录像处理结果的正式守卫和仓储入口。`MediaArtifact` 提供工具成品、大小、摘要及同步事实，`decide_media_processing` 提供处理分支；二者本身不构成数据库中的完成及提升事实。须追踪实际结果保存链，不能注册空守卫放行 `INTERMEDIATE_FILE_CHANGED.LIFECYCLE`，也不能用预置 `PROMOTED` 文件的关联测试替代完整提升验收。

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

同批引用通过真实完成入口验证：先后顺序、跨表同号文件、预览与修复共同登记、缺失及错误引用、重复预览、关联写入故障和整组回滚。既有原片引用通过正式事件组合验证，包含当前事实的先后顺序和已清理派生仍保留身份。未知关系集合、同事件重复派生、未来原片不能提前授权等分支由具名守卫单元测试验证；复核没有发现 R2 阻断问题。R3—R5 保持未完成，尤其不能据此证明修复文件的完整提升链或终态追加入口。
