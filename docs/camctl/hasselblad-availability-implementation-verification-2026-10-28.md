# Hasselblad 可用性恢复实施验证

本记录保存[实施计划](../superpowers/plans/2026-10-28-hasselblad-availability-recovery.md)的阶段性软件验证。软件替身和测试环境中的结果不表示真实相机或目标主机已经验收。

## H1：录像启动占用释放测试装配

2026-10-28，在独立工作区 `/tmp/camctl-h1-capture-20261028` 的提交 `50691389` 上，使用 CPython 3.11.17 执行：

```bash
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/capture/test_recording_start_release.py -q --tb=short
```

命令退出码为 0，六个测试通过，原始输出位于 `/tmp/camctl-h1-release-final.log`。该提交已集成为 `afbe3109`。测试使用真实业务数据库与正式会话工厂，保留可靠无效果释放、未知结果保留、降低预算、输出范围限制以及在途调用收场的原有分区。该结果不证明哈苏原责任恢复、报告生成或后续可用性检查；这些项目仍需按计划验证。

在同一工作区的提交 `61f5ece2` 上执行下列命令，退出码为 0，11 项通过；原始输出位于 `/tmp/camctl-h1-focus-4.log`，集成提交为 `d47367fb`。

```bash
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/capture/test_hasselblad_start_recovery.py -q --tb=short
```

该测试通过真实业务数据库和会话工厂覆盖相对调焦后的原调用中断、可靠和缺失的恢复边界、同会话调用仍在执行、已保存结果、提交回执未知及原操作键。它不代替安装版故障门与新检查的联合验证。

在独立工作区 `/tmp/camctl-h1-reporting` 的提交 `e0a9c8ee` 上，使用 CPython 3.11.17 执行 `apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/reporting/test_emergency_error_generation.py -q --tb=short`，退出码为 0，六项通过；前台输出未单独保存。集成提交为 `7fed2961`。该范围验证应急错误报告的三种活动状态、原动作错误和固定报告字节，不证明哈苏新检查报告。

同一工作区的完整 reporting 目录另有一次失败：204 项通过、285 项失败、10 项错误，原始日志 `/tmp/camctl-h1-reporting-full.log` 的 SHA-256 为 `e0dab6096747e94cc3a38dfca0627bc01d26a8503d5b65def41f87dfc98aa2c8`。失败均在旧测试装配阶段，分别涉及旧 `accept_input` 调用、将连接直接交给异步数据库接口、旧 `CaptureRuntime` 和旧 `ObtainRuntime` 装配；这些用例尚未验证目标报告行为，正在迁移。

在 `/tmp/camctl-h1-capture-20261028` 的提交 `47259000` 上，使用 CPython 3.11.17 对 `test_hasselblad_start_recovery.py`、`test_recording_start_release.py` 和 `test_capture_contract.py` 一同执行 pytest，退出码为 0，38 项通过；原始输出位于 `/tmp/camctl-h1-capture-final.log`，集成提交为 `ca5db88c`。原节点和参数保持：哈苏原恢复 11 项、录像释放 6 项、拍摄契约 21 项；拍摄契约通过正式处理入口和真实结果页事务保留文件登记回滚及原键恢复覆盖。完整目录仍需验证。

## H2：固定身份和证据类别

2026-10-28，在独立工作区 `/tmp/camctl-h2-availability-storage-20261028`，使用 CPython 3.11.17 执行：

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/devices/test_hasselblad_resource_identity.py apps/camctl/tests/unit/devices/test_hasselblad_configuration.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/devices/test_evidence.py -q
```

两条命令退出码均为 0，分别为 37 项和 15 项通过。验证时的提交分别为 `57d58d7a` 和 `fd2b0e56`，集成提交为 `e7409865` 和 `035016f5`。前台输出未保存原始日志。这些测试覆盖身份值、资源键、配置解析和证据类别；不证明身份已经在活动预留时固定，也不证明可用性事务、历史回放或业务调用已经接通。

## H4：公共诊断消费者

在 `/tmp/camctl-h4-diagnostics` 的提交 `0316cb2c` 上，使用 CPython 3.11.17 执行日志单元目录和组件集成目录，分别为 366 项和 296 项通过；补充测试后，受影响单元文件 51 项、集成文件 8 项通过。原始输出分别位于 `/tmp/camctl-h4-diagnostics-unit.log`、`/tmp/camctl-h4-diagnostics-integration.log`、`/tmp/camctl-h4-diagnostics-unit-final.log` 和 `/tmp/camctl-h4-diagnostics-integration-final.log`；集成提交为 `cd7282fb`。测试覆盖公共消费者、原双流、重复交付、满队列和非检查媒体输出隐藏。检查的实际结果拥有者尚未接入该入口，真实设备收场和 HA-11 联合验收尚未完成。
