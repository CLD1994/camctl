# Action6 延时、预览与设备源删除实施计划

> **执行方式：** 使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans` 按任务执行；复选框只在实际实现与验证后勾选。

**目标：** Action6 从同一正式合同提供普通录像、原生有限延时、视频 LRF 预览和独立设备文件删除，能力导出与实际执行一致。

**架构：** 复用现有任务、完成上下文、分页归属、产物规则、文件清理和恢复机制。型号适配只负责命令、响应、固定任务及文件格式，不增加公共协议或数据库表。

**技术栈：** Python 3.11、JSON Schema Draft 2020-12、现有 ADB 受管调用、SQLite、TypeScript 客户端及 C host 联合测试。

**规格：** [拍摄参数](../../architecture/camera-parameters.md)、[驱动执行与产物边界](../../architecture/camera-capabilities.md#驱动执行与产物边界)、[按时间与产物判定完成](../../architecture/camera-capture.md#按时间与产物判定完成)、[预览取得](../../architecture/preview-obtaining.md)、[产物清理](../../architecture/output-cleanup.md)、[硬件命令资料](../../hardware/camera-control-handoff.md)。实施先将下列有效契约同步到责任专题。内部命名、拆分及预计文件是实现建议，可依实际数据流调整。

## 全局约束

- 新普通录像请求的光圈和码率必填；保留已实现的精确光圈非阻断策略、真实设置调用历史和码率命令接受语义。恢复消费原生效参数，不重新受理。
- 普通录像保留 1000 ms 启动余量、停止后 5 秒文件等待和原任务产物规则。
- 原生延时复用 Action6 自身参数域及完整 `6c` 模板，不增加普通录像的光圈、码率、视野或防抖参数。
- 原生延时使用设备有限目标、`SENT`、`TIME_AND_OUTPUTS`、启动余量 0、目标后结果余量 30 秒；提前停止后文件等待 30 秒。两段 30 秒是运行假设，不能表述为实测保证。
- 延时 STOP 为 `dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 00`。Action6 的设置、START、STOP 仅接受严格完整 `00`，普通录像光圈例外只保留在原操作范围。
- 新正式任务固定内部 `/mnt/media_rw/emulated/DCIM`；设备状态查询继续关闭。已保存任务范围不被当前配置改写。
- 视频预览仅为唯一同目录、同 stem 的 MP4/LRF；后缀识别不区分大小写，名称其余部分精确匹配。JPG、DNG 是独立原片，不生成照片预览。
- 单文件删除遵守原绑定及精确文件身份，不级联删除配对文件、延时照片或目录。
- 只验证软件组合，不访问真实相机；既有 Action6 固件问题及现场验证暂停继续保留。
- 同一工作区只保留一个写入者。组件集成测试按目录分别启动并串行执行，其他代理只读评审。

## 评审重点

1. 静态能力说明不能依赖真实请求的 `outputs`；缺失真实请求字段不能被补成默认值。任务 1 验证真实 `describe` 与缺字段拒绝。
2. 自然完成不能再睡眠一次，STOP 等待必须来自原活动及原结果页。任务 2、3 验证上下文、跨页等待及持久化来源。
3. 预览扫描不能改变主扫描游标或将历史视频冒充本次原片。任务 2、3 验证跨页、基准及配对冲突。
4. RAW/JPEG 不能相互满足冻结规则，零字节及必要元数据错误不能被静默跳过。任务 1、2、3 验证缺件及部分结果。
5. 删除能力必须包含独立查询、匹配证据及恢复授权，且不开放活动查询。任务 3 验证真实合同、精确删除及声明导出。

### 结果跨页历史修复契约

主结果游标和 LRF 配对扫描继续分开。正式结果适配器在派发后页设备调用前，按同一 `AttemptTicket` 逐页核验已可靠保存的前页；校验页号、结果页历史引用、页间游标、设备绑定、当前目录范围和无错误状态，并要求末页 `next_cursor` 等于本次输入游标。首页只能从没有已保存页的票据开始。任何缺页、保存未交接、读取错误或不一致都在零次新增设备调用的情况下失败。

通过核验后，读取器通过只读回调按需逐页访问前页，并只枚举历史结果页 `file_ids` 引用的条目，因此既有基准文件不会进入同次尝试的历史。回调不进入 JSON 参数，正式 `session_capture_assembly` 必须连接结果仓储和会话连接；孤立的合成驱动适配器可不提供回调。适配器和读取器每次最多持有一页及固定末页引用，不缓存全目录。读取器只在可选扫描可靠结束并确认匹配原片数为零后消费历史；历史缺失、扫描失败、扫描未完成和候选歧义都不能折叠为空历史或原片消失。

| 完整扫描 | 当前页匹配原片 | 同票据新增前页匹配原片 | 结果 |
| --- | --- | --- | --- |
| 恰有唯一原片 | 不限 | 不读取 | 正常按精确目录与 stem 配对。 |
| 失败、超时或候选不唯一 | 不限 | 不读取 | 保留原可选扫描诊断，不推断缺失。 |
| 确认零原片 | 有 | 不读取 | 结果冲突、保留当前部分页、`set_finalized=false`。 |
| 确认零原片 | 无 | 有 | 同样返回结果冲突和非最终化当前部分页。 |
| 确认零原片 | 无 | 已核验为空或无匹配项 | 保持可选 `missing_pair`。 |
| 确认零原片 | 无 | 后页无历史提供器 | 返回 `history_unavailable` 类错误和非最终化部分页。 |

本修复不得增加公共机器协议或数据库结构，不得放宽差集过滤、主结果游标、32 项主目录批量上限、128 条调用诊断上限或可靠保存前页要求。

## 任务 1：接入有限延时任务与严格响应

**预计文件：** `apps/camctl/src/camctl/devices/drivers/adb_cameras/action6_record.py`、`commands.py`、`contracts.py`、`responses.py`、`definitions.py`；必要时新增 Action6 格式定义模块。测试位于 `apps/camctl/tests/unit/devices/test_action6_record_contract.py`、`test_action6_responses.py`、`test_adb_camera_commands.py` 和真实登记集成测试。

**接口建议：** 保持 `action6_record_contract() -> CameraContract` 的登记入口，新增 `task_factories["action6_timelapse"]`。能力说明通过纯任务元数据入口消费与真实工厂共用的固定任务定义，不以缺失 `outputs` 的假请求调用真实工厂。文件格式映射同时供规则和读取器使用。

| `outputs` | 冻结的必需产物规则 |
| --- | --- |
| `video` | 至少一个非空 MP4，不限定确切数量。 |
| `video_raw` | 上述 MP4，另有至少一个非空 DNG 照片。 |
| `video_jpeg` | 上述 MP4，另有至少一个非空 JPEG 照片。 |

不按时长除以间隔推算固定张数。可靠归属的其他实际照片仍可登记，不能替代所需格式。

- [x] 写失败测试：三种输出与两种曝光的任务规则、固定内部范围、30 秒规则、有限取消及普通录像原时间规则；真实工厂缺 `outputs` 拒绝，静态 `describe` 成功。
- [x] 写失败测试：延时 START 只产生 `timelapse_sent`，STOP 只接受 `00`；`e3`、`ee`、`df`、畸形或多响应均不确认。验证 Action6 完整 STOP 命令及不继承 OSMO 例外。
- [x] 运行最窄目标并记录失败，再接入任务、纯说明入口、命令及响应登记；保留原 v1 证据。
- [x] 运行上述单元目标及 `apps/camctl/tests/integration/devices/test_adb_camera_registration.py`，确认 Catalog 与运行合同的正式任务一致。

## 任务 2：接入原片与有界 LRF 配对读取

**预计文件：** 新增 `apps/camctl/src/camctl/devices/drivers/adb_cameras/action6_results.py`；按必要范围提取 `osmo360ii_results.py` 的共享读取机制。保留 `record_results.py` 中其他消费者仍使用的公共辅助函数。新增 `apps/camctl/tests/unit/devices/test_action6_results.py`，复用 OSMO 结果测试结构。

**接口建议：** `async list_action6_results(driver, request: ControlRequest, batch: int) -> DeviceCallResult`。请求仍只含活动、范围、游标及完成上下文；读取实际 MP4/JPG/DNG，不扩展请求传递原始 `outputs`。固定规则由原受理任务验证。Action6 新结果证据使用 `result_returned/v2`，保留旧 v1；OSMO 格式及证据版本保持原样。

- [x] 写失败测试：合法自然上下文不再等待；普通 STOP 按原 5 秒上下文，延时 STOP 按 30 秒上下文，后页引用首次真实等待；过早、跨活动、非法引用不建立完成事实。
- [x] 写失败测试：MP4/JPG/DNG 独立原片、零大小保留、必要元数据失败返回真实部分页；100 多张图片分页不攒全目录，诊断调用记录有界。
- [x] 写失败测试：LRF 两种跨页顺序、精确同目录同 stem、重复候选、基准原片、主扫描观察后的原片消失，以及可选扫描/元数据失败。可选预览失败保留必要原片。
- [x] 写真实 SQLite 分页失败测试：前页 MP4 消失后后页 LRF 不能最终化；覆盖前页保存/恢复读取、其他尝试隔离、缺页与游标/范围伪造、历史基准差集过滤、裸后页无历史失败及可靠空历史仍保持可选缺配对；Action6 和 OSMO 包装均覆盖。原设备调用必须在历史不一致前置时为零。
- [x] 运行失败目标，实施最窄格式声明与共享读取；主扫描顺序和游标保持，第二阶段只提供配对身份。
- [x] 运行 Action6 与 OSMO 结果单测，再运行 `apps/camctl/tests/integration/devices/test_osmo360ii_result_pages.py` 及新增 Action6 真实合同与 SQLite 分页组合测试。

## 任务 3：文件删除、恢复与正式软件链

**预计文件：** Action6 合同、登记及相关测试；`tests/integration/_action6_builtin_adb.py`、`test_action6_builtin_demo_roundtrip.py`、`test_device_capability_declarations.py`；必要时新增独立延时测试模块。同步责任规格、当前计划状态及组件验证记录，不改写历史现场观察。

**接口：** 直接装配既有 `file_command_factory(FILE_QUERY/DELETE)`、`FILE_COMMAND_EVIDENCE`、`query/delete` 恢复操作及原版本恢复证据。公共删除与预览字段由完整合同导出，不增加客户端型号清单。

- [x] 写失败测试：真实 Action6 声明提供 FILE_QUERY/DELETE 及恢复、保持活动 QUERY 关闭；精确删除 MP4、LRF 或单张照片后其余文件存在；错误、未知及原尝试恢复沿通用清理路径处理。
- [x] 写真实合同的持久化集成测试：自然结束沿原目标及 30 秒核实，提前 STOP 及 30 秒只等待一次；首次 STOP 失败沿有限目标保留真实错误，缺可靠 SENT 不补造锚点；保存重送不重新拍摄。
- [x] 扩展安装版 ADB 替身识别 Action6 自身完整命令，文件访问仍在真实临时目录运行 find/stat/读写脚本。新增有限延时代表链，核对视频原片、LRF 预览、照片原片及独立删除；普通录像回归保留原时间断言。
- [x] 更新正式能力预期，运行安装版 `describe` 至真实客户端表单/导出的组合；必填光圈码率及新增延时来自实际 Schema。保留通用可选字段和 Hasselblad 设备源不支持删除覆盖。
- [x] 同步当前规格的延时任务、文件格式、等待假设和用户可选/驱动固定边界；将硬件资料中缺少 STOP 的当前说明更新为完整命令。历史硬件未知状态和验证暂停保持。
- [x] 串行运行设备单元、设备集成、ADB acceptance、相关 bootstrap、安装版 Action6/OSMO 往返、能力客户端及清理联合目标。只在后续变更或失败产生新风险时追加测试。
- [x] 运行文档链接检查、人工核对新增锚点及 `git diff --check`，并由只读评审者核对实际代码、测试和日志后登记软件证据边界。

运行命令遵守 [camctl 测试指南](../../../apps/camctl/tests/AGENTS.md)和[跨组件测试入口](../../../tests/integration/README.md)。本计划完成不代表 ARM、相机固件或真实设备的完成与删除行为已经验收。
