# 仓库开发与检查脚本

本目录保存跨组件使用的脚本。组件自己的构建、生成和测试工具留在组件目录。

## 文档与公共协议

在仓库根运行 `node scripts/check-doc-links.mjs` 检查文档的本地文件链接。

运行 `node scripts/check-protocol.mjs` 检查公共协议 Schema、共享计划与能力样例、报告原始字节摘要、已登记错误详情及结构正反例。脚本复用 `apps/client` 已锁定的 Ajv 依赖，需要先准备客户端依赖；不依赖网络下载 Schema。这是访问真实规格文件的检查，不属于单元测试，也不代替生产组件的语义与集成验收。

## 报告字段依赖

运行 `node --disable-warning=ExperimentalWarning scripts/check-report-dependencies.mjs` 检查[报告字段依赖](../docs/camctl/database/report-dependencies.md)。使用 Node.js 24 的 SQLite 接口和上述 Ajv，核对字段覆盖、条件来源、嵌套结构、枚举、真实关联及报告父对象；来源列与实际 SQL 一致。该检查不实现生产报告生成。

运行 `node --disable-warning=ExperimentalWarning --test tests/integration/report-dependenc*.test.mjs` 验证登记的正反例与条件场景；这些测试读取真实 Schema、登记和规格 SQL，属于集成测试。

## 事件转换规则

运行 `node --disable-warning=ExperimentalWarning scripts/check-event-transitions.mjs` 核对[事件转换规则](../docs/camctl/database/event-transitions.md)与实际投影列、状态模型、历史对象及报告依赖的覆盖关系。修改规则后使用 `--write` 更新事件索引与 SQL 类型范围；默认检查拒绝生成内容漂移。运行 `node --disable-warning=ExperimentalWarning --test tests/integration/event-transitions.test.mjs` 验证规则反例和行权限。具名业务校验、生产事务和完整历史恢复不由这些脚本执行。

## 数据库结构与运行库

运行 `~/.venv/bin/python scripts/sync-history-object-sql.py` 核对历史对象登记生成的 SQL；仅在修改登记后使用 `--write` 更新生成区段。运行 `~/.venv/bin/python scripts/check-database-spec.py` 检查真实 SQLite 结构，运行 `~/.venv/bin/python -m unittest discover -s tests/integration -p 'test_database_history_sql.py' -v` 验证历史与报告所需的状态组合、目录范围、外键及分页查询。上述检查不创建实际状态库，也不执行设备操作。

结构检查在内存 SQLite 中加载全部规格 SQL，核对表目录、STRICT 类型、主键、外键及索引，并用合法与非法样本检查状态组合、完整事务边界和精确整数。样本只验证结构和查询，不作为可回放的业务历史；查询计划通过也不代表目标硬件的性能达标。完整生产验收按[事务验证门槛](../docs/camctl/database/transactions.md#实现与验证门槛)及[数据库一致性验收](../docs/camctl/database/consistency-verification.md)执行。

结构检查先读取[统一运行库版本条件](../docs/camctl/sqlite-runtime.json)，核对文档版本表、Python 实际链接的 SQLite 及必要能力。`--runtime-only` 只执行这部分检查，使用隔离的临时文件库验证 WAL/FULL、JSON、STRICT 及分段 BLOB 读写；`--write-runtime-doc` 从同一条件生成[版本表](../docs/camctl/sqlite-runtime.md#允许的版本)。脚本尚未接入生产入口，也不证明目标存储的并发或断电持久性。

结构检查同时读取[全库整数编号定义](../docs/camctl/database/enum-registry.json)，核对有限值列的完整分类、实际 SQL 列值约束、公共错误映射及事件状态条件。`~/.venv/bin/python scripts/check-database-spec.py --write-enum-doc` 从同一来源生成[编号一览](../docs/camctl/database/enum-values.md)；默认模式检查该文档是否一致。SQL 列值检查与完整表的状态组合样本分别执行，JSON 内部分类只检查定义及归属；生产类型适配、JSON 业务校验和历史往返仍待实现。
