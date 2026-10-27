# Hasselblad 可用性恢复实施验证

本记录保存[实施计划](../superpowers/plans/2026-10-28-hasselblad-availability-recovery.md)的阶段性软件验证。软件替身和测试环境中的结果不表示真实相机或目标主机已经验收。

## H1：录像启动占用释放测试装配

2026-10-28，在独立工作区 `/tmp/camctl-h1-capture-20261028` 的提交 `50691389` 上，使用 CPython 3.11.17 执行：

```bash
apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/capture/test_recording_start_release.py -q --tb=short
```

命令退出码为 0，六个测试通过，原始输出位于 `/tmp/camctl-h1-release-final.log`。该提交已集成为 `afbe3109`。测试使用真实业务数据库与正式会话工厂，保留可靠无效果释放、未知结果保留、降低预算、输出范围限制以及在途调用收场的原有分区。该结果不证明哈苏原责任恢复、报告生成或后续可用性检查；这些项目仍需按计划验证。
