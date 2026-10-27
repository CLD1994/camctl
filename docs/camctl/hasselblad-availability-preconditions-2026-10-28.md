# Hasselblad 可用性恢复的设计前软件验证

## 范围与来源

本记录于 2026-10-28 整理隔离 Linux 软件环境中的设计前证据，对应[正式设计](hasselblad-availability-recovery.md)。实验使用 Python 3.11.17、临时状态库、临时文件及受工具接口约束的相机替身，没有连接真实相机，也没有使用或修改现场状态库。

现有业务及结构实验的生产源码为 `b173ae1272ccba466cb2a2b7fbbbe380c83b2c95`。独立 doctor 接口的实验源码为 `670871aa`，该分支提供底层检查接口，尚未接入本文设计的检查责任、调度和占用释放。安装版缺失 START 实验只在隔离测试副本中调整了旧 host 启动文案，生产源码未因此修改。

本次收据存放于[验证数据目录](verification-data/2026-10-28-hasselblad-availability/receipts.json)。目录内保存原始文本日志及其 SHA-256，收据列出原日志位置与归档文件。短日志只包含结果汇总时，不据此补造没有记录的完整命令或进程环境；下文列出实际测试目标和已核对的关键分区。

## 已取得的证据

| 验证目标 | 结果 | 证明范围 |
| --- | --- | --- |
| 独立 doctor 接口单元 | 16 passed，0.18 秒 | 唯一设备、serial、明确 PASS、异常或不完整结果、前序调用事实及完整检查的共同期限。 |
| 独立 doctor 接口与真实受管工具替身 | 2 passed，0.96 秒 | 实际执行 list、info、doctor，保存双流与退出状态；PASS 文本不能覆盖非零退出。 |
| doctor 调用中断补充实验 | 2 passed，1.77 秒 | info 超时及 doctor 取消后，实际脚本进程收到 SIGTERM 并已退出；前序和部分双流保留，无后续命令。 |
| 活动释放仓储与收场事务 | 4 passed、3 passed，分别为 0.55 秒、0.43 秒 | 临时 SQLite 中，可靠未派发的 UNKNOWN 可以释放；可能已派发且缺少结束依据时保留 HELD；可靠停止可以形成结束事实；输出范围责任可以使 ENDED 继续 HELD。 |
| 设备执行报告字段的登记解释 | 9 passed | 状态由动作终态、派发、活动及完成依据决定，不依赖 occupancy。它没有执行完整生产报告生成。 |
| DEVICE 安装版业务链 | RAW、JPEG 共 2 passed，39.85 秒 | 实际安装包经 CLI 登记 original、preview，回收拍摄临时副本，然后两次独立下载两种正式源，保持原大小、摘要和产物身份。 |
| DEVICE 公开读取接口 | 7 passed，0.84 秒 | 内容截短或替换、原绑定缺失或不匹配、serial 不符，以及真实超时和取消后的进程退出与临时文件收场。 |

独立 doctor 的实际目标分别为 `apps/camctl/tests/unit/devices/test_hasselblad_availability.py` 和 `apps/camctl/tests/integration/devices/test_hasselblad_availability.py`，位于上述独立分支。中断补充实验直接调用公开 `HasselbladDriver.check_availability`，仅将可执行工具替换为临时脚本。

DEVICE 安装版目标为：

```text
apps/camctl/tests/integration/bootstrap/test_hasselblad_device_distribution.py::test_installed_device_photo_reclaims_then_downloads_both_sources_twice[raw]
apps/camctl/tests/integration/bootstrap/test_hasselblad_device_distribution.py::test_installed_device_photo_reclaims_then_downloads_both_sources_twice[jpeg]
```

DEVICE 公开读取目标为下列三个节点的全部参数，分别为 2、3、2 项：

```text
apps/camctl/tests/integration/devices/test_hasselblad_download.py::test_partial_or_replaced_content_is_rejected_before_session
apps/camctl/tests/integration/devices/test_hasselblad_download.py::test_original_binding_and_serial_fail_before_download
apps/camctl/tests/integration/devices/test_hasselblad_download.py::test_real_timeout_or_cancel_waits_for_process_exit_before_removing_partial
```

两组 DEVICE 实验使用独立源码及 Python 环境，集成资源同步与构建相互隔离；bootstrap 和 devices 分别运行。读取实验经公开 `open_read` 入口观察实际结果，安装版实验经真实 init/run CLI 进入业务。

## 未取得的恢复证据

`tests/integration/test_hasselblad_photo_roundtrip.py::test_installed_hasselblad_missing_start_result_never_replays` 没有完成目标恢复。第一次执行受旧 host 启动提示文案阻挡；隔离副本只校正该提示后，已经进入真实故障门，工具替身执行了一次拍摄并形成两份文件。重启时，测试恢复包装访问已删除的 `CaptureRuntime.owned`，引发 `AttributeError`；实际目标恢复没有运行，最终为 1 failed。

该失败能够定位到过时测试包装，不能据此判定生产恢复存在相同缺陷。它也没有证明生产恢复正确。尤其不能将已经出现两份文件、旧进程结束或异常捕获解释为未知 START 已经可靠收场。原始失败日志保留在收据目录的 `missing-start-adjusted.log`。

另三组既有测试分别有 8、6、6 项在目标行为之前因 `CaptureRuntime(..., owned=...)` 装配失败。它们没有取得预期的释放、恢复或报告生成证据；完整失败摘要分别保留在 `legacy-capture.log`、`legacy-start-release.log` 和 `legacy-report.log`。本次没有删除用例、放宽断言或恢复私有运行时接口来制造通过。

## 证据边界与实施后的门禁

doctor 中断实验中，驱动把取消转换成 unavailable，并保留实际退出与输出；业务结果拥有者继续交付原结果并向等待者传播取消的组合尚未验证。该实验也没有覆盖工具忽略 SIGTERM 后的升级终止。

报告登记解释没有证明真实报告字节生成和历史重建。现有 DEVICE 正常链没有经过新的检查责任或 `UNKNOWN + RELEASED`，不能作为新恢复功能已经完成的证据。无设备、多设备时的 DEVICE 读取及新的同机并发交错仍需实现后验证。

正式设计的 [HA-01—HA-12](hasselblad-availability-recovery.md#实施责任与联合验收)是完整交付门禁。其中 HA-01 必须通过公开业务入口重新形成原 START 未保存的中断场景，取得实际本地收场边界、原未知结果、原文件保留、零重放，以及后续独立检查与新工作推进的证据。新操作的次数、取消、原子释放、所有资源守卫和历史恢复也必须通过实际实现验证。

本记录没有提供 ARM 目标环境、真实相机 doctor、物理断电或拍摄恢复时间的验收结论。doctor 的业务含义仍受公开工具契约及正式设计采用的运行假设限制。
