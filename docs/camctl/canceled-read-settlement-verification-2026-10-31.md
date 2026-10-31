# 取消读取收场修复验证

## 问题与当前行为

[真机批次记录](file-batches-real-device-verification-2026-10-31.md)中的取消发生在哈苏下载期间。源会话最终正常读到末尾，但末段保存因取消被跳过，可靠进度仍为零。旧实现用会话位置申请完整 `READ_END`，被严格进度守卫拒绝；原尝试及取消动作持续未结束。

普通设备取回、本机及照片缓存、录像检查与修复输入共同区分真实源返回和可靠副本完成。只有可靠段结果证明副本全部提交，才申请完整结束。取消时实际已经正常读完的源仍保存成功的尝试结果；部分停止保存 `UNKNOWN/read_stopped`。两者均按原取消责任收场，不补写进度。源错误和本机错误保留原诊断。

已取消旧读取由独立入口发现，优先消费原结果、原业务保存及持久结束依据。没有原返回时，按原票据、旧拥有者结束边界、原意图水位和原驱动 READ 恢复契约保存未知结果，不重新下载。读取流程已经终态时，迟到的真实返回只结束原尝试，不改变原流程结果。

## 隔离单元验证

Linux 开发容器，Python 3.11.17。测试子代理在独立工作树的 `70316ef3` 基线上取得 3 项失败、5 项通过，反例分别证明末段跳过仍被认为可靠完成、正常 EOF 无法持有取消结果及其原键恢复缺口。随后按状态分区补充测试。

最终命令从仓库根目录执行：

```bash
env PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  apps/camctl/.venv311/bin/python -m pytest -q -p no:cacheprovider \
  apps/camctl/tests/unit/capture/test_input_copy_cancel_isolated.py \
  apps/camctl/tests/unit/outputs/test_stopped_read_result_cancel_isolated.py \
  apps/camctl/tests/unit/outputs/test_read_end_persistence_isolated.py \
  apps/camctl/tests/unit/outputs/test_canceled_read_end_recovery_isolated.py \
  apps/camctl/tests/unit/capture/test_canceled_processing_read_isolated.py
```

执行会话 `91107`，66 项及 4 个子测试通过，2.12 秒，退出 0；完整输出为 `tmp/canceled-read-unit-final.log`。资源、数据库、文件、工具和执行器在受测边界提供内存替身。新增用例完整加载输入编排及读取结果模块，验证可靠完成、实际 EOF、部分停止、原结果身份、冷恢复分类，以及取得结束依据后取消事实查询失败的继续保存。既有消费者的源码抽取用例只证明局部接线，不能作为真实装配、SQL、历史回放或清理组合行为的证据。

首次运行上述范围中的四个文件时，结果为 43 项通过、3 项失败：两个消费者用例仍模拟旧的内部组织，另一个旧摘要用例缺少 `allow_recopy` 配置，并通过 `asyncio.to_thread` 启动了真实线程。这次执行超出纯单元约束，已向用户说明，结果不计入符合约束的验证。随后将该用例的执行器隔离为同步内存替身，补齐原配置；消费者用例改为核对共同收场端口，原票据、结束依据和恢复分区由共同入口的单元用例验证。没有放宽生产守卫。

独立评审确认并修正了 ADB 恢复模板误用于 READ、原流程终态后迟到观察被错误放回续读责任的问题。最终复核未发现新的确定生产阻断。12 个变更 Python 文件的 AST、变更文档的本地链接和差异空白检查通过。

## 交付与未验证范围

取消修复前已交付 `70316ef3` 的完整部署包，位于 `tmp/deploy-package-20261031-70316ef3/camctl-deploy.zip`，SHA256 为 `aeefe99bc2d791ecb9b1acae6468b05c61a2b50104e6f73cda5532462798d88c`。构建 13 步成功，Linux x86_64 仓库外主机安装、客户端启动及完整摘要核验通过；独立复核确认路径修复已入包，用户的媒体目录没有入包。该包包含哈苏路径修复，取消修复只存在于后续源码。

未新增集成测试。除上述已说明的旧测试线程越界，未运行集成测试套件、真实 SQL 组合、升级、设备操作或断电验证。旧失败环境和状态库保持原样；源码的冷恢复路径尚未在该现场执行，不能将单元通过表述为旧环境已经恢复。构建依赖、配置、设备能力和持久化结构没有改变；部署 skill 和打包入口无需调整。
