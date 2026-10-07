# camctl 共享类型与公开投影模块实施计划

> 供执行 Agent 使用：实施时按 `superpowers:executing-plans` 逐任务执行；明确选用 subagent（子 Agent）执行方式时，可使用 `superpowers:subagent-driven-development`。复选框只跟踪实际实施，不因计划已经编写而勾选。

**目标：** 建立精确、受约束的跨模块值类型和纯公开字段计算，使输入、数据库、历史和报告使用同一含义。

**组织建议：** 共享包只包含身份、带单位的时间、精确 JSON、有限批次及公开字段计算；业务阶段和外部句柄留在拥有者模块。内部类型、签名、文件和提交拆分中未由规格固定的部分均为建议，行为契约及项目规则是硬性要求。

**技术基础：** 使用 dataclasses、Enum、Decimal、Fraction 和标准库 json；公共表示从根 protocol 读取。版本与依赖以组件锁文件及现有权威登记为准。

**设计依据：** [输入与数字适配](../../camctl/data-types.md)、[分页结果契约](../../camctl/module-contracts.md#分页结果契约)、[报告编码](../../architecture/report-encoding.md)、[报告字段依赖](../../camctl/database/report-dependencies.md)、[内部整数登记](../../camctl/database/enum-registry.json)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [共享实施契约](../../camctl/module-contracts.md)

## 行为契约与实施边界

公共 ID 以规范十进制字符串表达，数据库保存整数，转换不经过浮点。数学值为整数的小数及指数可以满足整数字段，布尔值不可以；省略与显式 null 保留区别。历史边界必须是完整事务末位，公开字段只从边界处事实计算，不能访问当前配置、设备、墙钟或数据库。

统一的精确数字、单一登记来源、完整事务、取消后接手、测试分类及执行命令采用[共享实施契约](../../camctl/module-contracts.md#实施范围与统一执行方式)。本计划只覆盖第一版已经定义的故障范围；软件集成不连接真实设备。

## 接口与数据建议

公共入口建议显式导出下表类型。每个转换函数成功返回有效值，非法输入抛出带字段位置的类型化错误；不会使用 0、空对象或当前时间代替错误。

| 类型 | 字段或含义 |
| --- | --- |
| `ObjectId / OperationKey` | ObjectId 是经范围校验的整数身份；OperationKey 标识一次完整数据库业务操作，并绑定其输入、阶段和目标。 |
| `UtcMicros / DurationMillis` | 分别为 UTC 起点的整数微秒和非负或正整数毫秒；合法范围按所属字段校验，不混用单位。 |
| `ClockPort / MonotonicClock` | contracts.clock 拥有时钟端口；monotonic_ns 返回本机单调整数纳秒，utc_micros 返回精确 UTC 微秒，读取失败明确抛错，测试使用替身。 |
| `JsonValue / MISSING` | int、有限 Decimal、字符串、布尔值、None 及递归容器；MISSING 仅表示字段省略。 |
| `HistoryBoundary / Page[T, C]` | 历史边界采用共享实施契约的字段；Page 的字段和结束属性遵守[分页结果契约](../../camctl/module-contracts.md#分页结果契约)，游标类型 C 与读取范围关联，游标包含固定范围与最后排序位置。 |
| `ProjectionInput / PublicFragment` | 单对象的类型、身份、自身事实和已恢复到同 H 的有界字段依赖；输出按公共 Schema 定义的字段片段，子集合由消费者分页组织。 |

公共 ID 解析只接受规范十进制字符串；内部 ObjectId 从合法整数构造。下表单独描述普通 JSON 数值的整数转换，字符串在这个数值入口不是合法数字，不能与公共身份解析混用。转换先校验类型，再判断数值和范围。

| 输入 | 整数转换结果 |
| --- | --- |
| int 或数学值为整数的有限 Decimal，且在范围内 | 返回精确整数。 |
| 有限 Decimal 但数学值非整数，或值超范围 | 返回字段类型或范围错误，不舍入。 |
| bool、字符串、null、容器或非有限值 | 返回类型错误，不隐式转换。 |
| 字段省略 | 返回 MISSING，由拥有者按字段契约处理。 |

公开投影输入缺少必要依赖或包含不同历史边界时，返回明确的解释错误；有效空子集合与缺少依赖分开。公开变化判断比较规定字段，不以任意内部行变化推定报告变化。

## 文件职责建议

| 预计生产文件 | 责任 |
| --- | --- |
| `apps/camctl/src/camctl/contracts/values.py` | 身份及单位转换。 |
| `apps/camctl/src/camctl/contracts/clock.py` | UTC 与单调时钟端口，不读取实际时钟。 |
| `apps/camctl/src/camctl/contracts/json_values.py` | 精确解析、Unicode 校验及存在性。 |
| `apps/camctl/src/camctl/contracts/enums.py` | 从权威登记生成或加载受约束枚举。 |
| `apps/camctl/src/camctl/contracts/history_values.py` | 完整边界和引用校验。 |
| `apps/camctl/src/camctl/contracts/pages.py` | 范围游标及批次结果。 |
| `apps/camctl/src/camctl/contracts/public_projection.py` | 纯公开字段投影和比较。 |

涉及 SQLite 的用例通过窄仓储接口接入；事件、投影、目录和维护进度在同一完整事务内保存。测试文件在对应任务列出；尚不存在的路径是实施位置建议。

## 任务依赖与交付

| 前置或组合计划 | 需要的交付 |
| --- | --- |
| [入口与装配](2026-09-30-camctl-bootstrap.md) | B1 提供包及测试命令；K1—K3 不依赖其他业务模块。 |
| [历史](2026-09-30-camctl-history.md) | H1、H2 提供边界处事实；K4 与事件消费者组合。 |
| [报告](2026-09-30-camctl-reporting.md) | R1、R3 消费纯投影，验证输出与变化判断同源。 |

K1、K2、K3 完成基础值后，受理和持久化可以实施。K4 先为首条报告链实现已接入实体，随后每个新事件同步增加字段依赖用例。K5 在实际模块出现后检查依赖，不能以空包通过架构门禁。

## 审阅重点

以下五项均落实到具体失败用例；它们与任务内的其他用例共同约束实施。

| 易遗漏的条件及预期 | 负责的任务和用例 |
| --- | --- |
| 长小数及低 Decimal 精度仍保持原值。 | K2，`test_decimal_context_does_not_change_value` |
| bool 不作为枚举编号或整数身份。 | K1，`test_bool_is_not_object_id` |
| 字段省略与显式 null 往返后仍不同。 | K2，`test_missing_is_distinct_from_null` |
| 空有效页仍按候选游标继续。 | K3，`test_empty_page_can_continue` |
| 内部变化不自动成为公开变化。 | K4，`test_internal_change_is_not_public_change` |

## 实施任务

### K1 身份、时间和整数枚举

**预计文件：** `apps/camctl/src/camctl/contracts/values.py`、`apps/camctl/src/camctl/contracts/enums.py`、`apps/camctl/src/camctl/contracts/clock.py`；测试为 `apps/camctl/tests/unit/contracts/test_values.py`。

**接口与依赖：** 提供 `parse_object_id(raw: JsonValue) -> ObjectId`、`make_object_id(value: int) -> ObjectId`、`to_utc_micros(raw: str) -> UtcMicros`、`seconds_to_duration_ms(seconds: int | Decimal) -> DurationMillis`；ClockPort 继承 MonotonicClock，分别提供 utc_micros 与 monotonic_ns，实际适配由 B6 装配；枚举映射读取权威登记。前置交付：B1；相关协议与整数登记。

- [x] 编写失败用例。建立 `test_bool_is_not_object_id`，输入 True 断言身份类型错误；公共 ID 只接受规范十进制字符串，JSON 数字、前导零、正负号和越界分别拒绝，最大合法值精确返回。make_object_id 验证内部整数且拒绝 bool。建立 `test_exact_duration_conversion`，输入 Decimal('1.5') 秒，`assert result == 1500`；Decimal('1.0005') 不能精确表示。日期覆盖起点两侧、合法最早最晚值、闰日、小数秒非法及不同本地时区；枚举覆盖未知编号、跨枚举混用及公共文本编码。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/contracts/test_values.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。实现精确整数与单位适配；日期采用整数运算，枚举只从登记加载或生成，不复制完整编号清单。
- [x] 再运行上述命令，要求全部 PASS，并核对 合法值往返相等，非法值没有默认替代，生成映射与权威资源一致。
- [x] 审阅实际接口、状态分区及失败路径，检查 全部身份、时间和枚举转换入口是否绕过统一适配；记录门禁证据，建议以“feat: 实现身份时间与枚举适配”形成独立提交。

### K2 精确 JSON 解析与数值判断

**预计文件：** `apps/camctl/src/camctl/contracts/json_values.py`；测试为 `apps/camctl/tests/unit/contracts/test_json_values.py`。

**接口与依赖：** 提供 `parse_exact_json(text: str) -> JsonValue`、`is_json_integer(value: JsonValue) -> bool`、`is_multiple(value: int | Decimal, divisor: int | Decimal) -> bool`。前置交付：K1 的字段错误类型。

- [x] 编写失败用例。建立 `test_decimal_context_does_not_change_value`，在不同 Decimal 精度下解析 1.0000000000000001，`assert value == Decimal('1.0000000000000001')`；`assert is_json_integer(Decimal('1e0')) is True`，`assert is_json_integer(True) is False`。建立 `test_missing_is_distinct_from_null`，断言原对象保留缺省及 None。重复成员、NaN、Infinity、未配对代理码点分别拒绝；multipleOf 用独立有理数预期。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/contracts/test_json_values.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。按原文直接构造 Decimal，检查全部键和字符串 Unicode；用 Fraction 处理整数和倍数，不通过 float 或受上下文舍入的 normalize。
- [x] 再运行上述命令，要求全部 PASS，并核对 原结构化输入保留事实，错误定位可安全编码。
- [x] 审阅实际接口、状态分区及失败路径，检查 解析、比较及倍数路径是否遗漏 bool 与数值相等的区分；记录门禁证据，建议以“feat: 实现精确 JSON 数字与存在性”形成独立提交。

### K3 完整历史边界与分页结果

**预计文件：** `apps/camctl/src/camctl/contracts/history_values.py`、`apps/camctl/src/camctl/contracts/pages.py`；测试为 `apps/camctl/tests/unit/contracts/test_boundaries.py`。

**接口与依赖：** 提供遵守[分页结果契约](../../camctl/module-contracts.md#分页结果契约)的 Page；建议提供 `validate_boundary(boundary: HistoryBoundary, transaction: TransactionRange) -> None` 和 `validate_page(page: Page[T, C], scope: ReadScope[C]) -> None`。TransactionRange 含事务 ID、首尾事件，ReadScope 含固定上界、排序及上次游标；具体游标结构由所属查询接口定义。前置交付：K1；历史格式规定的初始边界。

- [x] 编写失败用例。建立 `test_empty_page_can_continue`，仅用空 items 和合法后续候选游标构造 Page，`assert page.exhausted is False`。分别建立 `test_nonempty_page_can_continue`、`test_last_page_keeps_items`、`test_empty_page_is_exhausted`，覆盖其余三种成功状态，结束时仍保留本批数据；结束属性不可独立传入或赋值。事务中间位置、错误事务 ID、倒退或未推进游标、跨范围游标分别拒绝。初始化零事件边界另按规格验证。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/contracts/test_boundaries.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。把 H、C、S 统一为完整边界类型；Page 只保存本批数据和继续位置，以只读属性推导结束，范围及位置校验与具体查询配合。批量上限和确认末尾由读取方保证。
- [x] 再运行上述命令，要求全部 PASS，并核对 恢复位置与业务变化序号不能互相替代。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有批次构造方、测试替身及消费者是否遵守两字段构造与只读结束属性，是否在处理最后一批数据后才结束；空 items 不推定结束。记录门禁证据，建议以“feat: 定义完整边界与分页契约”形成独立提交。

### K4 公开字段计算与变化比较

**预计文件：** `apps/camctl/src/camctl/contracts/public_projection.py`；测试为 `apps/camctl/tests/unit/contracts/test_public_projection.py` 和 `apps/camctl/tests/integration/contracts/test_public_projection.py`。

**接口与依赖：** 提供 `project_public(facts: ProjectionInput) -> PublicFragment`、`public_changed(before: ProjectionInput, after: ProjectionInput) -> bool`。前置交付：K1—K3、H1 的事实结构及公共 report-dependencies 登记；不依赖报告协调器。

- [x] 编写失败用例。建立 `test_internal_change_is_not_public_change`，只改变内部尝试依据且公开结果不变，`assert public_changed(before, after) is False`；改变交付最终结果则断言为 True。按 report-dependencies 的条件覆盖字段省略、失败、未知、父对象补齐及关联文件变化；缺少必要事实必须报错。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/contracts/test_public_projection.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。读取统一字段依赖，把公开投影放在无 IO 的同步函数中；一次处理单对象和有界字段，子集合留给分页编码。
- [x] 再运行上述命令，要求全部 PASS，并核对 事件消费者与编码器读取同一字段计算，原始错误输入没有被有效参数覆盖。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/contracts/test_public_projection.py -q`，组合 H2 与 R3，验证同一事实的目录变化和实际报告字段一致。
- [x] 审阅实际接口、状态分区及失败路径，检查 依赖是否经过报告协调器回调数据库或设备；记录门禁证据，建议以“feat: 实现纯公开投影与变化比较”形成独立提交。

### K5 实际模块依赖验证

**预计文件：** `apps/camctl/src/camctl/contracts/public_projection.py`；测试为 `apps/camctl/tests/integration/contracts/test_dependencies.py`。

**接口与依赖：** 验证实际导入图与 bootstrap 装配；不新增通用插件框架或生产依赖检查服务。前置交付：B6 及本批已经接入的模块。

- [x] 编写失败用例。建立 `test_rules_do_not_import_adapters`，解析实际 Python 导入关系，`assert forbidden_edges == []`；用故意增加违规导入的最小样本确认检查可以失败。运行规则函数时外部接口替身均拒绝调用，验证 `assert external_calls == []`。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/contracts/test_dependencies.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。在集成测试读取真实源码结构与导入结果，检查共享层、规则、流程和适配器的方向；把确实需要的依赖作为有说明的允许边。
- [x] 再运行上述命令，要求全部 PASS，并核对 检查面对别名、相对导入和换行仍有效，且检查对象不是空包。
- [x] 审阅实际接口、状态分区及失败路径，检查 新增公共类型是否有真实消费者，是否把厂商响应或 SQL 行放入共享层；记录门禁证据，建议以“test: 验证实际模块依赖边界”形成独立提交。

**K5 实施说明（2026-10-07）：** 检查器以 AST 解析全部源码的真实导入（相对导入解析到绝对名，`from camctl import x` 与成员名展开成完整路径，别名与多行括号形式在语法层处理），按五条方向规则输出违规边：共享层（contracts）不导入业务、适配器与持久化；业务流程不直接使用 sqlite3、对 `persistence.runtime` 只允许 `OwnedConnection` 类型注解；报告生成（worker 除外）不导入会话、采集或设备控制；适配器实现（logging_runtime、devices 实现）不导入业务流程、持久化或其他适配器实现；历史事件应用与公开投影不导入报告协调器。合成违规样本验证检查器对每类边都能报出。分层中的端口服务层（operations、devices.read_session/evidence/parameter_schemas、session.supervision、host_files、acceptance.schema）与装配入口（bootstrap、cli、worker、initialization、devices.catalog）按 module-contracts 的依赖图定义；确实需要的依赖（连接类型注解、worker 在子进程内开连接与使用锁后端、仓储依赖业务端口）以注释说明的允许边表达。检查暴露并修复一处真实方向违规：`bootstrap/resources.py` 是通用资源读取原语却被共享层反向依赖，已迁移为顶级模块 `camctl/resources.py`（13 处导入更新，bootstrap 计划文件表同步）。运行行为验证：外部接口替身（open/sqlite3.connect/Popen/socket）拒绝调用环境下运行 K1—K3 纯规则函数，无外部调用发生。共享层内无厂商响应或 SQL 行类型（R1 零业务与持久化导入）；本轮未新增公共类型。

## 模块完成门禁

K1—K3 的往返和边界用例通过；K4 与真实事件、报告消费者组合通过；K5 检查真实导入与装配。新业务类型的公开字段测试随该业务任务完成，不把第一批对象通过当作所有对象已覆盖。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
