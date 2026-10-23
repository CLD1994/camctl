# 视频结果模式、公共分段层与 Action6 接入执行计划

> **执行要求：** 阅读本计划与三份设计规格，按 `- [ ]` 跟踪实际交付。实施方式根据实际依赖和工作区选择，Superpowers 技能作为方法参考；任务依赖、阶段集成与进度更新遵守仓库实施规则及本版执行安排。

**目标：** 让 Action6 和 OSMO 360 II 都能按每相机部署配置选择独立的单文件或多段视频流程，将共有分段能力抽离，并完成 Action6 普通录像及原生延时的异常实际内容检查、整段取回和恢复。

**架构：** 受理时冻结显式结果合同，执行入口按合同分派。公共层负责观察集合、范围、逐段检查、共同发布、取回复用及历史；设备特化负责名称、目标解释和判定规则。先接通现有单文件与 OSMO 业务，再扩展 Action6，逐段检查产生的副本必须接入完整的使用与清理责任后才启用异常检查。

**技术栈：** Python 3.11、SQLite、现有 ADB 适配与受管文件操作、ffprobe／ffmpeg、根公共协议、TypeScript 客户端、C host（主程序接入模块）及 wheel 安装链。

**规格：** [公共视频结果设计](../specs/2026-10-23-video-result-modes-and-segmentation-design.md)、[Action6 特化](../specs/2026-10-23-action6-video-segmentation-design.md)、[OSMO 特化](../specs/2026-10-20-osmo360ii-record-segmentation-design.md)。控制与计时继续引用[相机录像](../../architecture/camera-recording.md)、[拍摄与原生延时](../../architecture/camera-capture.md)、[异常恢复](../../architecture/camera-recovery.md)及两份设备规格引用的完成、启动余量合同。

## 全局约束

- `devices.<id>.video.result_mode` 按每相机统一设置，默认 `single_file`，可选择 `segmented`，同时作用普通录像和原生延时视频；配置结构、错误边界与冻结规则以公共规格为准。
- 单文件普通录像保留原必要视频数量与单原片异常处理；单文件原生延时保留原有限任务及最低格式、数量要求。模式不会根据实际文件数自动切换。
- 开发重构可以统一调整模型、枚举、数据库及事件格式，不承担旧格式读取或迁移；当前格式仍保持首次受理后固定、原申请核实和同一历史边界恢复。
- 正常分段任务默认全部合格视频，不新增内容检查或输入拷贝；正常完成已可靠保存且仅登记中断时继续正常登记。
- 实际内容策略只在异常恢复中完整拷贝必要分段，检查主视频并选择达到目标的最短连续前缀，最后一段完整交付。范围外合格原片仍登记，明确 ID 可以取回。
- Action6 普通录像累计主视频精确时长；原生延时采用 `floor(D/I)+1` 个采样帧，`300/2` 为 151 帧，30 fps 时约 5.033 秒；实际值采用可靠整数帧数。
- OSMO 普通分段保持完整非尾段 4.5～5.5 秒合同，采用 `4500*K` 毫秒下界及 `N=(T_ms-1)//4500+1`；最后非零尾段不计入 `K`，后续空尾不提升该尾段。其原生延时使用自身空尾策略。
- 命名、顺序和后继由设备特化提供；归属始终来自可靠完整基准与本次活动。当前两设备均使用目录内 `0001..0999`、下一目录 `0001` 的顺序，datetime 不作排序、归属或计时依据。
- 独立照片按原规则登记和取回，不进入视频顺序、覆盖或范围过滤。视频范围为空或未知时分别保留视频选择失败及照片处理结果。
- 来源结果、产物、视频范围、适用覆盖、等待转换及副本复用关系共同提交。成员、检查、范围和等待者按页处理，不进入增长 JSON 或完整内存列表。
- 副本复用只面向来源提交时已等待的正式取回；各取回仍有独立 delivery 和交付文件。相关取回全部结束且实际文件操作收场后，才清理副本。
- 普通录像取消放弃内容；原生延时取消保留合格完成文件。停止未知、调用结束、动作终态和设备执行状态分别表达；时钟受限会话不启动或恢复媒体检查。
- 公共机器表示只在根 `protocol` 维护；编号、事件、归属与报告依赖从现有权威登记读取或生成。软件、安装包、ARM、真机、媒体及客户端 ACK 分别保留证据。

## 优先审查的五类场景

| 场景 | 用户应取得的结果 | 固定它的任务 |
| --- | --- | --- |
| 受理后切换部署模式，或同驱动的两台设备配置不同 | 已受理动作保持原模式；新动作及能力说明采用各自设备的新选择；单文件普通录像不会接受多段合同。 | V1、V2、V10 |
| 已观察首段消失，或 RAW/JPEG 与视频索引混在同页 | 视频起点不后移，照片不制造缺口，也不被视频范围筛掉；空范围与未知范围分别报告。 | V2、V4、V8 |
| 第三段拷贝或帧数检查未知，但已有可靠控制完成 | 固定前两段，覆盖未确认；动作可以成功，前缀可取回，不检查第四段补位。 | V5、V6、V8 |
| 来源提交回执丢失，同时存在两个等待取回和一个后到请求 | 原事务固定原等待者；前两个可复用，后到请求读设备；每次 delivery 独立，恢复不重建截点。 | V7、V8、V10 |
| 一个取回结束，另一个仍在读检查副本，清理和报告同时推进 | 源副本及目标文件均受保护；全部相关责任结束才清理；旧 H 的范围、覆盖和报告字节保持。 | V7、V9、V10 |

## 使用方法与当前基线

编制时基线为 `2f570f0`，实际进度由[当前进度与交接记录](#当前进度与交接记录)及对应步骤维护。执行前核对当前目录、分支、工作区、实际生产代码与正式进度；不重复 OSMO 原计划已经完成的工作。其软件和现场证据仍由[原分段计划](2026-10-21-osmo360ii-record-segmentation.md#当前执行位置与继续顺序)跟踪。

V1—V11 是软件及安装版交付；V12 是独立安排的目标环境与实机验收，不作为软件步骤的前置。没有真实设备证据时保留 V12 未完成，不改变软件步骤的实际结果。

### 本版执行与验证安排

软件交付范围为 V1—V11，包括已有明确合同的失败、取消、恢复、历史及安装版联合验证。先补齐实现缺口和定向证据，生产接线稳定后集中完成完整软件门槛；已通过且未受变更影响的结果可复用。组件集成按目录逐进程执行，实际边界隔离后可在独立工作区并行，避免无依据地重复耗时测试。V12 的目标 ARM 与真机验证留待设备准备，不作为本次软件交付的前置。所有未完成步骤保持未勾选，不能用已接通的正常路径替代完整软件验收。

下文的模块名、接口签名、新测试文件和提交拆分是**实施建议**。它们用于明确任务间交接；执行者可以按实际结构调整，但须同步全部生产者、消费者、测试与本计划。规格确定的行为、失败语义及仓库规则保持。每次只补当前任务需要的能力；出现需用户决定的行为缺口时按[设计缺口规则](../../../.agents/rules/design-and-planning.md#实现中发现设计缺口)处理。

除特别说明，命令在仓库根执行。表中的 camctl 生产路径相对于 `apps/camctl/src/camctl/`；测试路径和其他组件路径写仓库相对路径。建议新增的文件在建立前不执行对应测试命令。每项验证必须有真实完整运行结果；下面的预期不代表已经运行。

### 执行环境与提交方式

先读取[组件测试指南](../../../apps/camctl/tests/AGENTS.md)、[客户端说明](../../../apps/client/README.md)、[跨组件测试说明](../../../tests/integration/README.md)及[脚本入口](../../../scripts/README.md)。核对现有 `.venv311` 的 Python 3.11；缺少环境时按组件指南用 uv 准备。Node、npm、C 工具、媒体工具及离线依赖按对应测试说明核对，不将缺工具记为用例通过。客户端依照其 README 使用 `package-lock.json` 和 `npm ci`，先确认 npm CLI 可调用，再执行构建与测试。

```bash
pwd
git rev-parse --show-toplevel
git branch --show-current
git status --short
apps/camctl/.venv311/bin/python --version
node --version
npm --version
export PYTHONPATH="$PWD/apps/camctl/src"
apps/camctl/.venv311/bin/python -c 'import camctl; print(camctl.__file__)'
```

源码侧 Python 验证显式使用当前工作树的 `apps/camctl/src`，并保存实际导入位置；共享解释器不共享可编辑安装的归属。安装版子进程按联合测试说明移除源码搜索路径，实际导入独立环境的 `site-packages`。

每项新增行为先写能证伪合同的失败测试，实际运行确认失败原因，再实现并运行同一范围。集成测试按目录分别启动 pytest，同工作区顺序执行；跨任务共享资源先隔离。阶段结束检查 diff、更新原步骤进度并提交该任务实际文件，检查暂存差异及提交后状态。局部通过后继续下一项，不为没有新增疑点的范围重复全量回归。

## 依赖与交付顺序

| 任务 | 必需前置 | 当步可观察交付 |
| --- | --- | --- |
| V1 显式模式与现有流程 | 已批准规格、现有装配 | 两相机单文件流程及 OSMO 分段可经部署、受理和执行选择。 |
| V2 公共层与 OSMO 特化 | V1 的冻结合同 | OSMO 的正常、异常、取消及取回实际经公共层工作。 |
| V3 公共覆盖协议与客户端基础 | V1 的模式含义 | 根协议、类型生成、客户端导入和展示可消费新的覆盖结果。 |
| V4 Action6 正常分段 | V2、V3 | 普通与原生延时完整登记、默认取回及正常覆盖报告闭合，无媒体拷贝。 |
| V5 主视频指标与纯覆盖判定 | V2、V3、V4 的冻结目标 | 现有受管工具能取得时长或实际帧数，纯规则能确定前缀。 |
| V6 独立分段检查责任 | V5、现有可靠拷贝与历史内核 | 一段的完整拷贝、检查、保存和重启接续可独立执行。 |
| V7 来源提交与副本取回复用 | V2、V4、V6 | 来源事务、等待者截点、独立取回与副本清理共同闭合。 |
| V8 Action6 异常业务接入 | V4—V7 | 两类动作从恢复入口到检查、终态、范围及实际取回闭合。 |
| V9 覆盖报告与同 H 重建 | V3、V8 | 实际检查结果进入有界报告，客户端与历史取得一致结果。 |
| V10 安装版联合验收 | V9 | wheel、C host、CLI、客户端及 ACK 的模式与恢复矩阵。 |
| V11 回归与发行交接 | V10 | 完整软件门槛、归档发行物及准确部署资料。 |
| V12 ARM 与实机 | V11、届时设备及执行安排 | 目标固件的控制、分段、媒体及交付证据。 |

推荐按表顺序集成。V3 与 V2 的调查、V5 中不依赖 Action6 目标提供者的媒体端口适配可在接口稳定后并行开发；它们不阻塞不依赖新字段或指标的现有模式验证。共享文件修改使用隔离分支，按依赖集成后再执行联合检查。

V1、V2、V4 都须取得实际业务结果，不能以基础类型或仓储测试代替。V5、V6、V7 分别提供指标、文件责任及安全交付，是 V8 启用异常检查的直接前置：缺少 V6 无法保存多段输入，缺少 V7 会提前清理等待取回仍要使用的文件。这三项只实现当前链所需能力，各自完成即回到后续业务接入，不扩建额外框架。

## 文件职责与任务接口

| 责任 | 主要现有文件与建议新增位置 |
| --- | --- |
| 部署和受理 | `bootstrap/config.py`、`devices/catalog.py`、`devices/tasks.py`、`devices/drivers/adb_cameras/{registration,definitions,action6_record,osmo360ii_capture}.py`、`acceptance/definitions.py`、`capture/models.py`；建议 `devices/video_results.py`。 |
| 公共集合与策略 | `capture/{segment_models,segment_storage,segment_completion,native_completion,segment_finish}.py`、`contracts/segment_values.py`、`devices/segment_profiles.py`；设备算法保留独立模块。 |
| 设备身份与目标 | `devices/segment_profiles.py`、`devices/drivers/adb_cameras/osmo360ii_segments.py`、`capture/{segments,native_segments}.py`；共享目标、判决和精确累计位于 `contracts/video_coverage.py`。 |
| 存储、守卫及共同提交 | `persistence/repositories/segment*.py`、`persistence/repositories/{capture,outputs,history}.py`；新增检查与复用仓储按 V6、V7 划分。 |
| 字节和媒体 | `host_files/media.py`、`capture/{media,media_facts,media_flow}.py`、`outputs/{qualification,read_attempts,work_files,obtain_flow}.py`；建议 `capture/segment_inspections.py`。 |
| 历史及公开报告 | `history/{snapshots,snapshot_stream}.py`、`contracts/public_projection.py`、`reporting/bounded_generation.py`、`docs/camctl/database/` 的 SQL、编号、事件、归属及报告依赖登记。 |
| 机器协议及客户端 | 根 `protocol`、`apps/client/src/{domain,shared,web}` 的报告校验、历史合并、结果模型和界面；类型从根 Schema 生成。 |

下面列出任务间使用的建议接口。共同事实先在生产类型中定义；各任务只补自己拥有的行为。现有 `SegmentResultRef`、`SegmentKey`、`HistoryBoundary`、`DbOutcome`、操作键及分页类型继续复用。

| 接口或类型 | 定义任务与调用任务 |
| --- | --- |
| `resolve_video_result_mode(declaration: Mapping[str, object]) -> VideoResultMode`；`read_video_result_contract(spec: Mapping[str, JsonValue]) -> VideoResultContract` | V1 定义；装配、恢复、V2—V9 消费。合同显式包含模式和适用的特化标识，单文件不靠缺字段表示。 |
| `resolve_segment_profile(contract: VideoResultContract) -> SegmentProfile`；`SegmentProfile.classify_file(identity: str) -> SegmentFileFacts` | V2 定义，V4 增加 Action6 实现；V6、V8 消费。文件事实表达视频、预览、独立照片、顺序键及配对身份，未知单独表示。 |
| `SegmentDecision` | V2 定义有限的完成、诊断与视频范围，V3 增加适用覆盖引用；共同提交与报告消费。设备算法的内部累计状态不作为公共判断格式。 |
| `CoverageTarget`、`CoverageDecision`；`SegmentProfile.coverage_target(effective_params: Mapping[str, JsonValue]) -> CoverageTarget \| None` | V3 定义目标与有限覆盖结果结构，V4 实现 Action6 目标提供者并在受理时冻结目标；V5—V9 读取同一目标。OSMO 下界策略不产生实际内容目标。 |
| `VideoMetric`、`CoverageAccumulator`、`CoverageFinishFacts`；`advance_coverage(state: CoverageAccumulator, metric: VideoMetric) -> CoverageAccumulator`；`finish_coverage(state: CoverageAccumulator, facts: CoverageFinishFacts) -> CoverageDecision` | V5 使用 V3 的结果类型定义纯累计规则；V6 产生逐段度量，V8 编排，V9 投影。目标量纲和数值须匹配，停止原因及完整集合事实为显式输入。 |
| `inspect_segment(runtime, request: SegmentInspectionRequest) -> SegmentInspectionStep` | V6 定义异步端口；请求引用来源动作、集合成员、目标量纲及原预算。V8 调用；Step 表达等待、完成或有限结束，引用原检查身份而非返回整组文件。 |
| `finish_segment_recording(request, key, owned, *, batch_size=128)` | V2 将现有端口推广到公共判断；V7 加入检查引用和等待者截点，保持有限申请与 `DbOutcome` 原键核实。V4、V8 消费。 |
| `read_bound_check_copy(selection_id: int, output_id: int, owned) -> BoundCheckCopy | None` | V7 定义；取回授权与读取消费。关联缺失与关联损坏分别处理，返回值不改变正式产物源。 |

## V1：接通部署模式、冻结合同与现有完整流程

**为什么先做：** 后续集合、检查和取回都需要可靠知道本动作采用哪个合同。此步必须穿过真实装配和受理，先保住两台相机的单文件流程及 OSMO 已有分段业务。

**文件：** 修改上述部署、任务和受理文件、`capture/handlers.py` 的分派、`bootstrap/capture_assembly.py`、配置说明及相关样例。新增建议接口 `devices/video_results.py`。

**输入／交付：** 消费现有 `DeploymentTaskFactory.__call__(declaration) -> CaptureTaskFactory`。产出显式结果合同、按设备绑定的任务工厂和统一分派读取器；V2 消费。Action6 分段工厂的生产启用在 V4 与处理器共同完成，阶段中不把尚未接通的路径作为已支持能力发布。

- [x] **步骤 1：增加配置与合同失败测试。** 已建立 `apps/camctl/tests/unit/bootstrap/test_video_result_configuration.py` 和 `apps/camctl/tests/unit/capture/test_video_result_contract.py`。省略表、空表、显式两值、非法类型／值／子键及不支持模式分别断言；已受理视频定义缺必需模式为无效定义，不能默认为单文件；单张拍照及不生成视频的任务保留原结构。目录测试另覆盖没有视频任务及仅提供照片任务的驱动拒绝 `segmented`，该反例实际先失败2项，修正绑定后相关配置、合同、目录及特化单元61项通过；实际装配与受理4项通过。
- [x] **步骤 2：运行上述两份单元测试，核对显式模式合同。** 命令为 `apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/bootstrap/test_video_result_configuration.py apps/camctl/tests/unit/capture/test_video_result_contract.py -q`。
- [x] **步骤 3：实现配置解析及部署绑定。** 在加载器校验闭合 `video` 表，经内建驱动注册和目录绑定形成模式工厂；非法配置与设备不支持按所属错误结束，拍摄调用为零。由同一部署定义生成现有能力 `description` 中的模式和必要产物说明，不新增拍摄参数；核对 `Catalog.describe_document()` 与客户端 `loadCapabilities()` 的实际消费，不能只替换 task factory 而保留静态说明。
- [x] **步骤 4：实现冻结、读取及显式分派。** 修改 `CaptureTask`、`build_capture_spec()`、`validate_capture_spec()` 和受理入口，保存本次模式及特化；更新相应事件／历史解码。普通录像及原生延时的恢复读取只消费已保存合同。
- [x] **步骤 5：保留并验证现有完整路径。** 两设备的单文件普通录像要求恰好一份必要视频，原生延时保留最低数量合同；单文件异常继续 `run_recording_media()` 及适用修复。OSMO 分段仍由现有完成模块执行，先用适配器消费新合同。按模式更新正常、失败、取消和恢复入口，不以“存在策略”等价于“使用 OSMO”。
- [x] **步骤 6：增加真实装配和受理集成测试。** 建议 `apps/camctl/tests/integration/bootstrap/test_video_result_mode_binding.py`：使用实际 `_default_catalog`，同驱动两台设备选择不同模式；受理后切换配置再重启，旧定义不变、新请求改变；原请求重送不重建定义。核对 START、STOP 等控制合同不随模式改变。
- [x] **步骤 7：运行最窄门槛。** 单元同步骤 2；集成命令如下，成功标准是实际执行和恢复走正确流程，既有 OSMO 分段夹具显式选择该模式。提交前按全局方式审查并记录进度。

```bash
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/bootstrap/test_video_result_mode_binding.py apps/camctl/tests/integration/bootstrap/test_action6_capture_completion.py apps/camctl/tests/integration/bootstrap/test_osmo_shared_recovery.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/capture/test_recording_media_link.py -q
```

- [x] **步骤 8：完成阶段提交。** 按全局提交方式审查实际 diff 和验证证据，更新本任务原步骤进度，仅提交本任务文件；记录提交标识、通过范围与剩余接线，并核对提交后状态。

## V2：抽离公共分段层，并让 OSMO 全流程使用它

**为什么在 Action6 前完成：** 当前仓储、守卫和取回直接解析 CAM／OSV，并把 OSMO 下界模型当作完整判断。先以已有业务检验公共层，能够发现真实下游接缝。

**文件：** 修改 `capture/segment*.py`、`capture/native_completion.py`、`persistence/repositories/segment*.py`、`contracts/segment_values.py` 与相关事件、SQL、历史登记；公共文件特化位于 `devices/segment_profiles.py`。OSMO 解析和算法仍在对应设备模块。

**输入／交付：** 消费 V1 的 `VideoResultContract`，定义 `SegmentProfile`、`SegmentFileFacts`、有限 `SegmentDecision` 及策略分派；推广现有集合和来源完成端口。V4、V6、V7 直接使用这些生产端口。

- [x] **步骤 1：增加公共合同与 OSMO 回归反例。** 使用 `apps/camctl/tests/unit/devices/test_video_segment_profiles.py` 及 `apps/camctl/tests/integration/persistence/test_segment_file_proofs.py`。断言公共成员资格不会调用固定 CAM 解析器；不匹配冻结特化的成员、配对或申请会被独立守卫拒绝。
- [x] **步骤 2：分别运行单元文件和 persistence 文件，核对反例及回归。** 使用下面前两条命令，两个目录不合并进同一进程。
- [x] **步骤 3：抽离文件身份与有序集合责任。** 审计 `segments.py`、`segment_result_members.py`、`segment_starts.py`、`segment_listing.py` 的直接设备解析与后缀判断。由冻结特化提供解析结果，仓储仍独立核对原页、原固定文件、来源及完整申请，不能信任调用方直接给出的合格标记。2026-10-23：公共文件特化已接入原页证明、固定成员、起点及候选；OSMO 原解析器也复用该解析。定向单元两组分别为59项和49项通过，固定成员／原页文件证明集成15项通过（90.95秒，日志 `/tmp/camctl-video-shared-profile-tests.log`）。Action6 完成判定与发布接线继续后续步骤。
- [x] **步骤 4：抽离设备判定与公共发布。** OSMO 的普通下界／N 和 native 空尾分类返回公共有限判断；增长范围、缺口沿关系分页保存。解除 `segment_transactions.py` 对 OSMO `SegmentAssessment` 的通用假设；必要判定仍由独立守卫按相应策略核对。
- [x] **步骤 5：推广默认选择及可用性查询。** 修改 `persistence/repositories/segment_selections.py`、`segment_queries.py` 和 `outputs/segment_query.py`，使资格来自冻结合同及已发布视频范围；保留普通、预览、明确 ID、组和计划选择的原身份与预算。公共仓储、取回及报告不反向依赖设备完成处理器。
- [x] **步骤 6：将 OSMO 所有生产入口接回公共层。** 普通正常／异常、基准失败、START 失败、外部失败、停止耗尽、取消、原结论恢复及 native 完成分别验证。保持 `CAM_001/0999 → CAM_002/0001`、当前扫描与跨轮保留的区别，缺口不重建虚构文件。
- [x] **步骤 7：运行 OSMO 业务与存储门槛。** 分别运行 `apps/camctl/tests/unit/capture/test_record_segments.py`、`apps/camctl/tests/integration/capture/test_record_segment_scope_recovery.py`、`test_native_segment_completion.py`；再单独运行 persistence 的新文件及 `test_segment_transaction_batches.py`，history 的 `test_record_segment_history.py`，outputs 的 `test_segment_default_scope.py`。同目录文件可在一次进程中组合。断言 `9000 -> N=2`、`9001 -> N=3`、空尾不提升短尾、缺口前缀及运行时零媒体拷贝。

```bash
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/devices/test_video_segment_profiles.py apps/camctl/tests/unit/capture/test_record_segments.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/persistence/test_segment_file_proofs.py apps/camctl/tests/integration/persistence/test_segment_transaction_batches.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/capture/test_record_segment_scope_recovery.py apps/camctl/tests/integration/capture/test_native_segment_completion.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/history/test_record_segment_history.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/outputs/test_segment_default_scope.py -q
```

- [x] **步骤 8：完成阶段提交。** 按全局提交方式审查实际 diff 和验证证据，更新本任务原步骤进度，仅提交本任务文件；记录提交标识、通过范围与剩余接线，并核对提交后状态。

## V3：固定公共覆盖表示，并接通客户端消费

**文件：** 根 `protocol/schemas/status-report.schema.json`、`protocol/errors/workflow-codes.json` 及共享样例；`contracts` 的覆盖值适配；客户端 `apps/client/src/domain/{generate-report-types,reports}.ts`、`apps/client/src/shared/{segment-report-errors,status-report.generated}.ts`、`apps/client/src/web/result-model.ts` 和关联结果界面。生成文件从脚本更新。

**输入／交付：** 消费公共规格的四种覆盖结论和 V2 的范围语义；交付准确根协议、生成类型和消费者，供 V4、V9 输出。此步不要求尚未建立的检查记录作为事实来源，也不把 Schema 通过记为生产报告已接入。

- [x] **步骤 1：增加协议和客户端失败样例。** 建议 `tests/integration/video-coverage-protocol.test.mjs` 及客户端 `tests/unit/video-coverage.test.ts`。覆盖普通／延时、秒数／整数帧、正常未检查、空／未知范围、成功动作但覆盖未知、非法交叉组合、未知与零，以及照片不属于视频数组。
- [x] **步骤 2：运行新样例并核对 Schema 和导入器的接受与拒绝。** 根执行 `node --test tests/integration/video-coverage-protocol.test.mjs`；客户端在 `apps/client` 执行 `npm exec -- vitest run tests/unit/video-coverage.test.ts`。
- [x] **步骤 3：在根 Schema 定义 `result.coverage` 及引用条件。** 建议表示包含量纲、目标、已确认连续量、结论、已检查产物引用、默认视频范围与原因；约束单位对应的数值类型和范围状态。复用既有实体 ID 与错误类型，移除实际内容策略原生延时不能带 `result` 的结构限制，保持单文件及其他策略的出现条件。扩展缺口错误的动作适用范围和引用校验，不能继续限制为普通录像。参数未生效或未形成决定时不补造结果。
- [x] **步骤 4：统一设备位置表示及客户端文案来源。** 解除客户端 `segmentIssueText` 等固定 CAM 展示，按公共位置事实及相应格式描述显示 DJI／CAM。保持缺失位置没有路径或 `output_id`；公共错误编号仍由权威登记提供，不能维护第二套清单。
- [x] **步骤 5：接通导入、跨报告知识及结果展示。** 修改 Schema 校验、`parseReport`、`validateReportAgainstHistory`、`ownFacts` 和实际结果历史合并；展示目标、覆盖、固定范围和各文件结果。完整及增量报告的已知／未知处理遵守同一历史合同；媒体先到、报告先到均不提前确认 ACK。
- [x] **步骤 6：生成公共类型及同步说明。** 客户端目录执行 `node --import tsx src/domain/generate-report-types.ts`，生成类型与根 Schema 一致；同步协议字段说明。
- [x] **步骤 7：运行协议与客户端门槛。** 根执行 `node scripts/check-protocol.mjs`、本任务步骤 2 的协议测试；客户端执行步骤 2 的新单元测试及相关 segment 报告／结果模型测试、`npm run typecheck`。全部预期通过，非法组合仍被拒绝。

建议将业务语义固定到下面的精确结构，再从根 Schema 生成类型；可调整字段组织，但须在本步同步全部消费者。`coverage` 的建议字段为 `metric`、`target`、`confirmed`、`status`、`checked_output_ids`、`default_scope` 和可省略的结构化 `reason`。`metric` 区分 `duration_s` 与 `frames`；`status` 区分 `not_performed`、`reached`、`confirmed_short`、`unconfirmed`；目标与已确认量分别采用精确秒数或整数帧，未知实际量为 `null`。正常未检查时 `confirmed=null`、已检查列表为空，默认范围仍为全部合格视频。范围复用已有公共状态与 ID 结构，已检查列表和范围列表分别从自身关系生成，不能塞进动作的小型 JSON。枚举以权威登记／Schema 为源，后续任务不再定义第二套值。

- [x] **步骤 8：完成阶段提交。** 按全局提交方式审查实际 diff 和验证证据，更新本任务原步骤进度，仅提交本任务文件；记录提交标识、通过范围与剩余接线，并核对提交后状态。

## V4：闭合 Action6 正常分段及照片的默认取回

**文件：** `devices/segment_profiles.py`、`action6_record.py`、`action6_results.py`、设备注册、`capture/{handlers,action6_completion}.py`、公共来源发布、默认选择及必要正常报告投影。

**输入／交付：** 消费 V1—V3，增加 Action6 特化，并同时启用对应部署工厂与正常处理器。交付两类动作从真实 RESULTS 页、来源终态到默认／预览／明确 ID 取回及报告的路径；异常内容检查由 V8 完成。

- [x] **步骤 1：写解析和目标绑定失败测试。** 使用 `apps/camctl/tests/unit/devices/test_action6_record_results.py` 和 `test_video_segment_profiles.py`：DJI 目录与十四位时间、`0999 → 下一目录0001`、大小写配对原字节、不同 datetime、冲突键、非法名和孤立 LRF；不得用编号替代归属。原生延时目标使用冻结采集时长和间隔，不改变照片最低要求。
- [x] **步骤 2：写正常纵向集成失败测试并运行。** 建议 `apps/camctl/tests/integration/capture/test_action6_segmented_normal.py`，同时覆盖普通和 native；观察原页、全部合格 MP4／LRF／照片、默认范围、产物及终态，拷贝和媒体调用次数为零。单独运行该文件确认当前不能闭合多段。
- [x] **步骤 3：实现 Action6 文件特化及普通／native 绑定。** 视频和 LRF 具有序列位置，照片独立分类；普通多段为至少一份非空 MP4，不再要求精确一份。`coverage_target()` 从受理时生效参数生成精确时长或 `floor(D/I)+1` 整数帧，随执行定义固定；正常报告与之后异常检查读取同一目标，不另算一份。native 的 `outputs` 继续读取同一驱动格式映射，单文件路径不改变。
- [x] **步骤 4：接通正常、取消及有限失败的公共发布。** 两类动作共用公共结果集合，保留原控制、等待与取消资格。普通取消放弃；native 取消默认全部合格完成视频及照片。基准／START 失败、文件完成未知和必要格式缺失保持真实失败或未知，不调用 OSMO 下界或 native 专属入口。
- [x] **步骤 5：接通混合产物默认选择。** 建议 `apps/camctl/tests/integration/outputs/test_video_scope_photos.py`，验证默认视频范围加独立照片、配对预览用相同视频范围、范围外明确 ID、视频空／未知时照片继续及其他来源继续。保留实际查询、清理及逐项授权规则。
- [x] **步骤 6：接通正常覆盖的实际报告投影。** 正常实际内容策略输出未执行检查，实际量保持未知；范围及各产物来自同一提交。同步本步实际使用的存储字段、报告依赖及生成路由。新增 `apps/camctl/tests/integration/reporting/test_video_coverage_report.py`，经根 Schema 和客户端 V3 消费，不以单个错误头代替成功动作的覆盖。V9 再完成实际检查产生的增长关系展开和跨层重建。
- [x] **步骤 7：分别运行 devices、capture、outputs、reporting 的上述测试。** 补跑 Action6 现有结果页及单文件必要回归；测试证明模式确实由生产装配选中，原生延时没有被塞入只支持普通录像的 `FinishRecordingResults` 入口。

```bash
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/devices/test_action6_record_results.py apps/camctl/tests/unit/devices/test_video_segment_profiles.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/devices/test_action6_result_pages.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/capture/test_action6_segmented_normal.py apps/camctl/tests/integration/capture/test_recording_media_link.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/outputs/test_video_scope_photos.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/reporting/test_video_coverage_report.py -q
```

- [x] **步骤 8：完成阶段提交。** 按全局提交方式审查实际 diff 和验证证据，更新本任务原步骤进度，仅提交本任务文件；记录提交标识、通过范围与剩余接线，并核对提交后状态。

## V5：提供可靠主视频指标及纯覆盖算法

**文件：** `host_files/media.py`、`capture/media_facts.py`、`contracts/video_coverage.py`。共享类型、目标解释、纯累计及公共判决解码由 `contracts` 唯一维护；媒体操作继续使用现有受管工具与文件端口。

**输入／交付：** 消费目标解释与文件特化，提供逐流可靠时长、实际整数帧数及 V5 接口表中的纯覆盖类型；V6、V8 消费。此步是可独立验证的媒体操作和纯判定，不创建来源终态或交付。

- [x] **步骤 1：写精确指标失败测试。** 扩展 `apps/camctl/tests/unit/host_files/test_media.py`，覆盖主视频／容器时长不同、封面与音频、多个不能解释的主视频、指标缺失或非法、整数帧数、工具错误与媒体错误分别保存。未知帧数不以时长乘帧率补值。
- [x] **步骤 2：写纯覆盖边界失败测试并运行。** `apps/camctl/tests/unit/contracts/test_video_coverage.py`：60 秒目标下 `18+27+22=67` 选三段；恰好目标立即停止；300 秒／2 秒目标151帧，`60+60+45=165`；非整倍间隔不虚构终点；可靠完整不足与未知尾部不同；首段未知形成空前缀。
- [x] **步骤 3：实现受管主视频指标读取和事实编码。** 扩展 `MediaStream`／`MediaProbe` 及 `probe_document()`／`read_probe()`；使用已有工具从完整本地文件取得可靠帧计数，按所选主视频流保存计数依据。工具配置及结果跟随原操作保存，时长保持精度，帧数为整数。
- [x] **步骤 4：实现纯累计与结束判定。** `advance_coverage()` 只累计同量纲可靠值；`finish_coverage()` 明确处理达到、完整不足、中断未确认和范围未知。缺口、不合格或未知位置结束连续前缀；后段不能补位。函数不执行文件或数据库操作。
- [x] **步骤 5：以真实本地媒体工具验证度量端口。** 建议 `apps/camctl/tests/integration/host_files/test_video_metrics.py`，按既有工具环境生成小型已知帧数媒体，经真实受管端口读取。验证持续时间、实际帧数、工具收场及 OSMO 单文件必要流保留，不使用真实相机。缺工具须列出缺失，不能跳过后声称该门槛通过。
- [x] **步骤 6：运行上述单元和独立 host_files 集成文件，更新媒体事实文档。** 指标本身尚未证明拍摄恢复已接入，该结果交给 V6、V8。

```bash
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/host_files/test_media.py apps/camctl/tests/unit/contracts/test_video_coverage.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/host_files/test_video_metrics.py -q
```

- [x] **步骤 7：完成阶段提交。** 按全局提交方式审查实际 diff 和验证证据，更新本任务原步骤进度，仅提交本任务文件；记录提交标识、通过范围与剩余接线，并核对提交后状态。

## V6：建立每段独立的检查、输入拷贝与恢复责任

**文件：** 建议新增 `capture/segment_inspections.py` 与 `persistence/repositories/segment_inspections.py`；修改 `outputs/qualification.py`、拷贝授权及仓储、`capture/media_flow.py` 的可共享字节操作、SQL、枚举、事件、历史归属、文件维护装配。

**输入／交付：** 消费 V2 的固定成员与 V5 的指标端口，交付 `SegmentInspectionRequest`、`SegmentInspectionStep` 和异步 `inspect_segment()`。检查按来源动作与真实成员唯一归属，输入副本及实际工具记录各有身份；不反复替换单原片 `recording_processing` 的来源。

- [x] **步骤 1：写多段身份及资格失败测试。** 建议 `apps/camctl/tests/integration/capture/test_segment_inspections.py`：同动作两成员产生两个检查和输入副本；重送原申请复用原身份；替换源、集合、目标或预算被拒绝。完整性未确认的副本不运行媒体工具。
- [x] **步骤 2：运行新文件，核对独立检查所有权。** 审计 `FileCandidate` 的 item／processing 分支、`file_copies.processing_id UNIQUE` 及复制流程的所属规则，不能只删除唯一约束。
- [x] **步骤 3：实现独立检查所有权与拷贝分支。** 每项拷贝明确属于 delivery、单原片处理或分段检查中的一种；保存原成员、精确设备源、目标主机文件、预算及进度。复用现有 prepare、transfer、complete 的字节与校验内核，主文件及操作历史保持独立。
- [x] **步骤 4：实现单段可靠执行。** `inspect_segment()` 先完整拷贝并可靠核验，再调用 V5 工具，按原操作保存主视频指标及实际失败；部分字节、工具退出、指标未知、明确媒体问题分别表达。取消、空间不足及调用收场沿现有有限规则处理。
- [x] **步骤 5：接通进程内和重启接续。** 输入进度或媒体结果提交未知时保留原键和申请，核实后继续；已可靠保存的检查不重跑，不重置预算；来源提交前有效检查可续传，已有来源终态时进入 V7 的保留与清理判断。
- [x] **步骤 6：同步历史及维护资格。** 新检查、内部 copies 与工具记录归原动作；输入文件仍为独立历史对象。使用 `apps/camctl/tests/integration/history/test_action6_video_result_history.py`，验证快照、正反向恢复及原 H 文件事实。维护器在来源检查或提交未可靠结束时保留副本，不能因普通 owner 分类不识别而删除。
- [x] **步骤 7：分别运行 capture、history 及相关 outputs 的复制门槛，检查登记生成。** 原单文件 `test_recording_media_link.py` 和既有复制测试继续通过；本步的检查能力在 V7 的正式取回及清理接缝完成后，由 V8 启用 Action6 异常编排。

```bash
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/capture/test_segment_inspections.py apps/camctl/tests/integration/capture/test_recording_media_link.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/history/test_action6_video_result_history.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/outputs/test_copy_segments.py -q
```

- [x] **步骤 8：完成阶段提交。** 按全局提交方式审查实际 diff 和验证证据，更新本任务原步骤进度，仅提交本任务文件；记录提交标识、通过范围与剩余接线，并核对提交后状态。

## V7：共同发布来源，并闭合等待取回的副本复用与清理

**文件：** `capture/segment_finish.py`、`persistence/repositories/{segment_transactions,outputs,history}.py`、`outputs/{obtain_flow,qualification,read_attempts,work_files}.py`、`bootstrap/work_file_assembly.py`；建议新增有限复用关系及 `persistence/repositories/segment_copy_reuse.py`。

**输入／交付：** 消费 V2 的公共完成端口、V4 的来源选择、V6 的完整副本，提供 `BoundCheckCopy` 和 `read_bound_check_copy()`。交付来源共同事务、原等待者关联、真实取回读本地副本和最终维护清理；V8 可直接发布实际检查结果。

- [x] **步骤 1：写事务截点失败测试。** 在 `apps/camctl/tests/integration/outputs/test_segment_copy_reuse.py::test_only_obtains_waiting_at_the_source_publication_boundary_are_bound` 验证：来源提交前已有两个 PENDING 正式选择，提交后才进入等待的第三个选择；前两者被原事务绑定，第三个不绑定。取回尚无固定 items 时按原 selection 身份关联，不能假定等待者已经完成产物选择。
- [x] **步骤 2：写取回与清理失败测试并分别运行。** 建议 `apps/camctl/tests/integration/outputs/test_segment_copy_reuse.py` 及 `apps/camctl/tests/unit/outputs/test_segment_copy_reuse.py`。两个取回有不同 delivery／目标文件；第一个结束时副本继续保留；读副本期间维护器不能删除源；全部相关责任结束后才可清理。
- [x] **步骤 3：扩展来源终态的有界整组事务。** 同时提交产物、视频范围、适用覆盖、动作终态、等待转换及来源发布截点。按[存储专题](../../camctl/database/segments.md#生产端口与后续分工)，从该完整 H 按页恢复实际已运行且等待来源的 PENDING 选择及准确副本关系。原操作键保存完整有限申请；核对同源成员、真实 output、完整副本、原依赖及适用等待者。
- [x] **步骤 4：实现原申请核实与取消次序。** 事务已提交但回执未知时，从原整组恢复原 H、产物和等待者；不重新查询后来的等待集合。取消先于来源终态时采用取消处置，来源终态先提交时保持原结果。复用资格与默认／明确 ID 的实际选择分别核对。
- [x] **步骤 5：接通取回授权和本地读取。** 保持设备原片的 `output_id` 与正式源身份，将已绑定完整检查副本作为可核验的读取源，仍由原 item 创建独立 delivery、交付文件及 copy。副本不能可靠使用时沿同一原片、delivery 和累计预算核实或读设备；预览读取真实 LRF，后到取回读设备源。
- [x] **步骤 6：保护读取源和目标的实际生命周期。** `run_owned_read()` 等实际操作同时登记检查源文件与交付目标的使用资格；文件操作、线程、读取会话及工具真正收场后才释放。必要控制可继续推进，文件不能在仍被读取时清理。
- [x] **步骤 7：实现保留与清理决策。** 绑定取回全部终态且实际操作收场才清理完整副本；没有绑定用途的完整副本与来源提交后无检查责任的部分副本及时清理。来源提交未知先核实，终态来源的部分检查不再续传。清理失败保存后续责任，不改写已有动作结果，不清理相机原片、照片、LRF 或正式交付文件。`test_segment_copy_maintenance.py` 的3项真实 Action6 组合已验证无等待者、实际文件任务保护、删除失败重开重试，以及等待者取消收场后释放；提交未知、部分检查及冷恢复组合已由组件门槛和 V10 安装版恢复矩阵核对。
- [x] **步骤 8：接通历史归属及冷恢复。** 来源拥有发布截点和复用关系，取回拥有自身使用与交付进度，文件独立恢复。新增关系纳入登记、事件、快照、有界读取和维护发现；测试等待者取消／失败、无等待者、清理失败与源文件后来不可用。
- [x] **步骤 9：分别运行 persistence、outputs、history 的相关门槛。** 扩展既有 `test_segment_transaction_batches.py`、`test_work_file_result_boundary.py` 及复用新测试。实际读取和维护组合必须通过，纯保留函数通过不能替代真实文件保护。

```bash
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/outputs/test_segment_copy_reuse.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/persistence/test_segment_transaction_batches.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/outputs/test_segment_copy_reuse.py apps/camctl/tests/integration/outputs/test_work_file_result_boundary.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/history/test_action6_video_result_history.py -q
```

- [x] **步骤 10：完成阶段提交。** 按全局提交方式审查实际 diff 和验证证据，更新本任务原步骤进度，仅提交本任务文件；记录提交标识、通过范围与剩余接线，并核对提交后状态。

## V8：接入 Action6 普通与原生延时的异常恢复

**文件：** 公共分段编排与建议的 `capture/segment_inspections.py`、`capture/handlers.py`、`bootstrap/capture_assembly.py`、有限核实与结论恢复、公共结果发布。

**输入／交付：** 消费 V4—V7，交付 Action6 的完整实际内容策略。普通目标采用主视频时长，native 采用主视频帧数；两类动作共用检查与范围机制，保留各自动作的控制和取消合同。

- [x] **步骤 1：写真实处理器异常集成失败测试。** 建议 `apps/camctl/tests/integration/capture/test_action6_segmented_recovery.py`。60 秒目标、18／27／22／40 秒仅检查前三段；第三段拷贝或指标未知只固定前两段；第一段未知形成空前缀；完整可靠全部不足才确认不足。
- [x] **步骤 2：写原生延时和明确错误反例并运行。** 151 帧目标、60／60／45／20 帧仅检查前三段；所需照片缺失不能被视频达标覆盖。真实生命周期用例已由 native 处理器进入异常检查；`test_action6_known_failure_precedence.py` 验证照片与视频交错、可靠视频缺口、零字节及明确 `no_video_stream` 的优先失败，并保留可靠前缀；与工具异常未知对照共5项通过。只有部分已检查量不足不能宣称全部内容短缺。
- [x] **步骤 3：实现异常判定与检查推进。** 控制和文件核实后，从冻结起点逐段调用 `inspect_segment()`，保存累计量及首个中断位置，达到目标即停止为选段检查后段；下一指标未知时不跨越。正常登记接续、已有终态和取消先分派，不能误启内容检查。
- [x] **步骤 4：实现来源终态与范围决策。** 使用 V7 一次发布所有合格原片和固定视频范围。可靠控制但覆盖未知可成功；没有控制或内容成功依据则失败；明确失败优先。来源失败不把非空前缀取回改为失败，空／未知视频范围不抑制独立照片处理。
- [x] **步骤 5：审计全部生命周期入口。** 核对下方入口表的普通、native、启动／绑定／外部失败、停止耗尽、时钟受限、取消、已有结论、提交未知及冷恢复分区。原生延时取消保留全部合格完成视频及照片，不继续检查以追求原目标。
- [x] **步骤 6：增加装配与恢复组合。** 使用 capture 目录的 `test_action6_lifecycle_recovery.py`、`test_action6_budget_cold_recovery.py` 和 `test_action6_segmented_recovery.py`，并由 V10 安装版恢复矩阵验证进程中断。在输入部分落盘、拷贝完成结果未保存、媒体结束、覆盖保存及来源提交前后分别中断。核对 START／STOP、预算、检查身份、范围及副本使用不会被重置或重复。
- [x] **步骤 7：分别运行新 capture 与 bootstrap 文件，补跑同模式单文件和 OSMO 关键回归后提交。** 此步结束应可从实际处理器取得正常／异常的来源、范围、独立取回及正确清理，不等待 V9 才补齐执行所需持久化或归属。

```bash
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/capture/test_action6_segmented_recovery.py apps/camctl/tests/integration/capture/test_record_segment_scope_recovery.py apps/camctl/tests/integration/capture/test_native_segment_completion.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/capture/test_action6_lifecycle_recovery.py apps/camctl/tests/integration/capture/test_action6_budget_cold_recovery.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/bootstrap/test_action6_capture_completion.py -q
```

### 必须逐项核对的生产入口

| 责任 | 当前代码入口及改后要求 |
| --- | --- |
| 普通正常、跨运行及受限恢复 | `capture/handlers.py` 的 `_record_handler()`、`_reconcile_recording()`、`_advance_recording_outcome()` 及受限时钟分支，按冻结模式和特化分派。 |
| 共同结束、基准／START／外部失败 | `_finish_capture()`、`_binding_failure_files()`、分段 baseline／start／external failure、`resume_closed_start()`；不得把所有有策略任务送入 OSMO。 |
| 停止耗尽和取消 | `_settle_stop_exhausted()`、`_finish_canceled_capture()`、`finish_segmented_cancellation()`；停止未知与取消内容处置分别保存。 |
| 原生延时正常、取消及原结论恢复 | `_timelapse_handler_once()`、`_close_canceled_timelapse()`、native 结果登记和已保存结论恢复；不套普通计时、下界或普通录像专用仓储身份。 |
| 原申请和工作文件恢复 | 待保存检查、来源共同事务、取回原选择、实际读取、维护发现；先核实原责任，再继续，历史回放不产生外部操作。 |

实现时按当前函数名重新定位，并在代码评审中逐项记录生产调用方与对应测试。搜索到标识符不代表实际路径已经接通。

- [x] **步骤 8：完成阶段提交。** 按全局提交方式审查实际 diff 和验证证据，更新本任务原步骤进度，仅提交本任务文件；记录提交标识、通过范围与剩余接线，并核对提交后状态。

## V9：完成实际覆盖的有界报告与同一历史边界重建

**文件：** `reporting/bounded_generation.py`、`contracts/public_projection.py`、相关历史读取；`docs/camctl/database/report-dependencies.json`、事件与归属登记、客户端实际覆盖消费及测试。

**输入／交付：** 消费已保存的检查与来源结果，按 V3 准确协议生成普通／native 报告。执行所需的新记录归属已由 V6、V7 实现，本步完成公开路由、增长展开及跨层字节一致性。

- [x] **步骤 1：写普通／native 实际报告失败测试。** 建议 `apps/camctl/tests/integration/reporting/test_video_coverage_report.py`：reached、confirmed short、unconfirmed、not performed；成功但未知、失败但取回成功、未知与零；目标和累计量按正确单位，OSMO 下界不进入实测结果。
- [x] **步骤 2：写冻结 H 的组合反例并运行。** `apps/camctl/tests/integration/history/test_action6_video_result_history.py` 从真实异常处理器固定 H，在 H 后完成已授权的取回并取得真实 copy／target／delivery 摘要，再删除设备原片与清理检查副本；旧 H 仍恢复同样的来源、范围、覆盖与文件身份，且不包含后续摘要。改变页批次和从初始、快照、当前状态恢复的方向后，报告原字节一致；该文件1项通过。后到取回的安装版组合由 V10 核对。
- [x] **步骤 3：登记公共投影及报告路由。** 成功与失败动作、普通和 native 均有适用覆盖路径；公开变化推进原动作水位，内部检查进度不直接制造公开结果。已发布视频范围与逐文件检查事实沿真实关系取得，不能只从 `recording_processing` 或普通录像类型判断。
- [x] **步骤 4：实现增长关系的同 H 有界展开。** 在 `_report_plan`／`report_tokens` 对接的实际生成路径中分页展开已检查成员、范围、缺口和实际 output 引用；有限结果头不嵌入增长数组。关联缺失、数量矛盾及历史损坏按状态库错误处理。
- [x] **步骤 5：用真实生成报告验证客户端导入与展示。** 客户端 segment 报告、知识合并、结果模型及浏览器测试已接入公共视频范围；真实 handler 生成的 `confirmed_short` 失败来源与成功 delivery 报告，在报告先到及媒体先到两种顺序中均通过 `Files.upload`、摘要核对、React 展示和累计 ACK。浏览器专属文件7项通过；两设备位置、范围外明确 ID、照片保留及其他覆盖状态由既有组件和 V10 安装链分别核对。
- [x] **步骤 6：运行分层门槛。** 分别运行 reporting 和 history 新文件；运行 `node --disable-warning=ExperimentalWarning scripts/check-report-dependencies.mjs`、对应登记集成测试，以及客户端相关单元、集成／浏览器测试。根 Schema、安装资源和客户端生成类型保持一致。

```bash
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/reporting/test_video_coverage_report.py -q
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/history/test_action6_video_result_history.py -q
node --disable-warning=ExperimentalWarning scripts/check-report-dependencies.mjs
node --disable-warning=ExperimentalWarning --test tests/integration/report-dependenc*.test.mjs
cd apps/client
npm exec -- vitest run tests/unit/video-coverage.test.ts tests/unit/segment-reports.test.ts tests/unit/segment-result-model.test.ts
npm run build
npm exec -- vitest run tests/integration/segment-reports.test.ts tests/integration/browser-segment-results.test.ts
cd ../..
```

- [x] **步骤 7：完成阶段提交。** 按全局提交方式审查实际 diff 和验证证据，更新本任务原步骤进度，仅提交本任务文件；记录提交标识、通过范围与剩余接线，并核对提交后状态。

## V10：通过真实安装版、C host 与客户端验证模式矩阵

**文件：** 建议新增 `tests/integration/test_action6_builtin_segmented_roundtrip.py`；扩展现有 Action6／OSMO 普通、native、能力声明联合用例、受契约约束的 ADB／媒体进程替身及 `tests/integration/client_driver.ts`。测试说明随入口更新。

**输入／交付：** 消费 V1—V9 的完整生产实现，以仓库外构建和安装的 wheel、真实 CLI／SQLite、C host 和客户端验证。替身仅控制设备／工具边界；不能用测试直接构造成功终态代替生产接线。

- [x] **步骤 1：构造完整模式与动作矩阵。** 两设备 × 两模式 × 普通／native；同驱动不同设备配置、配置切换后旧定义／新请求、默认省略配置均独立核对。单文件普通一份成功、多份数量错误；native 保留原最低数量合同；OSMO 分段 fixture 明确配置模式。
- [x] **步骤 2：加入 Action6 正常多段与照片链。** 经客户端导出、C host 递交、安装版受理和执行、独立默认／预览／明确 ID 取回、ready／processing 领取、客户端实际导入及 ACK。正常任务的检查拷贝与媒体探测调用为零。
- [x] **步骤 3：加入实际内容与等待者矩阵。** 不等长达到、未知前缀、完整不足、151 帧、缺必需照片；两个已等待取回及一个后到取回。保存每个正式产物、delivery、真实读取源、摘要、覆盖、范围与副本清理结果。
- [x] **步骤 4：在真实边界注入恢复。** 检查副本进度保存、媒体结果保存、来源事务提交回执、取回读取和清理前后安排确定故障点；恢复不重拍、不重复可靠 STOP、不改变来源等待者截点。故障点只改变实际边界结果，不直接注入业务成功依据。
- [x] **步骤 5：准备前置并顺序运行联合用例。** 按跨组件说明准备 Python 3.11、uv 离线缓存、客户端依赖与 Chromium、C 构建工具及对应进程工具。根执行下列文件，各用例完整结束并保留命令、日志、退出码及安装资源证据。

```bash
apps/camctl/.venv311/bin/python -m pytest tests/integration/test_action6_builtin_segmented_roundtrip.py -q
apps/camctl/.venv311/bin/python -m pytest tests/integration/test_action6_segmented_media_fixtures.py -q
apps/camctl/.venv311/bin/python -m pytest tests/integration/test_action6_segmented_recovery_roundtrip.py -q
apps/camctl/.venv311/bin/python -m pytest tests/integration/test_action6_builtin_demo_roundtrip.py -q
apps/camctl/.venv311/bin/python -m pytest tests/integration/test_action6_builtin_timelapse_roundtrip.py -q
apps/camctl/.venv311/bin/python -m pytest tests/integration/test_osmo_builtin_record_segmentation_roundtrip.py -q
apps/camctl/.venv311/bin/python -m pytest tests/integration/test_osmo_builtin_timelapse_roundtrip.py -q
apps/camctl/.venv311/bin/python -m pytest tests/integration/test_device_capability_declarations.py -q
```

- [x] **步骤 6：核对证据边界。** 安装版实际包含新协议与模式说明；报告及媒体上传分别保留实际导入凭据，累计确认水位持久化。替身通过不证明真实固件分段或真实媒体，V12 保持独立。

- [x] **步骤 7：完成阶段提交。** 按全局提交方式审查实际 diff 和验证证据，更新本任务原步骤进度，仅提交本任务文件；记录提交标识、通过范围与剩余接线，并核对提交后状态。

## V11：完成全量软件门槛、发行物和部署交接

**文件：** 受影响源码、权威资源、运行及配置说明、数据库专题、组件验证记录；模式样例与包内演示配置随实现更新，不复制权威清单。

- [x] **步骤 1：完成任务与规格覆盖核对。** 按下方验收映射确认每项已由真实生产路径和测试覆盖，审查所有显式模式分派及设备假设。旧 OSMO 证据按原范围保留，新增结果记录本次版本、环境与原日志。
- [x] **步骤 2：运行完整 Python 单元与组件集成门槛。** 单元命令 `apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit -q`，最终生产基线 `33687b5` 显式固定源码后7065项通过、1项平台跳过。16个组件目录均已有完整范围记录，具体版本、分批范围及后续受影响回归见组件验证记录；按测试指南分目录启动进程，不合并为单一pytest进程，同工作树内串行。下面脚本按实际测试目录发现，避免另外维护完整目录清单。

```bash
apps/camctl/.venv311/bin/python - <<'PY'
from pathlib import Path
import subprocess
import sys

root = Path('apps/camctl/tests/integration')
for directory in sorted(root.iterdir()):
    if directory.is_dir() and any(directory.rglob('test_*.py')):
        subprocess.run([sys.executable, '-m', 'pytest', str(directory), '-q'], check=True)
PY
```

- [x] **步骤 3：运行客户端和公共登记门槛。** 客户端完整 `VITEST_MAX_WORKERS=2 npm test` 已通过3212项单元、1559项集成／浏览器，并完成类型检查和实际构建；根目录下列只读检查、115项登记接缝测试和27项历史SQL测试均通过。修改登记时先使用所属生成脚本的写入模式，再运行只读模式核对，不能手工维护生成区。

```bash
node scripts/check-protocol.mjs
node --disable-warning=ExperimentalWarning scripts/check-report-dependencies.mjs
node --disable-warning=ExperimentalWarning scripts/check-event-transitions.mjs
apps/camctl/.venv311/bin/python scripts/check-database-spec.py
apps/camctl/.venv311/bin/python scripts/sync-history-object-sql.py
apps/camctl/.venv311/bin/python scripts/check-config-consumption.py
apps/camctl/.venv311/bin/python apps/camctl/scripts/sync_resources.py --check
node --disable-warning=ExperimentalWarning --test tests/integration/report-dependenc*.test.mjs tests/integration/event-transitions.test.mjs
apps/camctl/.venv311/bin/python -m unittest discover -s tests/integration -p 'test_database_history_sql.py' -v
```

- [x] **步骤 4：审查发行候选和部署资料。** 归档 V10 验证过且与最终源码、权威资源一致的 wheel 及摘要；之后若有生产或协议修改，重建并重跑受影响安装链。包内配置和正式演示明确选择所需模式；能力、配置参考与已实现情况一致，原单文件演示仍可执行。不得用源码目录可用代替安装物可用。
- [x] **步骤 5：完成文档及最终分支审查。** 执行 `node scripts/check-doc-links.mjs`、独立标题锚点／失效引用检查、正式文档临时目录引用检查及 `git diff --check`。独立整体审查应沿配置到交付和恢复核对，不以阶段提交数代替业务完成度。
- [x] **步骤 6：保存验证证据与交接说明。** 在组件验证记录写实际命令、退出码、完整目录结果、已知未验范围及发行摘要；更新本计划原步骤，交付可直接部署的资料与 V12 待验项。

- [x] **步骤 7：完成阶段提交。** 按全局提交方式审查实际 diff 和验证证据，更新本任务原步骤进度，仅提交本任务文件；记录提交标识、通过范围与剩余接线，并核对提交后状态。

## V12：目标 ARM 与实机媒体验收

**前置：** V11 的发行物、目标主机环境、固件及控制合同满足相应设备条件，并具有届时的实际执行安排。软件阶段不自动连接、拍摄、删除或构造物理故障。

- [ ] **步骤 1：核对安装和配置。** 记录 ARM Python／SQLite／媒体工具、安装物摘要、目标设备与固件、每相机模式及安装版能力导出，确认两个模式分别使用适合其产物的目标固件。
- [ ] **步骤 2：验证单文件与分段的普通／native 正常链。** 每次记录唯一控制、原参数、等待、完整基准及结果页、全部源文件和独立交付，逐文件核对长度与 SHA-256。客户端实际导入与 ACK 单独留证。
- [ ] **步骤 3：验证 Action6 异常实际内容选段。** 按预先确定的异常安排获取不等长分段，普通核对主视频时长，native 核对目标151帧等采样与实际帧数；选中最后一段保持完整，范围外原片保留。实际恢复核对已等待／后到取回和清理责任。
- [ ] **步骤 4：验证 OSMO 特化媒体。** 分别核对默认范围及全部保留原片的两路主视频、整体100毫秒默认短缺容限、音频、解码及空尾诊断；不借范围外原片补足默认覆盖。原生延时端点与段间依赖按实际证据记录。
- [ ] **步骤 5：保存独立验收结论。** 区分安装、控制、文件、媒体、客户端及 ACK，记录观察事实与未知；物理断电、故意缺段及独立播放作为专项安排。依据仍未知时不补拍或补发控制命令来替代原失败证据。

## 规格到验收的映射

| 规格责任 | 实现与组合验证 |
| --- | --- |
| 部署选择、闭合配置、原任务冻结、无旧格式负担 | V1；V10 的模式、切换及安装版受理。 |
| 单文件普通／native 原行为、正常／失败／取消／恢复独立 | V1、V2；V8 全入口审计；V10 的两设备矩阵。 |
| 公共边界、设备身份、连续性、当前扫描及跨轮保留 | V2、V4；原 OSMO 存储／历史回归；V8 的未知和缺口。 |
| 正常全部视频、照片独立、预览同范围、明确 ID、组／计划 | V2、V4；V7 选择授权；V10 实际取回。 |
| 实际主视频时长／帧数、最短前缀、未知与确认不足 | V5、V6、V8；V10 异常安装链。 |
| 明确失败优先、控制与内容成功依据、来源失败但取回成功 | V8；V9 报告；V10 来源／取回组合。 |
| 每段独立检查、有限预算、实际收场、部分副本恢复 | V6；V7 保留；V8 冷恢复与取消。 |
| 来源共同提交、等待截点、独立副本、后到请求、清理 | V7；V8 实际生产者；V10 提交未知与多等待者。 |
| 唯一归属、分页、同 H、旧报告字节不变 | V2、V6、V7 同步登记；V9 端到端重建；V10 进程恢复。 |
| 公共协议、覆盖报告、客户端消费及媒体与 ACK 分离 | V3、V4 正常投影、V9 实际覆盖；V10 真实客户端。 |
| OSMO 下界、N、空尾、双路媒体与采样端点边界 | V2 软件回归；V10 安装版；V12 设备与媒体。 |

## 当前进度与交接记录

2026-10-23：V1—V11的生产实现、软件门槛、安装版逻辑矩阵、最终发行物与证据归档已完成；V12保持独立待验。详细命令、环境、实际结果和发行证据由[组件验证记录](../../camctl/verification.md#视频结果模式与-action6-分段的软件验证2026-10-23)唯一维护；本节只登记原任务的交付状态和交接边界。

| 原任务 | 已交付的生产路径 | 当前交接状态 |
| --- | --- | --- |
| V1、V2 | 每相机模式配置、受理冻结、两设备独立单文件路径、公共分段集合及OSMO特化；冻结模式贯穿结果读取，单文件LRF保持原配对。 | 实现及组件验证完成。 |
| V3、V9 | 四种覆盖结论、量纲、根Schema、生成类型、客户端消费及有界报告；真实取回、源删除和副本清理后恢复旧H的原报告字节。 | 实现及客户端、历史验证完成。 |
| V4 | Action6普通与native正常分段、全部合格视频、独立照片、真实LRF、默认及明确ID取回；正常没有检查拷贝或探测。 | 实现及组件、安装版正常链完成。 |
| V5、V6 | 可靠主视频时长及整数帧、精确连续累计、每段独立检查和完整副本、原预算及实际工具结果冷恢复。 | 实现及真实媒体端口、组件恢复验证完成。 |
| V7 | 完整来源发布H固定原等待者，两次独立交付、后到设备读取；原copy换源、重拷和剩余预算；实际读取保护及清理恢复。 | 组件及安装版各故障边界已验证，最终候选普通／native代表场景通过。 |
| V8 | 两类异常入口、最短可靠前缀、未知与确认不足、明确失败优先；普通与native各自取消、停止耗尽、提交未知及冷恢复。 | 实现及完整capture、bootstrap范围完成。 |
| V10 | 两设备×两模式×两动作及配置切换；正常及异常实际媒体、C host、CLI、SQLite、客户端导入和累计ACK。 | 完整逻辑矩阵、最终生产基线代表场景及证据归档完成。 |
| V11 | 最终完整Python单元、16个组件目录、完整客户端、公共登记及结构检查；共享类型职责和实际模块路径已同步。 | 最终发行字节核对、独立源码与文档审查、链接和锚点检查及归档完成。 |
| V12 | 目标ARM安装、真实固件控制、真实媒体及现场异常。 | 待用户准备设备，所有步骤保持未完成。 |

V7以 `segment_sets.published_event_id` 所属完整事务为固定截点，从该H恢复真正已运行且等待来源的取回。仅已受理、后到或尚未等待来源的取回不取得复用资格。

### 软件交付与目标环境交接

1. 最终发行物、逐文件清单、组件门槛及两设备安装证据位于组件验证记录登记的统一归档目录。交付时按该记录核对wheel摘要；运行时按每台相机的固件产物选择 `single_file` 或 `segmented`，已受理任务继续使用原定义。
2. 完整组件、安装矩阵及修正后的定向验证分别保留原版本、实际命令和退出码；原失败及主动中断记录保留，不把分阶段结果表述为同一提交的一次运行。
3. V12在设备准备后独立执行；现场前按原执行定义、配置、固件及发行摘要准备，不自动拍摄、删除或构造物理故障。
