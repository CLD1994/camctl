# Action6 与 OSMO 360 II 延时参数与同源契约执行计划

> **执行者要求：** 使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans` 实施；先读[统筹计划](2026-10-16-osmo360ii-timelapse.md)，按以下复选框交付。

**目标：** 用一份权威定义表达两款相机共同的时间允许值和严格组合关系，使各自候选校验、受理及能力说明保持一致，并保留精确数值输入、型号专属曝光与输出以及原请求事实。

**架构：** 时间参数从完整命令样例中解耦；标准 Schema 从参数定义生成。候选命令缺口与非法参数分别处理，正式能力仍由完整任务工厂控制。

**技术栈：** Python 3.11、Decimal、JSON Schema Draft 2020-12、现有 Catalog、受理及历史持久化。

**规格：** [参数限制](../../hardware/camera-control-handoff.md#间隔与采集时长限制)、[同源 Schema](../../architecture/camera-capabilities.md#拍摄参数的-json-schema)、[只读执行定义](../../camctl/database/execution-definitions.md)、[输入数值](../../camctl/data-types.md)。

## 全局约束

完整遵守[统筹计划的全局约束](2026-10-16-osmo360ii-timelapse.md#全局约束)。本领域改变两款相机新请求使用的共同时间契约，不迁移已有请求、执行定义或历史；不绑定生产任务工厂，不推导未知设备负载。Action6 保留视频、video+RAW、video+JPEG 输出以及手动 ISO 800 / Auto 结构；OSMO 保留仅视频及其手动 ISO 800 / Auto 结构，时间条件不隐式切换这些选择。

## 上下文与建议文件

- 生产起点：`apps/camctl/src/camctl/devices/drivers/adb_cameras/definitions.py` 的 `_timelapse_schema`；`commands.py` 的 `TimelapsePreset`、`settings_for`。
- 同源消费者：`devices/catalog.py`、`acceptance/rules.py`、`acceptance/definitions.py`、`persistence/repositories/acceptance.py`、`capture/models.py`；优先验证，不预设都需要修改。
- 精确数值：`contracts/json_values.py` 的 `parse_exact_json`、`contracts/schemas.py`、`acceptance/schema.py` 的 `validate_precise`。
- 建议新增 `adb_cameras/timelapse_time_parameters.py`，只维护两款共用的精确时间域及条件生成，由各型号组合自己的参数类型、输出和曝光。建议新增 `apps/camctl/scripts/render_timelapse_time_parameters.py`，提供 `--write` 和 `--check`，处理文档明确标记的参数生成区及 D1A 所需独立诊断资源，保留原始命令和设备观察。
- 既有测试：`apps/camctl/tests/unit/devices/test_adb_camera_parameters.py`、`test_adb_camera_commands.py`、`test_action6_record_contract.py`；组件集成 `acceptance/test_adb_camera_definitions.py`、`devices/test_adb_camera_registration.py`。

## 输入分类与结果

先校验字段结构与数值域，再判断时间组合；执行能力是否齐备是另一项判断。

输入或目录状态|结果
-|-
两个值均属于允许域，且时长严格大于间隔|时间参数通过，包括最短允许间隔；继续曝光、输出及其他完整参数校验。
时长等于或小于间隔|参数校验失败；不自动延长时长或缩短间隔。
值不在允许域，或为布尔、字符串、显式空值、对象、数组|参数校验失败；不转换类型，不用默认值覆盖。
原字段缺失|按必填字段报错；能力说明的默认注释不自动写入输入。
参数符合 Schema，但对应候选命令尚未登记|明确暴露命令映射缺口；不抛出意外的 `StopIteration`，不改报为用户参数非法，不生成猜测负载。
新请求使用 Action6 旧样例的 5400/6000 秒时长|按共同时间域拒绝，不将原样例存在解释为当前参数合法，也不替换成相邻允许时长。
候选参数完整，但生产任务工厂尚未齐备|正式 `describe` 和受理目录继续过滤该任务。
已受理原请求重送，或从保存事实恢复|复用原输入、生效参数和只读执行定义；不以当前 Schema 重建或改写原要求。

## P1 权威参数定义、Schema 与候选缺口

**输入：** 用户提供的完整参数域与严格关系；统筹计划建议的 P/C Schema 子集。

**交付接口：** 保留 `candidate_capabilities(driver_id: str) -> tuple[ActionCapability, ...]` 和 `settings_for(driver_id, action_type, params)` 的消费边界。新权威模块建议提供精确间隔域、整数时长域及时间属性与关联条件；各型号的完整 Schema 组合此结果及自身的 `type`、`outputs`、`exposure`，不复制完整时间列表。内部命名可调整，所有消费者同步。

- [x] 对两款参数类型分别增加最短间隔、合法长间隔、等值、反向大小、域外值、缺失及错误类型分区；从共同权威域穷举时间组合，预期用独立数学比较核对，每款合法数均为 134。独立边界至少包括 `1/300` 通过，`0.5/300` 拒绝，`300/300`、`3600/3600`、`3600/300` 拒绝，`3600/7200` 通过，间隔 `7`、时长 `60`、Action6 旧时长 `5400/6000` 拒绝；`1.00000000000000001` 不匹配一秒枚举，`1.0`、`1e0` 与一秒等价。
- [x] 对两款分别增加“Schema 合法但没有映射”的命令用例；明确检查未产生设备调用或猜测命令，且错误不再由 `next()` 意外耗尽。仍符合新规则的完整映射继续通过；Action6 的 5400/6000 秒字面量保留独立编码资料及原受理事实的解释能力，新请求候选入口拒绝这些输入，原冻结事实的派发由 P2 验证。P1 的派发夹具优先使用仍合法且已有映射的8秒/1800秒 Auto；新增手动或最短派发覆盖在 D1 对应映射齐备后验收，不为使 P1 通过而猜测命令。产物夹具的完整调整由 E1 维护。
- [x] 运行 `P apps/camctl/tests/unit/devices/test_adb_camera_parameters.py apps/camctl/tests/unit/devices/test_adb_camera_commands.py -q`，确认目标失败来自参数或缺口行为。
- [x] 实现两款共用的时间权威来源：小数使用精确数值，`interval_s` 为 `number` 加枚举，`duration_s` 为数学整数加枚举；生成标准条件表达严格关系。各型号组合自身输出及手动 ISO 800 / Auto 结构，拒绝分支混用和额外字段。时间规则不再由完整负载表或曝光分支限定，两款的输出结构不相互扩散。
- [x] 在扩大两款候选 Schema 的同一交付中闭合 `settings_for` 缺口：沿既有未声明能力/规则错误的内部分类报告，不新增公共错误码；符合新规则的已知完整映射继续查表，未知映射分别交给 D1。候选说明同步表达共同时间限制，删除以旧完整组合表描述合法域的限制。
- [x] 重跑上述单元命令并核对原输入未被校验器修改；与 C1 核对实际生成条件属于其受支持子集。检查 diff 后提交本项参数和测试。

关键独立断言示例：

```python
assert parsed_params["interval_s"] == Decimal("1")
assert len(accepted_time_pairs) == 134
assert capability.defaults == {}
assert params_after_validation == params_before_validation
```

这里的 `accepted_time_pairs` 来自对所有权威域候选实际运行 Schema；判断预期使用数学大小关系，不调用被测 Schema 生成函数判定合法性。测试不复制完整允许值清单。

### P1 验证记录（2026-10-16）

在独立工作区的 Python 3.11.16、SQLite 3.53.1 环境中，使用本树锁文件和独立 `.venv311` 完成验证。以下测试均使用 `uv run --frozen --project apps/camctl --group test --python 3.11 pytest`，并显式设置本树 `UV_PROJECT_ENVIRONMENT`。

- 参数与命令两文件先得到 4 项失败、266 项通过，目标失败为半秒域外值仍被接受和权威域数量不符。
- 最终运行 `apps/camctl/tests/unit/devices/test_adb_camera_parameters.py apps/camctl/tests/unit/devices/test_adb_camera_commands.py -q`，270 项通过。两款各自穷举权威时间域，均接受 134 对组合；数学比较独立于 Schema 条件生成。`1/300` 通过，`0.5/300` 被拒绝；`1`、`1.0`、`1e0` 等值，邻近非等值被拒绝。
- 最终运行 `apps/camctl/tests/unit -q`，5314 项通过、1 项跳过。两项既有 asyncio 标记警告分别来自 `test_source_stream_protocol_shape` 和 `test_local_exit_is_exclusive`。
- 共同权威模块为 `adb_cameras/timelapse_parameters.py`，当前提供 18 个间隔与 8 个时长。间隔保持 `number` 枚举；根 `allOf` 使用显式必填 `duration_s` 的 `if`，并在 `then` 对间隔施加 `exclusiveMaximum`。独立曝光 `oneOf` 与各型号输出保持分离，客户端消费该标准形状；客户端验收由对应任务完成。
- `settings_for` 对合法但没有完整映射的参数抛出既有 `CapabilityNotDeclaredError`；两款驱动测试确认此时设备调用数为零。旧完整负载保留，新请求拒绝域外时长；原冻结事实派发由 P2 验证。
- `node scripts/check-doc-links.mjs` 通过，核对 4530 个本地文件链接；改动行涉及的 14 个本地标题链接均已核对实际标题。生产任务工厂及能力过滤条件保持，软件结果不表示设备编码、ARM 或真实拍摄已经验收。

## P2 原输入、冻结执行定义及能力过滤

**输入：** P1 的候选定义。

**交付接口：** 正常 Catalog、受理和 `describe` 的关联不变；测试通过专用任务工厂提供受约束的可执行合同。原执行定义仍由 `CaptureTaskFactory(effective_params)` 首次构造，恢复只读已保存值。

新请求和新诊断通过当前 Schema 校验后才允许生成候选命令。正式驱动控制接收的是公共处理器从原动作读取的 `effective_params_json` 及原绑定、尝试和派发资格，编码不能重新用当前候选 Schema 改判首次已受理的参数。建议在命令模块分开“当前候选校验入口”和“可信原生效参数编码入口”，后者保留必要的结构、型号及映射检查，不增加外部可选的跳过校验开关。原来具有依据的完整负载继续可被原事实解释，真正缺少映射时明确报告缺口；首次受理及新诊断不能借该内部入口接受域外值。

- [ ] 在 `acceptance/test_adb_camera_definitions.py` 对两款测试合同分别增加精确 `1/300` 受理及历史读取用例：原始参数、生效参数保留数值，执行目标为 `target_duration_ms == 300000`；间隔留在原生效参数，不为它新增不必要的执行定义列。
- [ ] 增加两款原请求重送及重启读取用例：首次受理后改变当前默认值或参数定义，原实例、生效参数和执行定义仍保持首次事实。旧规则曾受理的 Action6 `5400/6000` 秒继续按保存事实重送和恢复，同值新请求按新规则拒绝；非法新动作仍按动作级参数失败处理，不提升为整份计划拒绝。
- [ ] 在 `bootstrap/test_adb_camera_dispatch.py` 增加实际派发回归：通过测试合同按旧规则受理 Action6 的5400/6000秒任务，保持尚未启动，切换到新时间 Schema 后重启，并经公共处理器及真实 `AdbCameraDriver.control` 验证原参数、原绑定、原负载及唯一 START。同值新请求和直接当前候选入口必须拒绝；已存在 START 尝试的恢复仍不得重发。先单独运行该文件，确认目标失败来自当前 Schema 重校验。该软件兼容场景不表示生产已启用 Action6 延时或已有此类任务。
- [ ] 在生产登记测试保留候选不导出/不受理的断言，并验证单纯增加参数域或估算说明不绑定工厂。分别运行 `P apps/camctl/tests/integration/acceptance/test_adb_camera_definitions.py -q`、`P apps/camctl/tests/integration/devices/test_adb_camera_registration.py -q`，观察对应失败。
- [ ] 优先复用精确解析、默认值处理、冻结和恢复入口；按上述派发回归闭合 `commands.py` 与 `driver.py` 的可信编码接线，不反查当前候选 Schema，不访问设备重建原要求。只有实际缺口需要时才修改受理/持久化，不建立第二套用户参数校验规则；本项只用已有明确负载，不依赖 D1 的新编码。
- [ ] 按上述两个独立进程重跑，另行运行 `P apps/camctl/tests/integration/bootstrap/test_adb_camera_dispatch.py -q`，再分别运行 `P apps/camctl/tests/integration/acceptance -q`、`P apps/camctl/tests/integration/devices -q`。记录环境和退出码，检查 diff 后提交。

## P3 派生资料与跨语言接缝

**输入：** P1/P2、C0 的精确校验；C1 已核对的标准规则子集。

**交付接口：** 正式任务关闭时，测试可直接读取候选 Schema，或通过测试合同导出；不增加产品的候选能力导出开关。D3 完成后，E2 使用正式 `describe` 重验同一接缝。

- [ ] 将[共同硬件参数表](../../hardware/camera-control-handoff.md#间隔与采集时长限制)中的允许值表及合法组合计数改为由生产权威定义生成的视图；Action6 和 OSMO 的入口共同引用它，手工说明保留输入来源和设备证据边界。使用 `apps/camctl/.venv311/bin/python apps/camctl/scripts/render_timelapse_time_parameters.py --write` 生成后再核对，文档及诊断资源不再手工维护第二份完整枚举。
- [ ] 建议新增 `tests/integration/test_timelapse_time_parameters.py`，使用两款同源 Schema 及测试专用任务工厂贯通 Python、客户端共享校验和后端导出。分别验证 `1/300`、半秒域外值、等价词元、邻近非等值、严格等值、反向大小、域外及错误类型，每款合法时间对为 134；另验 Action6 三种输出和两款各自曝光结构，禁止型号之间扩散输出。客户端合成通用单元夹具不冒充完整设备枚举。
- [ ] 验证能力导出的精确 JSON 数字和参数规则可被浏览器、Node.js 后端及 Python 消费；估算信息仍仅展示，不进入生效参数或报告。
- [ ] 执行 `P tests/integration/test_timelapse_time_parameters.py -q`、`apps/camctl/.venv311/bin/python apps/camctl/scripts/render_timelapse_time_parameters.py --check`、`node scripts/check-doc-links.mjs` 及 `node scripts/check-protocol.mjs`。运行 `git diff --check`，提交资料与接缝验证；证据注明此时使用测试任务工厂，正式 OSMO 内置链另由 E2 验证，Action6 延时仍保留自身启用门槛。

## 审查重点与完成条件

P1 负责严格边界、类型和缺口；P2 负责冻结、恢复与能力门槛；P3 负责资料和多语言一致性。全部完成后，D 消费完整的合法参数域，C 消费其标准 Schema，但本领域的完成不代表新负载、相机执行或生产能力已经验收。
