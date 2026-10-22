# camctl 配置项参考

[返回 camctl 资料入口](README.md) · [配置加载与通信策略](../architecture/configuration.md) · [首次部署与联调](../architecture/deployment-example.md)

本页面向部署人员和第三方主程序集成者，汇总当前 camctl CLI（命令行接口）的 TOML 配置字段、类型、默认值、合法范围和实际生效边界。字段及默认值依据[配置加载器](../../apps/camctl/src/camctl/bootstrap/config.py)和内建驱动整理；调度、恢复、文件交接等业务规则继续由各责任专题定义。执行计划中的拍摄参数、`policy` 和主程序的 C 配置分别有自己的输入边界。

## 选择配置文件

`init`、`run`、`submit`、`describe` 共用以下加载规则。默认配置文件为运行 CLI 账户的 `$HOME/.camctl/config.toml`。安装到哪个目录，不决定 CLI 使用哪个用户主目录。

调用方式或文件状态|结果
-|-
省略 `--config`|读取默认配置文件。
提供 `--config <path>`|只读取指定文件；相对文件名按当前进程的工作目录解释。
所选文件不存在、为空或只有注释|使用内置默认值；不寻找其他文件。
只提供部分字段或合法空表|逐字段覆盖默认值，省略项继续使用默认值。
文件无法可靠读取、TOML 解析失败、字段类型或取值非法|按配置错误结束调用；不使用默认值掩盖错误。

选择另一份配置文件只改变配置来源，省略的数据路径仍按 CLI 的用户主目录生成。配置每次启动时加载，进程运行期间固定；更新前须停止相关 CLI 调用并确认已有进程退出，详见[配置更新规则](../architecture/configuration.md#本地配置的加载与更新)。配置加载成功也不代替首次 `camctl init`。

## 值的写法

下文表格列出完整字段名；例如 `log.level` 在 TOML 中写为：

```toml
[log]
level = "INFO"
```

- 整数项不接受布尔值、小数或数字字符串；没有列出上限的字段只执行所列下限校验，不表示任意大值都有运行保障。
- 秒数项接受有限整数、小数或相应数值字符串，例如 `3`、`0.5`、`"0.5"`；按表中要求区分正数和非负数。概率也接受数值或数值字符串。
- 字节容量项使用字符串，例如 `"128 MiB"`；单位支持 `B`、`KiB`、`MiB`、`GiB`、`KB`、`MB`、`GB`，区分大小写，换算结果必须是整数个字节。完整语法见[容量配置](../architecture/configuration.md#容量配置的表示)。
- 根配置和已定义的通用子表拒绝未知字段。设备声明的驱动扩展字段按[设备配置](#设备与驱动)处理。

## 文件路径

字段|类型与默认值|作用与约束
-|-|-
`paths.state_db`|非空字符串；`"$HOME/.camctl/state.db"`|状态库文件；部署时使用明确的绝对路径。
`paths.log_file`|非空字符串；`"$HOME/.camctl/camctl.log"`|Python camctl 的运行日志文件。
`paths.staging`|绝对目录字符串；`"$HOME/.camctl/staging"`|由 camctl 准备文件并保存主机正式源。
`paths.ready`|绝对目录字符串；`"$HOME/.camctl/ready"`|camctl 发布完整待传文件，主程序从这里领取。
`paths.processing`|绝对目录字符串；`"$HOME/.camctl/processing"`|主程序领取后的文件位置。

以 `$HOME/` 开头的路径使用 CLI 的用户主目录展开。直接填写的绝对路径保持原有基准，不拼接用户主目录；配置文件所在目录也不作为路径基准。`staging`、`ready`、`processing` 须为非空、不含 NUL（空字符）的绝对路径，规范化后不能相同或相互包含。`state_db`、`log_file` 的加载校验只要求非空字符串；使用绝对路径可避免后续文件操作依赖进程工作目录。

三个交接目录的移动须满足[原子移动条件](../architecture/file-handoff.md#发布原子性)。目录路径与状态库绑定：已有部署更换任一交接目录时，应先完成依赖旧目录的文件和责任，暂停 CLI、领取和传输，再修改配置并针对原状态库显式执行 `init`；成功后才恢复主程序。完整条件见[目录绑定与切换](../architecture/configuration.md#文件目录的绑定与变更)。

## 会话与日志

字段|类型与默认值|合法范围与作用
-|-|-
`session.plan_poll_interval_ms`|整数；`500` 毫秒|至少 1；检查新计划的轮询间隔。
`session.wakeup_margin_ms`|整数；`50` 毫秒|至少 0；提前唤醒余量，0 表示关闭；不提前执行业务动作。
`session.read_discovery_batch_size`|整数；`100` 条|至少 1；限制每批读取发现的候选详情数量，不限制计划或产物总数。
`log.level`|字符串；`"WARNING"`|`"DEBUG"`、`"INFO"`、`"WARNING"`、`"ERROR"`。
`log.max_size`|容量字符串；`"10 MiB"`|换算后至少 1 字节；单日志文件的轮换阈值。
`log.file_count`|整数；`3` 份|至少 1；包含正在写入的日志文件。
`log.queue_capacity`|整数；`1000` 条|正整数；每个进程各自的日志队列容量。
`log.queue_low_watermark`|整数；`700` 条|正整数；达到后停止接纳 `DEBUG`，对 `INFO` 采样。
`log.queue_high_watermark`|整数；`800` 条|正整数；达到后停止接纳 `INFO`。
`log.info_sample_probability`|数值或数值字符串；`0.1`|闭区间 `[0, 1]`；两个水位之间每条 `INFO` 被选中的概率。

日志队列的完整配置须满足 `0 < log.queue_low_watermark < log.queue_high_watermark < log.queue_capacity`。修改容量时，省略的水位仍使用默认值，不会自动按比例调整。日志接纳、轮换、丢弃和多进程共用的规则见[运行日志](../architecture/logging.md)。

## 数据库、历史与文件处理

字段|类型与默认值|合法范围与实际作用
-|-|-
`database.queue_capacity`|整数；`64` 条|至少 1；加载器接受，当前 CLI 的数据库执行尚未读取该值，不能依此调节实际队列。
`database.busy_timeout_ms`|整数；`9000` 毫秒|1 至 2147483647；当前用于启动只读核验及报告读取连接；普通业务写连接仍使用数据库层默认值。
`database.wal_autocheckpoint_pages`|整数；`1000` 页|1 至 2147483647；加载器接受，当前 CLI 尚未将该值传入写连接，实际连接使用数据库层默认值。
`history.snapshot_change_threshold`|整数；`64` 次|至少 1；加载器接受，当前 CLI 尚未接入按对象变化触发的快照维护。
`history.snapshot_batch_size`|整数；`16` 个对象|至少 1；加载器接受，当前 CLI 尚未接入对应的快照批量维护。
`history.entity_batch_size`|整数；`32` 个实体|至少 1；报告恢复和生成时的实体读取批量。
`history.event_batch_size`|整数；`256` 条事件|至少 1；报告恢复和生成时的历史事件读取批量。
`copy.segment_size`|容量字符串；`"128 MiB"`|换算后至少 1 字节；每段可靠拷贝进度的保存粒度。
`cleanup.max_delete_attempts`|整数；`3` 次|至少 1；每份正式产物删除的累计尝试上限，包含第一次。
`cleanup.max_query_attempts`|整数；`3` 次|至少 1；每份正式产物清理查询的独立累计尝试上限。
`cleanup.work_file_batch_size`|整数；`32` 条|至少 1；中间文件历史清理的单批检查数量。
`cleanup.work_file_limit_per_run`|整数；`128` 条|至少 1；一次正常运行检查中间文件的总预算。

数据库参数的设计要求见[数据库执行](persistence-runtime.md)与[历史存储](history-storage.md)；上表单独注明当前 CLI 的实际接入边界。历史读取与快照规则见[历史查询](historical-state-query.md)和[快照维护](history-snapshots.md)。文件处理规则见[分段拷贝](../architecture/file-copy.md)、[正式产物清理](../architecture/output-cleanup.md)和[中间文件清理](../architecture/file-handoff.md#中间文件清理的运行预算)。

## 来源检查次数

字段|类型与默认值|合法范围与作用
-|-|-
`obtain.max_source_check_attempts`|整数；`3` 次|至少 1；来源检查的全局尝试上限，包含第一次。设备子表不能覆盖此上限。

此值从本次启动加载的 `ConfigSnapshot.obtain` 读取；其内置默认值只在配置加载器的 `ConfigDefaults` 中定义。本字段目前只完成配置读取与校验，尚未接入来源查询调度或某次 operation attempt，因此不代表具体查询已使用该次数预算。

## 时钟与 ADB 客户端

字段|类型与默认值|合法范围与实际作用
-|-|-
`clock.min_plausible_date`|字符串；`"2025-01-01"`|严格的 `YYYY-MM-DD` 有效日期；最早可信日期。
`clock.lower_bound_tolerance_s`|秒数；`5`|有限正数；加载器接受，当前 CLI 时间检查尚未读取该值。
`clock.recheck_delay_s`|秒数；`5`|有限正数；时间复核的等待间隔。
`clock.recheck_count`|整数；`1` 次|至少 1；时间复核次数，不含首次检查。
`clock.recovery_wait_cap_s`|秒数；`60`|有限非负数；时钟异常受限收场时，录像额外等待的上限。
`adb.terminate_grace_s`|秒数；`3`|有限正数；终止本地主机 ADB 客户端时，升级为强制终止前的宽限。

日期必须加引号，TOML 的日期对象不作为该字段的字符串接受。ADB 客户端宽限是主机进程的收场参数，不是相机命令超时。完整规则见[时间检查](../architecture/clock-recovery.md)、[录像恢复](../architecture/camera-recovery.md)和[ADB 终止宽限](../architecture/configuration.md#本地-adb-客户端的终止宽限)。

## 设备与驱动

`devices` 默认为空表；不声明设备也可以初始化并执行无设备的报告流程。每个 `devices.<id>` 使用稳定、非空的设备标识。设备标识会被计划和历史引用，应在部署中保持稳定。

### 设备身份与连接

字段|类型与默认值|合法范围与作用
-|-|-
`devices.<id>.kind`|必填非空字符串，无默认值|当前相机部署使用 `"camera"`；其他种类没有相机动作目录。
`devices.<id>.driver`|必填非空字符串，无默认值|须为已登记驱动；当前内建标识见下表。
`devices.<id>.adb.serial`|字符串，无默认值|ADB 相机实际执行时必须有明确 serial；非空、不以 `-` 开头且不含 NUL，不按设备列表顺序选择。
`devices.<id>.connection.prepare_lead_s`|扩展字段，无当前生效的默认值|加载器仅保留该表，未专项校验类型或范围；当前 CLI 不读取该值，不能用它改变设备准备时机。

驱动标识|当前设备及部署配置|能力边界
-|-|-
`dji-action6`|DJI Action6；使用 `devices.<id>.adb.serial`|内建普通录像及原生有限延时摄影，延时参数类型为 `action6_timelapse`。
`dji-osmo360-ii`|DJI OSMO 360 II；使用 `devices.<id>.adb.serial`|内建普通录像及原生延时摄影。
`hasselblad_cfv100c`|Hasselblad CFV 100C；使用下面的 `hasselblad` 子表|单张拍摄，可选择主机或设备正式源；格式、曝光、白平衡及对焦按[单张参数规格](../superpowers/specs/2026-10-20-hasselblad-photo-parameters-design.md)解释，实际部署能力以导出结果及验收记录为准。

设备声明外层允许驱动扩展字段，并不保证未识别字段会产生作用；下面列出的已定义子表会校验结构及未知键。ADB serial 在加载器中仅在提供 `adb` 子表时检查，部署时仍须为两个 ADB 驱动填写该表。每个驱动能执行哪些动作，以相同配置导出的 `camctl describe` 为准；内建能力存在不表示具体设备和目标主机已经完成验收。

### 视频结果模式的设计合同

[公共视频结果设计](../superpowers/specs/2026-10-23-video-result-modes-and-segmentation-design.md#部署配置与受理)定义 `devices.<id>.video.result_mode` 的值、默认值及错误边界。Action6 与 OSMO 360 II 均按每台相机统一选择模式，同时作用于普通录像及原生延时；受理后的任务使用自身冻结选择。

该配置及公共层重构属于待实施设计。部署时须核对相应实施交付及安装版能力说明；已有 OSMO 分段软件验证不证明部署模式切换已经实现。

### 控制、查询与结果核实

以下字段位于每台设备的子表中。所有整数次数至少为 1，包含第一次；重试间隔和修复余量允许 0，调用时限必须大于 0。秒数均须有限。共用拍摄流程采用 `recording` 子表中的启动配置，是否需要停止、查询或结果列举由具体驱动和任务决定。

字段|类型与默认值|作用
-|-|-
`devices.<id>.recording.max_start_attempts`|整数；`3` 次|启动控制的累计尝试上限。
`devices.<id>.recording.max_stop_attempts`|整数；`3` 次|正常停止控制的独立累计尝试上限。
`devices.<id>.recording.start_timeout_s`|正秒数；`10`|一次启动控制调用的等待上限。
`devices.<id>.recording.stop_timeout_s`|正秒数；`10`|一次停止控制调用的等待上限。
`devices.<id>.recording.start_retry_interval_s`|非负秒数；`3`|具备重试资格后，再次启动前的等待。
`devices.<id>.recording.stop_retry_interval_s`|非负秒数；`3`|具备重试资格后，再次停止前的等待。
`devices.<id>.recording.repair_margin_s`|非负秒数；`10`|采用单原片媒体处理的任务判断多录修复的余量；多段模式按公共流程及设备特化选择整段，不使用该裁剪余量。
`devices.<id>.query.max_attempts`|整数；`3` 次|每项设备状态查询责任的累计次数。
`devices.<id>.query.retry_interval_s`|非负秒数；`3`|同一状态查询责任的再次查询间隔。
`devices.<id>.query.timeout_s`|正秒数；`10`|一次设备状态查询调用的等待上限。
`devices.<id>.residual_stop.max_attempts`|整数；`3` 次|残留录像收场的独立停止次数。
`devices.<id>.residual_stop.retry_interval_s`|非负秒数；`3`|同一残留收场责任的再次停止间隔。
`devices.<id>.residual_stop.timeout_s`|正秒数；`10`|一次残留收场停止调用的等待上限。
`devices.<id>.result_check.max_attempts`|整数；`3` 轮|每项拍摄结果核实责任的累计轮数。
`devices.<id>.result_check.retry_interval_s`|非负秒数；`3`|结果尚不可靠时的再次核实间隔。
`devices.<id>.result_check.call_timeout_s`|正秒数；`10`|每次设备结果查询调用的等待上限；同一轮的各次分页调用分别计时。
`devices.<id>.capture.extra_wait_ms`|整数；`0` 毫秒|至少 0；在驱动要求的拍摄结果等待之后增加部署余量。

`query` 表控制设备状态查询，`result_check` 表控制拍摄结果核实，下面的 `cleanup.query_*` 控制文件存在性查询，三者不是同一责任。当前三个内建驱动都没有声明设备状态查询能力，因此加载器接受 `query` 字段，但这些驱动不发起相应的设备状态查询。`capture.extra_wait_ms` 只用于任务声明了返回后等待的结果分支，不改变目标采集时长或正常停止时间。

超时不证明设备未执行，重试间隔届满也不授予重复调用资格；次数和已知结果在恢复时继续保留。完整规则见[控制超时](../architecture/configuration.md#控制调用超时)、[查询与核实](../architecture/configuration.md#状态查询与产物核实的配置)、[通信重试](../architecture/configuration.md#通信重试间隔)和[录像恢复](../architecture/camera-recovery.md)。

### 读取、重拷与文件清理

字段|类型与默认值|合法范围与作用
-|-|-
`devices.<id>.copy.max_read_attempts`|整数；`3` 次|至少 1；每份文件累计读取尝试次数。
`devices.<id>.copy.max_recopies`|整数；`1` 次|至少 0；摘要不一致后的额外整片重拷次数，0 表示不重拷。
`devices.<id>.copy.read_idle_timeout_s`|正秒数；`10`|连续没有新文件数据的等待上限；不限制整片总传输时间。
`devices.<id>.copy.retry_interval_s`|非负秒数；`3`|再次读取前的等待间隔。
`devices.<id>.obtain.source_check_timeout_s`|正秒数；`10`|设备来源检查的单次调用时限；只能设置在对应设备子表中。
`devices.<id>.obtain.source_check_retry_interval_s`|非负秒数；`3`|设备来源检查再次尝试前的间隔；0 保留为不额外等待。
`devices.<id>.cleanup.delete_timeout_s`|正秒数；`10`|一次设备文件删除调用的等待上限。
`devices.<id>.cleanup.query_timeout_s`|正秒数；`10`|一次设备文件存在性查询调用的等待上限。
`devices.<id>.cleanup.delete_retry_interval_s`|非负秒数；`3`|确认可以再次删除后，重发删除前的等待。
`devices.<id>.cleanup.query_retry_interval_s`|非负秒数；`3`|文件存在性查询失败后，再次查询前的等待。

设备文件清理的次数来自全局 `cleanup.max_delete_attempts`、`cleanup.max_query_attempts`；每台设备的 `cleanup` 子表提供时限和间隔。当前内建 Action6、OSMO 驱动提供设备文件删除与存在性查询；Hasselblad 未提供这两个驱动端口。主机正式源的查询与清理由本地文件流程处理，不采用设备调用时限。完整规则见[文件拷贝](../architecture/file-copy.md)与[设备文件删除和查询](../architecture/configuration.md#设备文件删除与查询的计时)。

来源检查的设备秒数从同一配置加载器的 `ConfigDefaults` 取得默认值，并沿设备执行子表的统一秒数校验规则规范为精确 `Decimal`。显式 `None`、非有限值、无效数值或不符合正数／非负范围的值都会作为配置错误拒绝。当前只加载和校验这些值，尚未接入来源查询执行；因此不能据此推断某次设备调用的实际超时或重试行为。

### Hasselblad 专属字段

这些字段只用于 `driver = "hasselblad_cfv100c"`，且 `kind` 必须为 `"camera"`。两个 Hasselblad 设备声明不能使用同一个 serial。

字段|类型与默认值|合法范围与作用
-|-|-
`devices.<id>.hasselblad.executable`|必填字符串，无默认值|非空、不含 NUL、以 `/` 开头的绝对路径；指向主机的 `hb_cli`。
`devices.<id>.hasselblad.serial`|必填字符串，无默认值|非空且不含控制字符；执行前核对机身身份。
`devices.<id>.hasselblad.product_id`|整数；`10`|1 至 65535；USB 产品标识。
`devices.<id>.hasselblad.output_storage`|字符串；`"host"`|`"host"` 或 `"device"`；选择新受理拍摄的正式源位置。

`output_storage` 在首次受理时固定到动作执行定义，后续修改部署配置不改变旧动作的来源。Hasselblad 工具路径采用明确绝对路径，不使用 `$HOME` 或 `~` 缩写；完整规则见[Hasselblad 接入](hasselblad-driver.md)。

### 设备配置示例

将下面的标识和工具路径替换为实际部署值；只保留需要接入的设备。serial 占位字符串仅用于说明配置格式，不表示已确认设备连接。

```toml
[devices.action6]
kind = "camera"
driver = "dji-action6"
[devices.action6.adb]
serial = "REPLACE_ACTION6_SERIAL"

[devices.osmo360ii]
kind = "camera"
driver = "dji-osmo360-ii"
[devices.osmo360ii.adb]
serial = "REPLACE_OSMO360II_SERIAL"
[devices.osmo360ii.recording]
start_timeout_s = 15
start_retry_interval_s = 3

[devices.hb0]
kind = "camera"
driver = "hasselblad_cfv100c"
[devices.hb0.hasselblad]
executable = "/opt/hb_cli/hb_cli"
serial = "REPLACE_HASSELBLAD_SERIAL"
product_id = 10
output_storage = "host"
```

拍摄类型与分辨率、帧率、曝光、延时摄影间隔等要求由计划动作的 `params` 表达；其合法结构从实际 `describe` 取得，不添加到上述设备部署表中。

## C 主程序的配置边界

`camctl_host_config` 是 C 接入模块的配置结构体，不属于 TOML。模块使用 `camctl_path` 启动 Python CLI；通过 `config_path` 为每次 `run`、`submit` 添加同一个 `--config`。路径与日志的对应关系如下：

C 成员|对应对象|配置责任
-|-|-
`camctl_path`|已安装的 camctl 可执行程序|主程序填写绝对路径；不决定 Python 的用户主目录或数据路径。
`config_path`|CLI 的 TOML 文件|非 `NULL` 时显式传入；`NULL` 时由 CLI 选择默认文件。
`ready_path`|TOML 的 `paths.ready`|主程序从这里领取；须与 Python 实际使用的目录一致。
`processing_path`|TOML 的 `paths.processing`|主程序将领取的文件移入这里；须与 Python 实际使用的目录一致。
`log_path`|C 接入模块的日志|与 TOML 的 `paths.log_file` 分别配置，分别写入各自日志。

C 接入层不把 `ready_path`、`processing_path` 转换成 Python 配置。自定义交接目录时，两端须同时设置。`camctl_host_config_set_home_paths` 只为 `NULL` 的必填路径生成 home 下的默认值，已经提供的路径原样保留；其 `home` 参数不修改 CLI 子进程的环境。C 字符串中的 `$HOME` 和 `~` 不由接口展开。

初始化成功时模块已复制配置，后续修改调用方结构体不改变正在运行的实例。C 数值参数、容量范围和路径缓冲区寿命见[公开配置接口](../host-demo/implementation.md#部署参数与公开接口)及[源码与接入说明](../../apps/host-demo/README.md#配置范围与日志)。

## 自定义交接目录示例

以下示例用于运行账户 home 为 `/home/user` 的首次部署。camctl 安装、状态库、日志和 `staging` 采用该账户的默认位置，`ready`、`processing` 使用主程序指定的目录。部署人员须准备权限，并确认三个目录位于支持原子移动的同一文件系统；已有部署按[目录切换规则](../architecture/configuration.md#文件目录的绑定与变更)操作。

在原配置中合入以下内容，保留原设备和其他配置：

```toml
[paths]
ready = "/var/opt/SatService/dt_outbox"
processing = "/var/opt/SatService/dt_sending"
```

主程序在调用 `camctl_host_init` 前填写：

```c
camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
config.camctl_path = "/home/user/.camctl/venv/bin/camctl";
config.config_path = "/home/user/.camctl/config.toml";
config.ready_path = "/var/opt/SatService/dt_outbox";
config.processing_path = "/var/opt/SatService/dt_sending";
config.log_path = "/home/user/.camctl/host.log";
```

以同一运行账户初始化，再启动主程序：

```bash
/home/user/.camctl/venv/bin/camctl init \
  --config /home/user/.camctl/config.toml
```

初始化成功后，新会话的报告和普通交付文件发布到 `/var/opt/SatService/dt_outbox`；调用 `camctl_host_claim` 后移到 `/var/opt/SatService/dt_sending`。主程序负责后续传输和处理。

## 核对配置是否被选中

先核对主程序传入的 `config_path`、实际文件内容和 CLI 的运行账户。配置文件不存在时允许使用默认值，因此文件名拼错也可能表现为使用默认目录。`describe --config <path>` 可以检查配置解析和设备能力，但不会展示全部生效配置，也不检查状态库的目录绑定；目录初始化和切换结果以 `init` 的退出状态与诊断为准。

字段覆盖核对从[通用配置解析](../../apps/camctl/src/camctl/bootstrap/config.py)、[文件与路径适配](../../apps/camctl/src/camctl/bootstrap/application.py)及[内建驱动](../../apps/camctl/src/camctl/devices/drivers/builtin.py)开始。维护配置实现时同步核对本页，实际业务含义仍按各节链接的责任专题解释。
