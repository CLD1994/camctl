# Hasselblad 真机验证与 H6 部署交接（2026-10-28）

本记录对应[无人值守恢复计划 H6](../superpowers/plans/2026-10-28-hasselblad-availability-recovery.md#h6-部署交接与目标设备验收)。本次直接生成正式 `plan.json`，由安装版 C host 管理 camctl，经过受理、调度、受管工具、登记、报告与 `claim` 文件领取。未运行客户端，没有新增或运行软件集成测试。现场发现的软件缺陷另用隔离单元验证；这些结果与真实相机、媒体及供电结果分别记录。

## 环境与交接

目标目录为 `/home/cld/.camctl/manual-validation-20261028`。原部署收据固定源码 `6132bd70b28552eaa97ddba24e80961a4c363569`，Linux x86_64、Python 3.11.17、SQLite 3.53.1；初始 wheel 的 SHA-256 为 `a300af0919fbb9ab2df9c76bd39572be4b884c68820debcea23a32b3deff63ca`。独立核对 wheel 中 386 个包文件，与安装副本及部署源码快照逐字节一致，详见环境内 `validation/installed-package-audit.json`。后续修复的构建、安装及复验另行记录，不把初始发行物证据套用到后续版本。

相机固定序列号为 `64010904`、产品标识为 `10`，工具参数为 `--pid 0x000a`。实际 `info` 返回固件 `v4.1.0`、body-type `152`、焦距 `28`；没有取得镜头型号和镜头序列号，不外推其他镜头或固件。工具为仓库 `tools/hb_cli`，二进制 SHA-256 及首次调用原始双流保存于 `validation/preflight.json` 和相关 `.stdout`、`.stderr` 文件。

首次 USB 节点为 `/dev/bus/usb/001/005`，属组为 root；`info` 返回 `LIBUSB_ERROR_ACCESS`。按仓库工具说明将该节点属组调整为当前用户所属的 `plugdev` 后，原机身身份核对和 doctor PASS 均通过。用户重启相机后节点变为 `001/006`，再次调整该节点属组并核对原机身及 PASS；这不构成持久 udev 规则部署。ADB 路由与其他设备配置保持原部署配置。

运行前核对同机没有 host-demo、camctl、hb_cli 进程。每组用例完成报告后执行 `claim`，再通过 `quit` 等待 host 及其 camctl 收场，串行使用相机。初始配置保留 `output_storage=host`、30 秒准备预算及 30 秒提前量；DEVICE 组使用独立配置文件选择 `output_storage=device`，状态库与交接目录保持一致，未同时运行两个设备控制环境。单张执行定义固定的 START 期限为 180 秒；query 生效值为 3 次、3 秒间隔、单次共用 10 秒期限。正常检查实际三条命令耗时能容纳于该期限；默认值不能承诺任意断电时长。

旧环境只读交接结果见 `validation/old-environment-handover.json` 与 `old-pending-details.json`。20261025／20261026 没有 pending 或 running 动作。20261027 的请求 `1793113200543195284` 尚有动作 7—9：拍摄 7 未执行、无操作责任和正式产物，原窗口为 2026-10-27 15:00:20 UTC 起 600 秒，已结束；取回 8／9 处于 running、没有已登记操作，仍引用原来源。这三个动作保留在旧库，不迁移、不重送、不延长窗口；新验证均用新请求身份。读取前后旧数据库及原 WAL 摘要一致，旧环境保持停止，未接续其客户端状态或确认水位。

## 初始发行物的实际计划结果

各组原始计划、最后报告、host 终端输出、请求身份及进程退出结果位于环境的 `validation/<组名>/`。表中的“成功”是相应业务动作或工具步骤的结果；媒体解码另见后文。

| 组名 | 输入及实际结果 |
| --- | --- |
| `01-host-raw-m` | HOST RAW，M、ISO100、f/8、0.008 秒、auto、MF 不移动。拍摄、原图与预览取回成功，形成 delivery 1／2。 |
| `02-host-jpeg-a` | JPEG、A、ISO200、f/8、手动 5500 K、AF-S。准备设置成功，autofocus 返回 `camErrFindFocus`，零 capture，动作失败。 |
| `02b-host-jpeg-a-mf` | 同一曝光与白平衡，改为 MF。capture 返回成功，但原图因 EOI 不在文件末尾被拒绝；动作 7 为结果未确认，没有登记正式产物。 |
| `03-device-raw-s` | RAW、S、ISO100、0.000667 秒、daylight、MF +5。工具快门回显为 1/1000 秒；focus 返回 `camErrDbusCallFailure`，零 capture。 |
| `04-device-jpeg-p` | DEVICE JPEG、P、ISO100、cloudy、MF -5。调焦命令成功，capture 返回成功，原图仍被 EOI 文件尾条件拒绝；动作 13 为结果未确认。 |
| `05-device-raw-mf` | RAW、S、0.000333 秒。快门设置返回 `camErrParamNotFound`，后续白平衡、对焦、capture 均未派发。 |
| `07-host-repeat-no-binding` | 当前配置中 `devices={}`，再次取回动作 1 的原图和预览，生成独立 delivery 3／4，大小及 SHA-256 与原源一致，零设备调用。它不等于物理断电见证。 |
| `08-device-raw-baseline`、`09-device-raw-repeat` | DEVICE RAW、M、ISO100、f/8、0.008 秒、auto、MF。拍摄成功，原图两次取回成功，独立 delivery 5／7；预览两次各耗尽三轮 READ，未发布交付。 |
| `10-parameter-matrix` | A、ISO64、f/4、2000 K；P、ISO25600、10000 K；S、0.008 秒、fluorescent；M、f/32、tungsten，四项拍摄成功。0.00025 秒在设置步骤被拒绝；32 秒设置成功，capture 返回 `camErrDbusCallFailure`，动作 31 保留结果未知。 |
| `11-capture-process-interrupted` | 原计划意图是在真实 capture 期间中断工具，但实际在可用性检查三次失败后结束，未到达中断点、未发送 SIGTERM；不计为进程中断验收。 |
| `12-binding-rejected` | 同逻辑设备存在旧 `UNKNOWN + HELD`，新配置 serial 改为另一值。动作 33 零新活动、检查与工具调用，但等待 66.36 秒后过期，违反有效准备区间直接绑定失败的规格。 |
| `13-unavailable-finite` | 旧限制保持，临时撤去当前用户的 USB 节点访问权限。三次检查均失败，新拍摄以 `device_availability_unconfirmed` 有限结束，无 START。结束后在 finally 恢复原节点属组。 |
| `14-device-recovery` | 原节点权限恢复后，新 DEVICE 原图取回触发检查，但机身仍 `camErrNoTethering`；检查三次耗尽，逐项以 `source_device_unavailable` 失败，没有 READ 尝试，未改源文件存在性。 |
| `15-af-s-after-adjustment` | 用户调整目标后再提交 AF-S，但仍因可用性失败结束，未到达 autofocus；不能计为调整后的 AF-S 见证。 |
| `17-cancel-before-start` | 未来 75 秒的单张被另一个新请求取消。原拍摄 canceled、取消动作 succeeded，零准备与 START。 |
| `19-parameter-rejection` | 缺 focus、额外 tint、MF 移动量 0、曝光分支字段不适用、格式为 null，五个动作在受理时分别失败，零设备调用。 |
| `21-reboot-capture-recovery` | 用户报告相机重启后，新 RAW 短曝光拍摄和原图取回成功；通过正常业务检查释放旧限制，未重拍动作 31。 |
| `22-af-s-after-restart` | 用户调整目标并重启后，RAW、M、0.008 秒、flash、AF-S 拍摄成功。 |
| `23-mf-positive-after-restart` | RAW、M、0.008 秒、MF +5 拍摄成功；不从命令成功推断精确焦点位置。 |
| `24-cancel-during-start` | 1 秒曝光的真实 capture 在途时提交取消；取消逐项返回 `task_cancel_unsupported`、`not_applied`，原拍摄成功，没有重拍或关机调用。 |
| `25-mf-negative-auto-preview` | RAW、M、0.008 秒、MF -5 及同刻的显式 auto_preview 关联，拍摄和自动预览取回成功。 |
| `26-device-delete-unsupported` | 对本轮 DEVICE 预览 output 4 发起明确清理。按未声明删除能力的契约失败，不调用机内删除，不将原文件标记为不存在。 |
| `27-idempotent-original-request` | 原请求 1 的计划文件经 host 再次递交，计划、动作、活动、产物、delivery 和操作尝试数量全部不变，新增 external_tool 事件为 0。 |

四种曝光模式、八种白平衡模式、手动色温两端、MF 不移动／正向／负向、AF-S，以及 ISO、光圈边界均经过真实设置或拍摄。不能把分支见证扩展为全部离散快门、ISO、光圈的逐档设备验收；0.000667 秒的工具回显也不能替代准确曝光读回。

## 未知活动、重启与有限失败

初始发行物的可用性 check 1／2 分别由动作 10／16 触发，成功尝试为 operation_attempt 9／14；真实 `list → info → doctor` 完整成功后，旧 activity 3／5 的占用从 HELD 变为 RELEASED，活动仍为 UNKNOWN。其旧动作 7／13 的失败和 `device_execution.status=end_unconfirmed` 保持，原 JPEG 工作文件保留。

32 秒拍摄失败后，机身仍可列出，但 doctor 连续返回 `camErrNoTethering`。用户随后报告相机卡在关机界面；当时立即停止后续设备调用并保存 `validation/hardware-blocked/` 中的一致数据库备份、日志、配置及摘要。没有调用 `hb_cli stop`，没有直接修改数据库状态，也没有用文件存在代替拍摄成功。

用户之后报告相机已经重新启动。再次核对原 serial 和 PASS 后，动作 44 的普通拍摄触发 check 7，operation_attempt 51 释放 activity 13。旧 activity 13 保持 UNKNOWN，动作 31 仍 failed、`end_unconfirmed`；动作 44 的新拍摄及原图交付成功。此前已经失败的检查和触发动作没有重开。首次上电与断电时刻、断电方式及实际离线持续时间没有取得，不能把这项记录称为受控物理断电持久性验收或晚于窗口恢复验收。

检查次数、检查失败与 READ 次数分别保存。持续不可用的 DEVICE 请求没有借用或消耗尚未派发 READ 的预算；之前预览下载长度不符则按 READ 自己的三次预算结束。报告保持这两种不同失败原因。

## 实际媒体与内容一致性

首次 HOST RAW 原图为 210,767,872 字节，SHA-256 为 `4e86ce1f506d8a0c6c2bb2c94c37611b0f701002e7a63ee00d0b2e65c666859d`；预览为 642,889 字节，摘要为 `cab68fd80cc7dc77d7ffd660aa8ccb57c3c313824706df121af186385c47627d`。首次操作结果、归档正式源及两次独立交付的相应大小、摘要相同。首次预览的 ffprobe 宽高为 0，实际 ffmpeg 解码退出 69；损坏在首次驱动文件核验时已经存在，归档与交付没有改变字节。第一版不默认逐图解码，因此动作成功不能代替真实媒体验收。

两个被初始程序拒绝的 JPEG 原图均可用 `ffmpeg -v error -xerror -i <file> -frames:v 1 -f null -` 解码，尺寸为 11656×8742；配对预览为 3888×2918。最后一个 EOI 后分别有 875 和 2647 字节；这两份样本不能被描述为“不可解码”。独立下载 `B0000273.JPG` 后，原图与拍摄副本均为 7,086,080 字节，SHA-256 均为 `412f60a3e77ffe213264e4a61eb804a66d825813fe0bf33e48732c70ae6deb5b`。

DEVICE 预览的现场内容保证未通过：

| 来源 | 拍摄时固定的主机预览 | 独立下载结果 | 结论 |
| --- | --- | --- | --- |
| `B0000273.JPG` | 376,511 字节，SHA-256 `93795e9f4a9204a27ccd31873c5e319eab391ffb5cc7ace03df4252caf15764d` | 376,832 字节，SHA-256 `49aa9fbd3c803dc074a493c9348800d3667dd97930cb1db69db11b5531f7c05f` | 拍摄预览恰为下载文件前缀，但完整文件不相同；不能截短下载后称原完整内容一致。 |
| `B0000275.3FR` | 642,889 字节，固定摘要 `0bf7be69907ab1687df33a2ab1952a8f71f46b8366a5baedf29d8ba50c9201eb` | 两次均为 237,568 字节，SHA-256 均为 `36776b78a745699a7a2aab5906e34a9b64ba73d0ea77210c0ec99a8411cf5144`，下载内容可解码 | 下载本身稳定，但不等于拍摄时固定的源内容；camctl 的长度拒绝符合原契约。 |

完整下载命令、原始 stdout／stderr、文件与解码结果位于 `validation/16-tool-content-diagnosis/`。这些事实尚不能定位相机、USB、厂商工具的内部根因。没有重定义源摘要、忽略多余字节或把视觉相似记为通过。

## 软件修复及后续复验

绑定变化的分类、独立守卫和既有检查有限收场，以及 JPEG 外层结束识别，已按正式 H6 子任务推进。用户确定了检查因绑定变化无法继续时的 FAILED 收场与新请求规则，也确定 JPEG 尾部数据应原样保留。实施、隔离验证、审查、构建安装和安装版复验在完成后补入本节；未完成的工作不由上述初始发行物真机结果代替。

## 当前验收边界

H6 尚未勾选。DEVICE 预览的完整内容一致性、32 秒拍摄的可靠行为及准确快门参数仍未通过；受控断电时间、恢复晚于窗口、物理断电持久性、第二机身及多设备数量分区、ARM64 目标主机均没有实际见证。未知 START 返回丢失的真实进程中断未到达原故障点，HA-01—HA-12 的全部软件交错、事务故障及历史重建组合也未执行。客户端按用户要求不在本次范围。

已通过的单个参数分支、原图传输、doctor、动作状态及文件摘要各自只支持其记录范围。后续修复后的安装版和真实媒体结果须另外核对，不能仅凭旧计划成功、静态资源相同或隔离单元通过宣布完整功能验收完成。
