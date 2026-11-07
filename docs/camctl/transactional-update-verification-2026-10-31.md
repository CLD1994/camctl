# 自动迁移与更新恢复验证

## 实现范围

基线为 `d72c1135`，最终生产代码为 `79c2eeaa`。行为由[组件版本与软件更新](../architecture/versioning-and-updates.md)定义，进度由[实施计划](../superpowers/plans/2026-10-31-transactional-camctl-update.md)跟踪。自动维护以当前 V9 为起点；当前格式只核验，后续格式在 `camctl.deployment` 的单一登记中提供迁移路径。数据库格式号仍从统一枚举派生。

包内更新入口已接通完整环境备份、SQLite 一致备份、副本迁移发布、失败回滚及下次入口恢复。控制进程在原虚拟环境外运行，先取得目标目录锁，再处理未完成记录；安装与维护子进程继承更新资格。恢复记录保存原部署和事务临时对象的事实，环境、数据库及适用能力文件按同一更新结果处理。部署 skill、完整包清单与 README 同步更新；维护完整性检查从 Python 包中的同一源码生成到包内。

独立只读审查已闭合提交结果分类、路径与文件身份冲突、记录状态与真实布尔分类、数据库完整性和外键、文件元信息及外层诊断。后续复核还覆盖未完成复制中的断链、原 UID/GID 已应用后的重入、旧终态与新配置的交接，以及准备阶段遗留材料对后续检查和回滚的影响。最终源码未发现剩余确定阻断；这项结论不代表真实恢复已验收。

## 隔离单元

工作目录为 `/workspaces/camctl`，Linux 开发容器、Python 3.11.17。最终命令为：

```bash
env PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  apps/camctl/.venv311/bin/python -m pytest -q -p no:cacheprovider \
  apps/camctl/tests/unit/logging_runtime/test_admission.py \
  apps/camctl/tests/unit/logging_runtime/test_startup_admission.py \
  apps/camctl/tests/unit/logging_runtime/test_startup_version.py \
  .agents/skills/camctl-real-device-deploy/tests/unit
```

结果为 **115 项通过、56 个子测试通过，0.72 秒，退出 0**。完整输出在 `tmp/transactional-update-unit-verified.log`；执行工具输出标识为 `bffdfb`，日志读取输出标识为 `f5b860`。其中部署工具范围为 83 项，其余为启动版本记录的局部回归。先前整合阶段的 93 项、112 项结果分别保留在 `tmp/transactional-update-unit-final.log`、`tmp/transactional-update-unit-complete.log`，不重复累计。

文件、锁、数据库连接、进程、平台与用户环境协作者均按受测入口替换；资源依赖在导入前隔离。测试覆盖公开更新编排、检查与恢复入口的状态及故障分区，没有用真实文件交换或数据库组合代替单元。

开发期间发生一次隔离越界：旧锁替身未覆盖新增标准库锁，尝试以 `O_CREAT` 打开 `/outside/state.db.session.lock`，因父目录不存在抛出 `FileNotFoundError`，没有创建文件。执行者报告该轮为 4 项失败、23 项通过及 5 个子测试通过，退出 1；该轮不计入合规验证。修正锁、用户路径、平台查询和导入边界后，经独立静态审计再执行上述范围。另有测试收集或静态检查因多个 `patch` 超过 Python 嵌套上限失败，修正替身组织后通过。原失败输出保留在执行代理工具会话中；本页的最终通过结果由根代理实际重跑确认。

## 静态检查与未运行范围

变更的 13 个 Python 文件完成 AST 解析，变更 Markdown 的本地文件链接及 `git diff --check` 通过。部署包 README 中的三条 `docs/` 链接由打包脚本生成，未把源模板中不存在的生成文件算作通过。完整部署包仍须按[打包契约](../../.agents/skills/camctl-real-device-deploy/references/packaging.md)另行构建验证。

没有新增或运行集成测试，没有实际下载依赖、构建新部署包、安装或更新已有环境，也没有操作相机。真实 SQLite/WAL、跨文件系统发布、UID/GID 与扩展属性恢复、目录交换、继承锁及子进程中断、物理断电、目标 ARM64 和最低运行环境均未验证。文件备份与回滚的真实组合仍需单独验收，不能由上述替身结果推断现场恢复成功。
