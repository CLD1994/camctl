# 格式 1 的 SQLite 结构

[数据库入口](../../database-schema.md) · [公共规则](../common.md)

| SQL | 所定义的表 | 字段语义 |
| --- | --- | --- |
| [core.sql](core.sql) | 计划与动作 | [计划与动作](../plans-actions.md)、[执行定义](../execution-definitions.md) |
| [workflows.sql](workflows.sql) | 自动预览、来源、取回、清理和取消明细 | [流程字段](../workflow-fields.md) |
| [files.sql](files.sql) | 设备文件、中间文件、正式产物、来源关系、交付和拷贝 | [文件字段](../file-fields.md) |
| [operations.sql](operations.sql) | 操作流程、尝试、设备活动和录像处理 | [操作字段](../operation-fields.md) |
| [reports.sql](reports.sql) | 输入诊断、报告、同步、全局运行状态和元信息 | [报告与运行状态](../reports-runtime.md#字段与状态组合) |
| [history.sql](history.sql) | 历史事务、事件、自身恢复关联、报告变化目录、快照和维护进度 | [历史格式](../history-formats.md) |

## 结构同步状态

本目录六份文件合起来定义 30 张表。来源依赖、清理项结果与产物汇总、单向流程关联、独立文件历史、两类目录及文件快照范围已落实到 SQL；[报告字段登记](../report-dependencies.json)的来源列与关联均使用实际结构核对。

等待事实、启动流程过期、设备查询、应急补记、活动占用、报告分页及部署目录绑定所需的结构已与各自规格同步。等待条件由所属业务记录表达；启动流程过期时保留已有尝试次数，残留停止流程只允许在尚未尝试时过期。生产工作见[模块计划](../../../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md#模块计划与任务入口)；结构检查通过不表示业务事件处理、跨表事实校验、历史恢复或报告生成已经实现。

事件类型范围由[事件转换规则](../event-transitions.json)生成，包含应急最终补记事件；业务列分类与实际 SQL 完整对应，生产代码仍须执行各事件的完整业务校验。

## 历史对象登记与生成

[内部登记](../enum-registry.json)的 `history_objects` 是对象编号、标识、主表及报告和快照资格的唯一来源。以下工具只更新 `history.sql` 的标记区段，不创建数据库，也不修改业务状态：

```sh
~/.venv/bin/python scripts/sync-history-object-sql.py --write
~/.venv/bin/python scripts/sync-history-object-sql.py
```

默认模式核对生成区段与登记完全一致；不一致时失败。快照候选索引使用生成的 `entity_type_name`，避免按整数编号排列类型。修改登记后须运行生成及全部结构测试，检查目录范围、真实主表和排序是否仍符合正式契约。

事件编号另由 `node --disable-warning=ExperimentalWarning scripts/check-event-transitions.mjs --write` 生成。默认省略 `--write` 时检查事件目录、列权限与状态模型，并核对生成区段；它与历史对象生成器各自维护独立标记区段。

## 全库整数编号检查

[整数编号定义](../enum-registry.json)区分普通枚举、JSON 内部整数分类、布尔列、外部编号来源和固定值。[现有结构检查](../../../../scripts/check-database-spec.py)双向核对实际 SQL 的全部有限值整数列，在 SQLite 中验证每列的合法值、非法值和空值，再执行完整表的组合样本。实际列与分类必须完整对应，遗漏或多出的定义均使检查失败。

人类阅读的[编号一览](../enum-values.md)由同一检查脚本生成；默认运行只检查，指定 `--write-enum-doc` 才更新该文档。该选项不修改 SQL，也不生成生产枚举代码。历史对象和事件类型的 SQL 生成继续使用上一节的专用入口。

## 初始化与验证

它们是供评审和验证的结构规格，不是日常入口可以自行执行的建库脚本。初始化器在新建临时数据库的一个事务中执行全部定义并创建两条单份状态记录，完成结构及持久性检查后按初始化契约提供正式状态库。已有状态库只校验，不重新执行建表语句。

SQL 允许前向外键引用，因此六份文件的建表顺序不影响最终结构。初始化记录、事件格式、整数枚举、状态组合及写入入口分别在所属专题定义。对表结构的任何修改都必须同步其语义文档和规格验证；生产实现须额外验证事件应用、并发、回滚及断电恢复。

结构、状态组合及查询的检查命令与覆盖范围见[数据库结构检查](../../../../scripts/README.md#数据库结构与运行库)，生产行为按[一致性验收](../consistency-verification.md)验证。实际运行库必须满足统一的[SQLite 运行库与部署要求](../../sqlite-runtime.md)；结构检查先核对版本及必要能力，再执行完整 SQL，不以某项 SQL 特性的最低版本代替部署要求。
