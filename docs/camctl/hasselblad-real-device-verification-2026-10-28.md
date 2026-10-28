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
| `14-device-recovery` | 原节点权限恢复后，新 DEVICE 原图取回触发 check 5。前两次尝试出现 `camErrNoTethering`；第三次 info 返回 `camErrSystemState`，serial 等字段含控制字符，身份无法解析，没有继续 doctor。三次耗尽后逐项以 `source_device_unavailable` 失败，零 READ，源文件存在性保持。 |
| `15-af-s-after-adjustment` | 用户调整目标后再提交 AF-S，但仍因可用性失败结束，未到达 autofocus；不能计为调整后的 AF-S 见证。 |
| `17-cancel-before-start` | 未来 75 秒的单张被另一个新请求取消。原拍摄 canceled、取消动作 succeeded，零准备与 START。 |
| `19-parameter-rejection` | 缺 focus、额外 tint、MF 移动量 0、曝光分支字段不适用、格式为 null，五个动作在受理时分别失败，零设备调用。 |
| `21-reboot-capture-recovery` | 用户报告相机重启后，新 RAW 短曝光拍摄和原图取回成功；通过正常业务检查释放旧限制，未重拍动作 31。 |
| `22-af-s-after-restart` | 用户调整目标并重启后，RAW、M、0.008 秒、flash、AF-S 拍摄成功。 |
| `23-mf-positive-after-restart` | RAW、M、0.008 秒、MF +5 拍摄成功；不从命令成功推断精确焦点位置。 |
| `24-cancel-during-start` | 1 秒曝光的真实 capture 在途时提交取消；取消逐项返回 `task_cancel_unsupported`、`not_applied`，原拍摄成功，没有重拍或关机调用。 |
| `25-mf-negative-auto-preview` | RAW、M、0.008 秒、MF -5 及同刻的显式 auto_preview 关联，拍摄和自动预览取回成功。 |
| `26-device-delete-unsupported` | 对本轮 DEVICE 预览 output 4 发起明确清理。按未声明删除能力的契约失败，不调用机内删除，不将原文件标记为不存在。 |
| `27-idempotent-original-request` | 计划 1 对应的请求 `1793174785481481` 经 host 再次递交，计划、动作、活动、产物、delivery 和操作尝试数量全部不变，新增 external_tool 事件为 0。 |

四种曝光模式、八种白平衡模式、手动色温两端、MF 不移动／正向／负向、AF-S，以及 ISO、光圈边界均经过真实设置或拍摄。不能把分支见证扩展为全部离散快门、ISO、光圈的逐档设备验收；0.000667 秒的工具回显也不能替代准确曝光读回。

机器允许域以环境中的 `device-capabilities.json` 为现场导出，以[单张参数规格](../superpowers/specs/2026-10-20-hasselblad-photo-parameters-design.md)为仓库权威定义。实际证据索引如下；曝光与对焦等见证中使用的安装版本按原记录保留，当前 `a9023ec0` 的来源和恢复复验另行标明。

| 功能面 | 实际见证及范围 |
| --- | --- |
| 曝光 M/A/S/P | `10-parameter-matrix/` 的四种模式均完成拍摄；A/P/S/M 分别覆盖手动色温 2000 K、10000 K、fluorescent 与 tungsten。相机自动决定的曝光量没有独立回读。 |
| 白平衡 | `01` auto、`02` manual 5500 K、`03` daylight、`04` cloudy、`10` manual 两端／fluorescent／tungsten／shade、`22` flash 覆盖八种模式；失败组只按已到达的设置步骤计证据。手动色温上下界均在第 10 组拍摄成功。 |
| 对焦 | `01` MF 不移动、`23` MF +5、`25` MF -5 和 `22` AF-S 有真实见证；第 2 组 AF-S 曾失败，第 15 组未到 autofocus，不能把它们算作成功。焦点距离及成像清晰度没有实测读回。 |
| 图片格式与正式源 | `01` 等旧组记录 HOST/RAW，`29` HOST/JPEG、`30` DEVICE/JPEG；当前 `44`—`47` 又覆盖 HOST/JPEG、DEVICE/JPEG、HOST/RAW、DEVICE/RAW，其中 HOST/RAW 失败，三种其余组合成功。格式域是 RAW（3FR）与 JPEG。 |

快门、ISO、光圈允许值分别见能力文件与规格；端点设置已有真实见证。完整 34 档快门目录在第 43、48 组逐档以 PREPARE 尝试受理；这不等同于每档实际曝光验证，其他组中个别快门值的真实拍摄结果仍按各自记录判定。文件操作和恢复见后文 50、56—60 组，均不把设置命令成功扩展成实际曝光或对焦准确性。

## 未知活动、重启与有限失败

初始发行物的可用性 check 1／2 分别由动作 10／16 触发，成功尝试为 operation_attempt 9／14；真实 `list → info → doctor` 完整成功后，旧 activity 3／5 的占用从 HELD 变为 RELEASED，活动仍为 UNKNOWN。其旧动作 7／13 的失败和 `device_execution.status=end_unconfirmed` 保持，原 JPEG 工作文件保留。

32 秒拍摄失败后，机身仍可列出，但 doctor 连续返回 `camErrNoTethering`。用户随后报告相机卡在关机界面；当时立即停止后续设备调用并保存 `validation/hardware-blocked/` 中的一致数据库备份、日志、配置及摘要。没有调用 `hb_cli stop`，没有直接修改数据库状态，也没有用文件存在代替拍摄成功。

用户之后报告相机已经重新启动。再次核对原 serial 和 PASS 后，动作 44 的普通拍摄触发 check 7，operation_attempt 51 释放 activity 13。旧 activity 13 保持 UNKNOWN，动作 31 仍 failed、`end_unconfirmed`；动作 44 的新拍摄及原图交付成功。此前已经失败的检查和触发动作没有重开。首次上电与断电时刻、断电方式及实际离线持续时间没有取得，不能把这项记录称为受控物理断电持久性验收或晚于窗口恢复验收。

检查次数、检查失败与 READ 次数分别保存。持续不可用的 DEVICE 请求没有借用或消耗尚未派发 READ 的预算；之前预览下载长度不符则按 READ 自己的三次预算结束。报告保持这两种不同失败原因。

## 实际媒体与内容一致性

首次 HOST RAW 原图为 210,767,872 字节，SHA-256 为 `4e86ce1f506d8a0c6c2bb2c94c37611b0f701002e7a63ee00d0b2e65c666859d`；预览为 642,889 字节，摘要为 `cab68fd80cc7dc77d7ffd660aa8ccb57c3c313824706df121af186385c47627d`。首次操作结果、归档正式源及两次独立交付的相应大小、摘要相同。首次预览的 ffprobe 宽高为 0，实际 ffmpeg 解码退出 69；损坏在首次驱动文件核验时已经存在，归档与交付没有改变字节。第一版不默认逐图解码，因此动作成功不能代替真实媒体验收。

另在独立诊断环境使用 rawpy／LibRaw 读取首次 HOST RAW 原图和第 32 组保留的 RAW 原图，两者均能解码，传感器有效数组为 8750×11664；半尺寸 RGB 输出为 4375×5832。两份 RAW 内嵌 JPEG 由 LibRaw 直接提取后均可解码，而工具生成的相应拍摄预览不可解码，进一步区分了原图与预览问题。诊断没有修改原图、没有向生产环境增加依赖，也不把其结果回填成原拍摄成功；工具版本、原文件及派生内容摘要见 `validation/37-raw-decoder-diagnosis/result.json`。

两个被初始程序拒绝的 JPEG 原图均可用 `ffmpeg -v error -xerror -i <file> -frames:v 1 -f null -` 解码，尺寸为 11656×8742；配对预览为 3888×2918。最后一个 EOI 后分别有 875 和 2647 字节；这两份样本不能被描述为“不可解码”。独立下载 `B0000273.JPG` 后，原图与拍摄副本均为 7,086,080 字节，SHA-256 均为 `412f60a3e77ffe213264e4a61eb804a66d825813fe0bf33e48732c70ae6deb5b`。

早期 DEVICE 预览同步副本的完整内容一致性未通过：

| 来源 | 拍摄时固定的主机预览 | 独立下载结果 | 结论 |
| --- | --- | --- | --- |
| `B0000273.JPG` | 376,511 字节，SHA-256 `93795e9f4a9204a27ccd31873c5e319eab391ffb5cc7ace03df4252caf15764d` | 376,832 字节，SHA-256 `49aa9fbd3c803dc074a493c9348800d3667dd97930cb1db69db11b5531f7c05f` | 拍摄预览恰为下载文件前缀，但完整文件不相同；不能截短下载后称原完整内容一致。 |
| `B0000275.3FR` | 642,889 字节，固定摘要 `0bf7be69907ab1687df33a2ab1952a8f71f46b8366a5baedf29d8ba50c9201eb` | 两次均为 237,568 字节，SHA-256 均为 `36776b78a745699a7a2aab5906e34a9b64ba73d0ea77210c0ec99a8411cf5144`，下载内容可解码 | 下载本身稳定，但不等于拍摄时固定的源内容；camctl 的长度拒绝符合原契约。 |

完整下载命令、原始 stdout／stderr、文件与解码结果位于 `validation/16-tool-content-diagnosis/`。这些早期副本及其失败仍原样保留，不能定位相机、USB 或厂商工具的内部根因。后续来源调整版另行按真实角色观察决定使用主机同步副本还是重新读取机内来源；新的 DEVICE/JPEG 与 DEVICE/RAW 交付均通过摘要核对，见后文。没有重定义源摘要、忽略多余字节或把视觉相似记为通过。

## 软件修复及后续复验

绑定变化的分类、独立守卫和既有检查有限收场，以及 JPEG 外层结束识别，已按正式 H6 子任务推进。用户确定了检查因绑定变化无法继续时的 FAILED 收场与新请求规则，也确定 JPEG 尾部数据应原样保留。JPEG 修复提交在主工作区为 `8c52f6f32da474f504a44cfb151beb8463dfee5d`。新增反例在旧实现上失败，修复后独立工作区及主工作区的 101 项隔离单元均通过；主工作区命令为 `PYTHONPATH=/workspaces/camctl/apps/camctl/src PYTHONDONTWRITEBYTECODE=1 apps/camctl/.venv311/bin/python -m pytest -p no:cacheprovider apps/camctl/tests/unit/host_files/test_capture_sources_jpeg_boundary.py -q`，退出 0。覆盖外层 EOI、尾部、嵌套元数据、填充、重启标记、多扫描、DNL（行数定义标记）、跨块、截断、完整大小／摘要、源与既有目标分类及 RAW 文件头。Sol 独立复核无剩余阻塞问题；现有集成测试的合法媒体替身同步修正，原业务断言保持，未执行集成测试。

`uv build --project apps/camctl --wheel --python /workspaces/camctl/apps/camctl/.venv311/bin/python --out-dir /workspaces/camctl/.local/delivery/h6-jpeg-20261028 --no-progress` 与冻结依赖导出均退出 0。安装前确认设备进程全部停止，使用 `uv pip install --python <目标>/venv/bin/python --no-deps --reinstall <wheel>`，安装退出 0。wheel SHA-256 为 `a32557dcec86f8935030894619fdc29989a84b35270fcacde96d616046402773`；387 个包文件与当前源码及安装逐字节相同，64 个受保护配置、数据库、媒体、交接、客户端数据与 host 文件的安装前后摘要相同。收据及一致数据库备份位于 `validation/28-jpeg-install/`；未重新 init，没有新增依赖、客户端或 C 构建要求，部署脚本的通用打包与初始化入口不需修改。

| 后续组名 | 新发行物实际结果 |
| --- | --- |
| `29-host-jpeg-fixed` | JPEG 拍摄、HOST 原图与预览登记、取回均成功，完整大小和 SHA-256 覆盖尾部字节。 |
| `30-device-jpeg-fixed`、`31-device-jpeg-original-repeat` | JPEG 拍摄与 DEVICE 原图两次独立取回成功；预览仍按原完整长度拒绝，三次 READ 耗尽后失败，未改变源摘要。 |
| `32-raw-structural-revalidation` | RAW capture 返回成功，原图为 210,776,064 字节，预览为 309,515 字节；预览起始字节为 `ffd8fff639ca…`，外层边界核验拒绝，FFmpeg 实际解码也退出 69。动作 60 为 `capture_result_unconfirmed`，activity 21 为 UNKNOWN／HELD，工作文件保留。不能将它归为新增 JPEG 校验拒绝了已证明有效的 RAW 预览。 |
| `33-host-original-delete` | 仅清理本轮成功 JPEG 动作的指定 HOST 原图；先保留独立取证副本，没有删除旧未知工作文件或机内原图。 |
| `34-host-cleanup-independence` | 原图再次取回失败，配对预览独立取回成功；原图清理不级联预览，已发布交付保持。 |
| `35-host-original-delete-again` | 同一指定原图的新清理请求成功，复用既有清理事实；正式原图路径缺席、预览路径仍在。 |

绑定修复的主工作区提交为 `261dc160`、`89dcbb8e`，测试替身接口补齐与恢复依据说明提交为 `61e73c8ac4e32dc5511664e0dd9f6c0a167926d3`。独立工作区先取得生产入口、守卫和 DEVICE 的失败回归，再修复。Sol 分阶段及收口复核通过；主代理另发现历史检查跨页聚合，已改为每页至多 128 条并逐页消费。原次配置身份与工具实际观察分别表达。

主工作区用 Python 3.11.17、显式源码及测试 PYTHONPATH、`PYTHONDONTWRITEBYTECODE=1`、`pytest -p no:cacheprovider` 分进程执行来源/守卫、准备入口与诊断、协调器与装配、检查等待终结、完整目标事实五组隔离单元，分别为 52、10、40、32、124 项通过，全部退出 0。完整目标文件首轮 121 通过、3 个既有替身缺少 `EventEnvelope.evidence` 报错；原基线同样复现，补齐两个替身的必填字段后 124 项全通过，原断言及参数矩阵保持。精确范围与日志保存在 `.local/verification/h6-real-device-20261028/binding-*.log`；未新增或运行软件集成测试。

从 `61e73c8a` 构建后续 wheel 与冻结依赖导出，均退出 0；wheel SHA-256 为 `d5730d9d7b21b9a42471643e4d26fa2b60384bb93904efc8bd3931d8a0e8295f`。安装及 387 个包文件逐字节核对通过，80 个受保护文件在安装前后大小及摘要保持；未 init、未启动客户端。完整收据位于 `validation/38-final-install/`。

| 安装版复验组名 | 实际结果 |
| --- | --- |
| `36-open-check-before-binding-update` | 先通过真实 USB 访问失败建立 check 8／run 66／触发动作 65；第一项检查结果保存后正常停止 host，保持 ACTIVE、次数 1、重试等待。节点权限随后恢复，未修改数据库标志。 |
| `39-binding-new-and-open-check` | 新版以当前配置中的另一 serial 运行。新动作 66 在有效准备区间绑定失败、执行标记 0，零新活动、检查和操作责任；原动作 65 及 check 8 也以绑定错误结束，原检查次数保持 1。新增 external_tool 事件为 0，旧 activity 21 及工作文件逐字节保持。 |
| `40-binding-original-request-after-restore` | 恢复原配置并重送动作 65 的原请求；动作与检查保持失败、次数不变，零新增工具调用。 |
| `41-device-read-releases-old-unknown` | 新 DEVICE JPEG 原图请求触发 check 9，成功尝试 80 释放 activity 21 后完成读取交付；activity 仍 UNKNOWN，原动作 60 的失败及未知报告保持，工作文件摘要相同，旧 check 8 不重开。 |
| `42-device-source-binding-rejected` | DEVICE 正式源固定机身与本次配置不符，逐项绑定失败，零工具调用。 |

随后按能力说明生成逐档参数准备核验：取同源的 10 档 ISO、13 档光圈与 34 档快门，交错组合而不生成笛卡尔积；仅进行 PREPARE，准备成功后提交正式取消计划，启动时间留有独立余量。用户准备给相机充电时停止该批次，原摘要保存 12 组完整回执、START 数量 0。离线另确认第 13 项（ISO200、f/32、0.5 秒）的 PREPARE 尝试 94 成功，原拍摄及独立取消请求均已正常取消／成功，仍为零 START；生成脚本在轮询期间被中断，没有写出该组 `result.json`。现存公开报告与只读数据库证据另存 `validation/43-catalog-13/recovered-evidence.json`，原 host 退出码及耗时保持未知，不将补充证据冒充完整运行回执。全批次已见证 13 档快门的准备，剩余 21 档待验；这不等同实际曝光。停机后核对所有 host-demo、camctl、hb_cli 已退出，状态库无 pending/running 动作。原摘要保持于 `validation/43-parameter-catalog-summary.json`。

## 正式源与同步副本的后续调整

HOST 以主机正式源为准，不为登记、取回或恢复归档向相机核实文件。DEVICE 以固定原机身、机内原图定位及对应读取种类所得内容为准；同步生成的主机副本只做一次实际一致性确认，可靠保存结论。一致时按结果提交截点固定的等待者有限复用，不一致时固定清理；正式运行不反复比较或重新选择资格。每项取回仍保有独立交付与既有最终校验。

来源调整期间曾误选旧响应测试：在独立工作区执行 `PYTHONPATH=apps/camctl/src /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/devices/test_hasselblad_responses.py -q -k device_capture`，收集阶段因未登记 `registry/enum-registry.json` 退出 2，没有执行用例。导入链实际检查了 `_resources` 目录存在性，未读到资源正文；pytest 缓存、字节码及重定向日志有本地写入，没有业务库、设备或被测程序子进程调用。这次结果不计为符合范围的单元验证。后续用例在导入前隔离资源，并禁用自动插件、缓存和字节码；通过结果及独立复核另行记录。原日志保存于 `.local/verification/h6-real-device-20261028/source-misclassified-collection.log`。

在该阶段记录时，程序仍需按这一来源规则调整 DEVICE 来源事实与副本复用责任。既有分段视频检查副本的有限复用规则已经定位；Hasselblad 原图、预览及同步工作区尚待接入相应的结果责任和清理。后续版本 `0962121e827c8bb4874e865668458f0db387d815` 已包含来源调整并完成正式安装，详见下文。相机充电期间仍不执行设备调用；新规则的真机复验待重新在线后安排。

来源生产链的阶段快照为 `c9627f55f6186ad3275a6f45f9d7ae5220782e48`，包含冻结模式、DEVICE 控制结果、真实 RESULTS 角色端口、页后文件责任及来源结果处理。主代理在独立固定副本、Python 3.11.17 上复跑四组隔离单元：`test_hasselblad_device_response_isolated.py`、`test_hasselblad_frozen_storage_isolated.py`、`test_hasselblad_device_results_isolated.py`、`test_photo_result_workspace_isolated.py`，57 项通过，退出 0；日志为 `.local/verification/h6-real-device-20261028/source-c9627f55-unit.log`。命令设置 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`、`PYTHONDONTWRITEBYTECODE=1`，显式使用该副本的源码 `PYTHONPATH`、部署版本解释器及 `pytest -p no:cacheprovider`。运行前已静态核对导入前的资源与协作者隔离。该快照的独立评审发现绑定派发、文件关闭责任和角色前序三项问题，随后按 H6 修复矩阵补齐。该阶段未实现一次比较、H 等待者缓存及原目录维护，也未合入或安装；隔离结果不代表真实事务、完整来源链或设备验收通过。

后续来源与单角色归档修复覆盖了原准备引用、实际关闭拥有者、原页子责任、重复角色拒绝、绑定变化及多关闭错误诊断；独立审查另发现归档结论的独立守卫缺少完整前状态，已在 `e682ab95` 修复并复核。主代理使用固定快照 `e37776fa23f07d4985f189fa2994eee5a5385eee`（`e682ab95` 加两个既有 JPEG 替身的 `owner=` 参数兼容）分进程复跑必要隔离范围：来源与归档七文件组 102 项、缓存事件前状态 5 项、JPEG 边界 101 项均通过、退出 0。三个日志分别为 `.local/verification/h6-real-device-20261028/source-e37776fa-unit.log`、`cache-guard-e37776fa-unit.log`、`jpeg-e37776fa-unit.log`；解释器和隔离参数同前。既有 JPEG 断言及参数矩阵保持。

开发分支的 H 复用与同一交付回退已形成接线快照。在该开发阶段记录时，仍在补齐摘要不符的回退门禁、源端 READ 的释放保护、用途目录读取边界及原目录维护；当时目标环境安装仍为前述 `61e73c8a`，这些阶段快照未安装。其隔离通过不代表真实事务、完整调度、文件操作或新规则真机结果。后续来源调整版已在 `44-source-authority-install` 中完成安装。

## 来源调整版安装与离线 HOST 复验

2026-10-28 的安装收据 `validation/44-source-authority-install/receipt.json` 固定源码提交 `0962121e827c8bb4874e865668458f0db387d815`。安装发行物为 `/home/cld/.camctl/manual-validation-20261028/validation/44-source-authority-install/camctl-0.1.0-py3-none-any.whl`，SHA-256 为 `c9f2a456dda9111807f0a64cb8acf9e53469ff019ba9373ab96bd5958f1a41e4`；安装退出码为 0。391 个包文件与部署源码及已安装副本逐字节一致，差异为 0；99 个受保护文件在安装前后全部一致。`metadata-check.json` 确认已安装能力说明与构建产物一致，`describe_sha256` 为 `3d34c77c02a3901baefd151625c3c322a6cbc5e4fa39a2b4b258c88971612d21`，10 个已安装依赖均兼容。

安装前的 `validation/source-config-adjustment/` 记录了显式增加 `devices.hasselblad.result_check.call_timeout_s = 180`；其他配置保持一致，调整时没有活动进程。安装收据记录本次安装没有运行 `init`、客户端、设备调用或软件集成测试。

安装后，真实 C host 在相机离线配置下完成 `validation/49-host-offline-after-source-update/`。请求 `1793193801992735` 的 host 退出码为 0，耗时约 10.10 秒；动作 95、96、97 分别取回原 RAW、RAW 预览及 JPEG 预览，三项均成功。交付 19、20、21 的本地目标文件大小和 SHA-256 均与验证记录中的交付摘要一致：原 RAW 为 210,767,872 字节、摘要 `4e86ce1f506d8a0c6c2bb2c94c37611b0f701002e7a63ee00d0b2e65c666859d`；RAW 预览为 642,889 字节、摘要 `cab68fd80cc7dc77d7ffd660aa8ccb57c3c313824706df121af186385c47627d`；JPEG 预览为 241,846 字节、摘要 `33c0d952431e4f9524881d4fbb70e39f9cd3c469eef78674504a7039ceef0d78`。

`verification.json` 显示三项均读取本地中间文件，新增设备检查为 0，诊断工具调用为 0；同时保留了三条成功的 `READ_FILE` 操作记录。因此本组证明新版 HOST 可离线读取并交付这些既有文件，不能表述为“没有 READ”。RAW 预览交付摘要仍与此前记录的预览相同；这次验证只证明字节传输一致，不改变该预览已有的媒体解码失败结论，也不替代新版四种来源／格式组合、有限复用、后到读取及进程中断等 H6 验收。

## 合并及冷恢复修复版安装

2026-10-28 将源码 `a9023ec07348a7a4138625dd29f6358337fbc8e8` 构建并安装到同一环境，收据为 `validation/51-cold-recovery-install/receipt.json`。wheel SHA-256 为 `a123bac055308d07cf58b2a2f1a659f05d4412c65ea3e16ac8fe379c456bac0a`，安装退出 0；392 个包文件与源码及实际安装逐项一致，安装前后 103 个受保护文件的摘要保持一致，另留有一致状态库备份。锁定依赖、SQL 结构资源、枚举及公共能力契约未变，包内权威资源仅更新事件转换登记；沿用既有配置、数据库、C host 和客户端文件，没有执行 `init` 或迁移。

安装版 `describe` 与依赖检查均退出 0，能力说明与原文件按精确数字解析比较一致，10 个依赖兼容；元信息核对后配置、数据库、WAL、客户端能力文件及 host 可执行文件摘要仍未变。完整结果见同目录 `metadata-check.json`，发行物及构建核对见 `build-verification.json`。安装核对本身没有启动 `run`、host 或客户端，没有调用设备或运行软件集成测试。第 49 组离线 HOST 证据属于前述 `0962121e` 安装版。后文四种来源组合、参数准备及进程恢复等正式计划是在 `a9023ec0` 安装后执行的独立业务见证；受控供电时序、恢复晚于窗口及未列出的 H6 组合仍保持待验。

## 合并修复版的在线来源、媒体与恢复验证

`validation/52-online-preflight/` 再次核对了 USB 身份和 doctor；序列号仍为 `64010904`，doctor PASS。随后以 `a9023ec0` 安装版执行四种来源／格式组合。详见各组的 `result.json`、`source-verification.json` 及汇总 `validation/source-authority-verification.json`。

| 组名 | 实际结果 |
| --- | --- |
| `44-host-jpeg-authority` 与 `44-host-jpeg-authority-later` | HOST/JPEG 拍摄成功；两轮共 4 份交付，全部大小与 SHA-256 匹配。HOST 以主机正式源完成交付。 |
| `45-device-jpeg-authority` 与 `45-device-jpeg-authority-later` | DEVICE/JPEG 拍摄成功；共 6 份交付全部匹配。原图同步副本匹配，选作有限缓存；预览同步副本不匹配，选择相机来源观察下载的文件作有限缓存。 |
| `46-host-raw-authority` | HOST/RAW 的 `capture` 正常退出，形成 210,771,968 字节原图及 404,599 字节 JPEG 预览；预览无效，`verify_files` 以源媒体错误拒绝整次拍摄，未发布取回交付。动作 110 的 activity 37 工作文件保留，没有重拍；原图后经 rawpy／LibRaw 确认可解码。 |
| `47-device-raw-authority` 与 `47-device-raw-authority-later` | DEVICE/RAW 拍摄成功；共 6 份交付全部匹配。原图同步副本匹配，选作有限缓存；同步预览无效，选择相机来源观察下载的文件作有限缓存。后到原图第一次 READ 超时并由 SIGTERM 收场，同一 copy 的第二次尝试成功，预览一次成功；本轮耗时 205.32 秒。 |

设备来源审计记录：每个 DEVICE 角色只观察一次来源页；在 H 时四个动作仍为 RUNNING、选择仍为 PENDING，这四个等待者复用对应角色缓存；H 后的新请求直接读取原相机来源。两轮中匹配的原图选用同步副本，不匹配或无效的预览选用相机来源；DEVICE/JPEG 与 DEVICE/RAW 的四份缓存及其 capture-work 均在终态清理。此结果证明当前安装版的 DEVICE 来源选择与角色交付流程成功，不能把旧的同步副本内容差异改写成已修复或把早期失败输出改写成成功。HOST/RAW 的失败也不影响 DEVICE/RAW 对相机原图及预览角色的成功交付。

`validation/53-source-media/result.json` 中五份有效 JPEG 内容实际解码退出码为 0；HOST/RAW 的无效预览解码退出 69。`validation/54-source-raw-decode/result.json` 使用独立环境的 rawpy 0.27.1／LibRaw 0.22.1 解码新交付 DEVICE/RAW `32.3FR` 与 HOST/RAW 失败后保留在 `capture-work/37/` 的原图，两者有效传感器数组均为 8750×11664，内嵌 JPEG 也能解码。诊断未修改原文件，也未向生产环境增加依赖。它证明原图可解码，不把 HOST/RAW 的失败动作改记为成功。

## 参数、进程中断、幂等与清理复验

`validation/43-parameter-catalog-summary.json`、第 13 项的 `validation/43-catalog-13/recovered-evidence.json` 与 `validation/48-parameter-summary.json` 合并覆盖规格中的 34 档快门 PREPARE：32 档被接受，`.000333` 与 `.00025` 两档被拒绝，所有组的 START 总数为 0。新旧记录对 `.000667` 秒请求的工具回显均为 `1/1000`；第 48 组的 `.0005` 秒请求也回显 `1/1000`。日志记录两值分别按精确十进制传给工具，未发现 camctl 格式化舍入；这不是实际曝光读回。这些 PREPARE 结果不证明实际曝光准确性。32 秒拍摄的动作 31 仍为失败／结果未知；第 21 组只在恢复检查后释放旧限制，没有重拍动作 31。

`50-device-results-camctl-interrupted/` 在真实 DEVICE 下载进行中 SIGKILL camctl；host 先确认原进程组收场，再自动启动不带计划路径的 `run`，沿原请求恢复。原 START 仅成功一次；RESULTS 第一次尝试按 `local_result_recovery` 保存 UNKNOWN，第二次尝试成功；activity 60 为 ENDED／RELEASED。RESULTS 未知不表示 START 未知。

`56-device-start-camctl-interrupted/` 在真实 `capture` 进行中 SIGKILL camctl。原 START 按 `local_control_recovery` 保存为 UNKNOWN，未重放；activity 61 保持 UNKNOWN／HELD。后续第 57 组新 JPEG 拍摄、原图和预览取回均成功；检查 11 的第 1 次尝试通过 `list`、`info`、`doctor` 释放了 activity 61 的占用。activity 仍 UNKNOWN，旧 attempts 未变，空工作目录仍在；本次没有形成可信文件，不能据此声称有新媒体字节得到保留。恢复报告仍保留旧动作 failed 与 `end_unconfirmed`，见 `56-device-start-camctl-interrupted/after-followup-report.json`。

第 58 组把第 45 组的原 request 重送，七类业务计数均不变，没有新增 operation 或 attempt。第 59 组成功清理 HOST 原图 output 27；对 DEVICE 预览 output 30 的清理失败，未产生任何 DEVICE `DELETE_FILE` 尝试，设备文件 device_file 6 仍为 PRESENT，完成事实及摘要保持。第 60 组在 `devices={}` 配置下再次取回该 HOST 来源：已清理原图失败，配对预览成功；先前已发布的 delivery 22—25 文件仍全部存在。具体记录分别见 `58-idempotent-device-request/verification.json`、`59-mode-specific-delete/verification.json` 和 `60-host-after-source-delete/result.json`。

本轮最终轻量审计见 `validation/final-online-audit.json`：80 份报告的 Schema 与文件名摘要错误均为 0；40 条 delivery 中，36 个实际文件全部匹配记录大小和 SHA-256。缺席的 delivery 6、8、9、15 均为旧失败且未发布的预览交付；没有已发布文件缺失。SQLite `integrity_check` 为 `ok`，`foreign_key_check` 为空。交付摘要审计不等同于媒体解码，媒体结论仍以第 53／54 组为准。

## HOST 预览保障分工的实现与安装

按[HOST 完成依据](hasselblad-source-authority.md#host-完成依据)，源码 `046d483187ec429ec337f521e3738dbb66ed7152` 在可靠响应确定预览角色后省略 HOST 预览的媒体内容检查，覆盖 RAW 和 JPEG 拍摄。拍摄文件检查与归档恢复采用同一角色分工；原图、DEVICE 来源和同步候选仍履行各自的内容检查，普通文件、长度、完整摘要、稳定性、同步及独立交付的校验责任保持。

Python 3.11.17 下，现有 `test_capture_sources_jpeg_boundary.py`、`test_photo_sync_observation_isolated.py` 与 `test_hasselblad_device_response_isolated.py` 共 174 项通过，退出 0。命令及日志保存在 `.local/verification/host-preview-trust-20261029/`，收据同时归档到现场安装目录。已有驱动隔离用例核对向文件协作者传递已解析预览名；这些结果不证明真实文件归档、已有目标恢复或新规则的完整设备流程。Sol 对最终源码的只读评审未发现必修问题。本次未新增测试用例；既有测试只适配新增参数，软件集成测试未运行。

实际安装记录为 `validation/61-host-preview-trust-install/receipt.json`：wheel SHA-256 为 `e6fdfc07d0de2c987d771a6bce7e3d133d63ff055d93d3ee1e3e4b2a76de732f`，392 个包文件与源码及实际安装一致，安装前后 167 个受保护文件保持一致，并保存安装前状态库备份。与前版 wheel 相比仅两个 Python 文件变化，依赖版本及哈希、包元信息、SQL 和登记资源均相同。导出依赖文件的命令注释不同，初次逐字节比较据实为不等；独立核对排除注释后依赖内容相同，证据见 `dependency-verification.json`。部署 skill 及脚本无需调整。

安装后的 `describe`、依赖检查均退出 0，能力说明按精确数字解析与原文件一致，配置、状态库、WAL、客户端能力文件及 C host 摘要保持；核对时无设备控制进程。安装过程未初始化数据库，未启动 host 或客户端，也未调用相机。相机充电期间未安排新拍摄；新 HOST/RAW 的拍摄、归档及独立取回仍待在线复验。第 46 组的旧失败、文件和媒体诊断保持原事实。

## 当前验收边界

H6 尚未勾选。`a9023ec0` 安装版已经有 HOST/JPEG、DEVICE/JPEG、DEVICE/RAW 的在线业务见证；HOST/RAW 因主机预览无效而失败，不能把四种组合写成全通过。RAW 原图本身经独立诊断可解码。完整 34 档快门目录均有 PREPARE 受理尝试，且两档被拒绝；这不等于逐档实际曝光验证。`.000667` 与 `.0005` 秒请求的工具回显均为 `1/1000`，没有实际曝光读回；32 秒实际拍摄失败。曝光准确性未获验证。camctl 在 DEVICE 下载和真实 `capture` 中的两类 SIGKILL 已覆盖，但不能替代所有事务故障点。用户明确暂缓受控物理供电时序；ARM64 与第二机身目前没有目标。HA-01—HA-12 的完整软件交错、事务提交未知与三路径历史重建组合仍未执行；本轮未新增或运行软件集成测试。客户端仍不在本次范围。

下表把现有现场分支映射到[完整 HA 验收契约](hasselblad-availability-recovery.md#实施责任与联合验收)，不将一个分支的成功记为整项通过。软件组合、真实相机和物理供电各自保留证据边界；新来源规则已安装并执行其真实在线流程。

| 条目 | 已有现场分支 | 尚缺的主要见证 |
| --- | --- | --- |
| HA-01 原责任先收场 | 第 50 组下载中断后以不带计划路径的 `run` 恢复；原 START 只成功一次，RESULTS 第一次尝试为 `local_result_recovery` UNKNOWN、第二次成功，activity 已 ENDED／RELEASED。第 56 组真实 START 中断后为 `local_control_recovery` UNKNOWN 且未重放。 | 其余 START／保存／RESULTS 事务边界及跨运行责任组合仍待验；第 11 组本身仍未到达原门闩。 |
| HA-02 完整检查分类 | 原机身身份与 doctor PASS、绑定不符拒绝、真实 USB 访问失败；第 57 组检查 11 通过并释放第 56 组的旧占用。 | 零台、多台、身份与输出异常、退出和整次期限的完整分类组合。 |
| HA-03 原子释放与报告 | 第 50 组 activity 60 为 ENDED／RELEASED；第 56 组后续检查释放 activity 61 的占用，活动仍 UNKNOWN，动作失败和 `end_unconfirmed` 保持。 | 多目标、提交失败或未知、部分放行拒绝及对应报告边界。 |
| HA-04 多活动与共享预算 | 同请求重送不增加七类业务计数、operation 或 attempt；检查和 READ 次数分开记录。 | 多目标和多候选跨页共享、独立动作、耗尽及重启的完整组合。 |
| HA-05 并发与其他设备 | 本轮串行使用单台 Hasselblad。 | 正式七行交错矩阵及其他设备同时推进的组合；未执行。 |
| HA-06 取消与窗口 | 准备前取消成功，真实 START 在途取消被拒绝；实际进程中断结果按未知保持。 | 创建、派发、完整返回及释放提交两侧的取消、时间变化和迟到返回组合。 |
| HA-07 故障与历史 | 第 50／56 组真实 SIGKILL 分别覆盖 DEVICE RESULTS 与真实 START；58 重送幂等，原失败报告及未知活动保留。 | 各事务提交未知、三路径历史重建及固定报告字节的完整组合。 |
| HA-08 HOST 路径 | HOST/JPEG 四份交付摘要匹配；HOST/RAW 本地源媒体检查失败且未交付。第 60 组证明指定原图清理后原图不可取回、配对预览仍可成功交付，旧 delivery 保持。 | 按当前 HOST 完成依据执行的新 HOST/RAW 成功路径及缺失成功返回时的归档恢复。 |
| HA-09 DEVICE 路径 | 新 DEVICE/JPEG 与 DEVICE/RAW 各六份交付摘要匹配；每角色来源观察一次，缓存选择与后到相机读取有真实见证，缓存及工作目录清理。 | 其他身份、源内容及未完成事务分区仍待验；旧输出同步副本差异作为原始失败保留。 |
| HA-10 DEVICE 失败与取消 | 第 47-later 的原图 READ 超时并由 SIGTERM 收场后第二次尝试成功；预览一次成功。 | 最终失败／取消、部分成功和跨运行恢复的组合。 |
| HA-11 诊断与关闭 | 第 47 组有真实 READ 超时及 SIGTERM；第 50／56 组 SIGKILL 后 host 进程组收场。 | 队列压力、保存失败、SIGTERM 升级及终态独立收场组合。 |
| HA-12 结构与环境隔离 | `a9023ec0` 安装核对 392 个包文件和 103 个受保护文件，元信息检查通过；52 在线身份检查通过。 | 不支持结构拒绝和样本保持；ARM64 当前无目标主机。 |

已通过的单个参数分支、原图传输、doctor、动作状态及文件摘要各自只支持其记录范围。当前安装版的三种来源／格式成功路径、HOST/RAW 实际失败、RAW 原图独立解码和两类进程中断均已分别记录；不能仅凭这些分支、旧计划成功、静态资源相同或隔离单元通过宣布 H6 完成。
