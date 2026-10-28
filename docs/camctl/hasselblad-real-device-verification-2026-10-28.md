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

## 未知活动、重启与有限失败

初始发行物的可用性 check 1／2 分别由动作 10／16 触发，成功尝试为 operation_attempt 9／14；真实 `list → info → doctor` 完整成功后，旧 activity 3／5 的占用从 HELD 变为 RELEASED，活动仍为 UNKNOWN。其旧动作 7／13 的失败和 `device_execution.status=end_unconfirmed` 保持，原 JPEG 工作文件保留。

32 秒拍摄失败后，机身仍可列出，但 doctor 连续返回 `camErrNoTethering`。用户随后报告相机卡在关机界面；当时立即停止后续设备调用并保存 `validation/hardware-blocked/` 中的一致数据库备份、日志、配置及摘要。没有调用 `hb_cli stop`，没有直接修改数据库状态，也没有用文件存在代替拍摄成功。

用户之后报告相机已经重新启动。再次核对原 serial 和 PASS 后，动作 44 的普通拍摄触发 check 7，operation_attempt 51 释放 activity 13。旧 activity 13 保持 UNKNOWN，动作 31 仍 failed、`end_unconfirmed`；动作 44 的新拍摄及原图交付成功。此前已经失败的检查和触发动作没有重开。首次上电与断电时刻、断电方式及实际离线持续时间没有取得，不能把这项记录称为受控物理断电持久性验收或晚于窗口恢复验收。

检查次数、检查失败与 READ 次数分别保存。持续不可用的 DEVICE 请求没有借用或消耗尚未派发 READ 的预算；之前预览下载长度不符则按 READ 自己的三次预算结束。报告保持这两种不同失败原因。

## 实际媒体与内容一致性

首次 HOST RAW 原图为 210,767,872 字节，SHA-256 为 `4e86ce1f506d8a0c6c2bb2c94c37611b0f701002e7a63ee00d0b2e65c666859d`；预览为 642,889 字节，摘要为 `cab68fd80cc7dc77d7ffd660aa8ccb57c3c313824706df121af186385c47627d`。首次操作结果、归档正式源及两次独立交付的相应大小、摘要相同。首次预览的 ffprobe 宽高为 0，实际 ffmpeg 解码退出 69；损坏在首次驱动文件核验时已经存在，归档与交付没有改变字节。第一版不默认逐图解码，因此动作成功不能代替真实媒体验收。

另在独立诊断环境使用 rawpy／LibRaw 读取首次 HOST RAW 原图和第 32 组保留的 RAW 原图，两者均能解码，传感器有效数组为 8750×11664；半尺寸 RGB 输出为 4375×5832。两份 RAW 内嵌 JPEG 由 LibRaw 直接提取后均可解码，而工具生成的相应拍摄预览不可解码，进一步区分了原图与预览问题。诊断没有修改原图、没有向生产环境增加依赖，也不把其结果回填成原拍摄成功；工具版本、原文件及派生内容摘要见 `validation/37-raw-decoder-diagnosis/result.json`。

两个被初始程序拒绝的 JPEG 原图均可用 `ffmpeg -v error -xerror -i <file> -frames:v 1 -f null -` 解码，尺寸为 11656×8742；配对预览为 3888×2918。最后一个 EOI 后分别有 875 和 2647 字节；这两份样本不能被描述为“不可解码”。独立下载 `B0000273.JPG` 后，原图与拍摄副本均为 7,086,080 字节，SHA-256 均为 `412f60a3e77ffe213264e4a61eb804a66d825813fe0bf33e48732c70ae6deb5b`。

DEVICE 预览的现场内容保证未通过：

| 来源 | 拍摄时固定的主机预览 | 独立下载结果 | 结论 |
| --- | --- | --- | --- |
| `B0000273.JPG` | 376,511 字节，SHA-256 `93795e9f4a9204a27ccd31873c5e319eab391ffb5cc7ace03df4252caf15764d` | 376,832 字节，SHA-256 `49aa9fbd3c803dc074a493c9348800d3667dd97930cb1db69db11b5531f7c05f` | 拍摄预览恰为下载文件前缀，但完整文件不相同；不能截短下载后称原完整内容一致。 |
| `B0000275.3FR` | 642,889 字节，固定摘要 `0bf7be69907ab1687df33a2ab1952a8f71f46b8366a5baedf29d8ba50c9201eb` | 两次均为 237,568 字节，SHA-256 均为 `36776b78a745699a7a2aab5906e34a9b64ba73d0ea77210c0ec99a8411cf5144`，下载内容可解码 | 下载本身稳定，但不等于拍摄时固定的源内容；camctl 的长度拒绝符合原契约。 |

完整下载命令、原始 stdout／stderr、文件与解码结果位于 `validation/16-tool-content-diagnosis/`。这些事实尚不能定位相机、USB、厂商工具的内部根因。没有重定义源摘要、忽略多余字节或把视觉相似记为通过。

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

当前程序尚需按这一来源规则调整 DEVICE 来源事实与副本复用责任。既有分段视频检查副本的有限复用规则已经定位；Hasselblad 的原图、预览及同步工作区需要接入相应的结果责任和清理。相机按用户安排用于充电，软件调整期间保持零设备调用，之后再安排新规则的真机复验。

来源生产链的阶段快照为 `c9627f55f6186ad3275a6f45f9d7ae5220782e48`，包含冻结模式、DEVICE 控制结果、真实 RESULTS 角色端口、页后文件责任及来源结果处理。主代理在独立固定副本、Python 3.11.17 上复跑四组隔离单元：`test_hasselblad_device_response_isolated.py`、`test_hasselblad_frozen_storage_isolated.py`、`test_hasselblad_device_results_isolated.py`、`test_photo_result_workspace_isolated.py`，57 项通过，退出 0；日志为 `.local/verification/h6-real-device-20261028/source-c9627f55-unit.log`。命令设置 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`、`PYTHONDONTWRITEBYTECODE=1`，显式使用该副本的源码 `PYTHONPATH`、部署版本解释器及 `pytest -p no:cacheprovider`。运行前已静态核对导入前的资源与协作者隔离。独立完整评审仍在进行，一次比较、H 等待者缓存和原工作目录维护尚未完成；本快照未合入或安装，不代表真实事务、完整来源链或设备验收通过。

## 充电期间的离线收口

相机充电期间未调用设备。再次审计现有证据，45 份报告均通过公共 Schema 与文件名 SHA-256 核对；18 条 delivery 中有 14 份实际交付文件，大小与固定摘要全部相符，另外 4 条对应此前失败且未发布的预览交付。17 份不同 JPEG 内容中 8 份可解码，其余继续保留实际失败，不以传输字节相同替代媒体有效。SQLite `integrity_check` 为 `ok`，`foreign_key_check` 为空。结果和旧审计副本分别位于 `validation/audit/result.json`、`result-before-source-authority.json`。

下一轮四种来源／格式及后到重复取回的生成入口已准备为环境内 `validation/source_authority_cases.py`；默认只列组名，尚未执行新组或发起设备调用。实际执行时生成新请求和有效窗口，DEVICE 同计划放置每角色两个独立等待取回，再以新请求取回同一来源，结合实际历史核对 H、缓存选择和后到设备读取。新来源修复完成、独立审查及重新安装后，才安排这轮真机流程。

## 当前验收边界

H6 尚未勾选。DEVICE 预览的完整内容一致性、32 秒拍摄的可靠行为及准确快门参数仍未通过；受控断电时间、恢复晚于窗口、物理断电持久性、第二机身及多设备数量分区、ARM64 目标主机均没有实际见证。未知 START 返回丢失的真实进程中断未到达原故障点，HA-01—HA-12 的全部软件交错、事务故障及历史重建组合也未执行。客户端按用户要求不在本次范围。

下表把现有现场分支映射到[完整 HA 验收契约](hasselblad-availability-recovery.md#实施责任与联合验收)，不将一个分支的成功记为整项通过。软件组合、真实相机和物理供电各自保留证据边界；来源规则调整后需要重新安装并执行相关现场流程。

| 条目 | 已有现场分支 | 尚缺的主要见证 |
| --- | --- | --- |
| HA-01 原责任先收场 | 旧 UNKNOWN 活动在新检查后释放，原动作和工作文件保持。 | 原 START 返回丢失及 RESULTS 在途的真实进程中断；第 11 组没有到达原中断点。 |
| HA-02 完整检查分类 | 原机身身份与 doctor PASS、绑定不符拒绝、真实 USB 访问失败。 | 零台、多台、身份与输出异常、退出和整次期限的完整分类组合。 |
| HA-03 原子释放与报告 | 释放后旧活动 UNKNOWN、原失败和 `end_unconfirmed` 保持。 | 多目标、提交失败或未知、部分放行拒绝及对应报告边界。 |
| HA-04 多活动与共享预算 | 有限检查和 READ 各自累计，终态检查不重开，同请求重送不新增操作。 | 多目标和多候选跨页共享、独立动作、耗尽及重启的完整组合。 |
| HA-05 并发与其他设备 | 本次仅串行使用单台 Hasselblad。 | 正式七行交错矩阵及其他设备同时推进的组合；未执行。 |
| HA-06 取消与窗口 | 准备前取消成功，真实 START 在途取消被拒绝，原拍摄继续。 | 创建、派发、完整返回及释放提交两侧的取消、时间变化和迟到返回组合。 |
| HA-07 故障与历史 | 受理重送幂等，绑定恢复后旧终态和旧文件不改写。 | 原进程中断、各事务提交未知、三路径历史重建及固定报告字节。 |
| HA-08 HOST 路径 | 原版本 RAW/JPEG 取回、无绑定取回、指定原图清理和配对独立保留。 | 新来源版本的 HOST 文件权威及零相机文件访问；缺失成功返回时的归档恢复。 |
| HA-09 DEVICE 路径 | 原版本原图独立重复取回成功，预览实际长度／内容差异按原预算失败。 | 新版首次来源观察、两角色正式登记、一次副本决定、固定等待者复用及后到相机读取。 |
| HA-10 DEVICE 失败与取消 | 可用性检查耗尽后读取项有限失败，零 READ，源存在性保持。 | 新来源下载中的真实错误、超时或取消收场、部分成功及跨运行恢复。 |
| HA-11 诊断与关闭 | 保存真实工具双流和错误，普通 host 退出后无残留设备进程。 | 队列压力、保存失败、真实工具中断、SIGTERM 升级及终态独立收场组合。 |
| HA-12 结构与环境隔离 | 独立环境、旧实例保留、wheel／安装文件与受保护数据收据。 | 新来源版本的安装复验、不支持结构拒绝和样本保持；ARM64 另需目标主机。 |

已通过的单个参数分支、原图传输、doctor、动作状态及文件摘要各自只支持其记录范围。后续修复后的安装版和真实媒体结果须另外核对，不能仅凭旧计划成功、静态资源相同或隔离单元通过宣布完整功能验收完成。
