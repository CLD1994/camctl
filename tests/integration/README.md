# 跨组件集成测试

本目录用于客户端、camctl CLI 和 C 接入模块之间的真实协作验证。终端演示使用同一模块；组件内部测试放在各自目录，目前客户端测试入口为 `apps/client/tests`。

`report-dependencies.test.mjs` 与 `report-dependency-conditions.test.mjs` 验证公共 Schema、数据库字段登记、规格 SQL 和检查器之间的接缝，包含错误登记反例与具体条件场景。运行入口见[检查脚本](../../scripts/README.md)；通过这些检查不表示真实 CLI 已经接入报告生成。

`test_database_history_sql.py` 在内存 SQLite 加载真实结构，验证来源及流程关联、清理状态矩阵、启动与残留停止的过期组合、独立文件历史元数据、两类目录和快照范围，并在有无统计信息时核对分页查询。它还读取统一运行库条件，验证检查脚本对主线版本、精确回移例外及非法版本输入的判定；这些版本样例不表示实际运行了所有 SQLite 版本。上述检查不代替生产入口、事件应用、历史恢复或报告字节一致性测试。

`event-transitions.test.mjs` 组合真实事件登记、SQL、枚举和报告依赖，验证列分类、分支和状态覆盖、历史归属及行权限反例。它验证设计检查器，不执行登记引用的业务校验，也不证明生产事件能够共同提交或恢复。

跨组件验收沿计划导出、模块交接路径、CLI 执行与报告发布、同步领取到 `processing`、客户端导入追踪业务结果，契约见[接入模块验收要求](../../docs/host-demo/design.md#验收要求)。报告同步主链已由真实客户端导出、真实 C 主程序递交与领取（WSL 构建的 host-demo）、camctl CLI 执行及客户端导入串联验证；录像、照片、延时摄影、取回、取消、清理、会话恢复与日志副本等场景经 camctl CLI 直接驱动验证，命令与环境见[集成计划验证记录](../../docs/superpowers/plans/2026-09-30-camctl-integration.md)。剩余为全部场景经 C 模块递交链复验、真实第三方主程序接入及目标环境执行，边界见[部署交接与待核验项](../../docs/camctl/verification.md#部署交接与待核验项)与[模块验证记录](../../docs/host-demo/verification.md)。
