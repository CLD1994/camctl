# Hasselblad 可用性恢复实施验证

本记录保存[实施计划](../superpowers/plans/2026-10-28-hasselblad-availability-recovery.md)的阶段性软件验证。软件替身和测试环境中的结果不表示真实相机或目标主机已经验收。

## H1：录像启动占用释放测试装配

2026-10-28，在独立工作区 `/tmp/camctl-h1-capture-20261028` 的提交 `50691389` 上，使用 CPython 3.11.17 执行：

```bash
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/capture/test_recording_start_release.py -q --tb=short
```

命令退出码为 0，六个测试通过，原始输出位于 `/tmp/camctl-h1-release-final.log`。该提交已集成为 `afbe3109`。测试使用真实业务数据库与正式会话工厂，保留可靠无效果释放、未知结果保留、降低预算、输出范围限制以及在途调用收场的原有分区。该结果不证明哈苏原责任恢复、报告生成或后续可用性检查；这些项目仍需按计划验证。

## H2：固定身份和证据类别

2026-10-28，在独立工作区 `/tmp/camctl-h2-availability-storage-20261028`，使用 CPython 3.11.17 执行：

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/devices/test_hasselblad_resource_identity.py apps/camctl/tests/unit/devices/test_hasselblad_configuration.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/devices/test_evidence.py -q
```

两条命令退出码均为 0，分别为 37 项和 15 项通过。验证时的提交分别为 `57d58d7a` 和 `fd2b0e56`，集成提交为 `e7409865` 和 `035016f5`。前台输出未保存原始日志。这些测试覆盖身份值、资源键、配置解析和证据类别；不证明身份已经在活动预留时固定，也不证明可用性事务、历史回放或业务调用已经接通。
