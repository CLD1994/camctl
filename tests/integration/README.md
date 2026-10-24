# 跨组件集成测试

本目录用于客户端、camctl CLI 和 C 接入模块之间的真实协作验证。终端演示使用同一模块；组件内部测试放在各自目录，目前客户端测试入口为 `apps/client/tests`。

完整跨组件回归可以使用[隔离并行入口](../../scripts/README.md#隔离并行集成测试)：`--scope root` 从实际源码发现全部根 pytest 模块，`--scope node` 发现全部 Node 集成文件，默认 `--scope all` 同时包含这两层与 camctl 组件目录。常规模块保持一个进程与一次模块／会话准备；长模块按当前真实收集结果分片，各片拥有独立准备，完整 Hasselblad 十二组正常链不被筛掉。`--target` 可选文件或 pytest nodeid，适合定向验证。

直接运行本目录的测试时，客户端构建可能改写当前工作区的 `apps/client/dist`，wheel 构建会同步 camctl 包资源，因而不能仅凭 pytest 临时目录不同就并发使用同一源码。并行入口为每个任务准备独立源码、临时根和安装环境，为每个执行槽复制独立 uv 缓存与客户端依赖；同一槽内顺序复用依赖。C 构建使用现有辅助中的随机唯一目录或任务临时目录。依赖、Chromium、媒体工具与部署版 Python 3.11 仍须事先准备，入口不连接真实设备。命令、资源边界、进度和失败回执的完整说明由上述入口文档统一维护。

`test_device_capability_declarations.py` 在仓库外离线构建并安装 wheel 与锁定运行依赖，比较安装版和源码的正式能力导出，核对全部内置驱动、Hasselblad 两种正式源、同驱动的独立设备配置，以及任意驱动标识和无拍摄任务设备。真实 `init`、`submit` 与 SQLite 核对配置切换后旧执行定义保持，新请求使用新来源；同一份安装版 JSON 经根 Schema、wheel 资源、客户端实际加载、HTTP、参数表单和设备指南消费。Action6 普通录像表单读取实际必填光圈／码率，原生延时表单读取实际参数类型、三种输出和预览声明；Action6 的 `delete_supported=true`、Hasselblad 设备源的 `false` 也按安装版输出核对。非法删除声明的重载保留旧有效目录，旧说明缺字段仍为未知，有效空目录整体替换。设备工具边界禁止调用，不连接真实相机。

页面联合链使用两份实际安装版导出验证当前源变更、逐设备指南及动作／产物清理入口。非法字段和重复键重载失败后仍按旧有效目录展示；首次加载失败与有效空目录按未知保留入口。混合来源、原片、预览、修复成品和产物状态分别核对，草稿及已导出原请求保持不变。

本用例需要 Python 3.11、uv 离线缓存中的锁定依赖与构建工具，以及已按客户端运行说明安装的 Node.js 24 依赖和 Chromium。浏览器目标由用例先构建当前网页，再通过测试专用 `CAMCTL_TEST_CAPABILITIES_FILE` 注入安装版导出；客户端独立组件测试仍使用自己的合成夹具，不依赖 Python。

```bash
apps/camctl/.venv311/bin/python -m pytest tests/integration/test_device_capability_declarations.py -q
```

`report-dependencies.test.mjs` 与 `report-dependency-conditions.test.mjs` 验证公共 Schema、数据库字段登记、规格 SQL 和检查器之间的接缝，包含错误登记反例与具体条件场景。运行入口见[检查脚本](../../scripts/README.md)；通过这些检查不表示真实 CLI 已经接入报告生成。

`test_database_history_sql.py` 在内存 SQLite 加载真实结构，验证来源及流程关联、清理状态矩阵、启动与残留停止的过期组合、独立文件历史元数据、两类目录和快照范围，并在有无统计信息时核对分页查询。它还读取统一运行库条件，验证检查脚本对主线版本、精确回移例外及非法版本输入的判定；这些版本样例不表示实际运行了所有 SQLite 版本。上述检查不代替生产入口、事件应用、历史恢复或报告字节一致性测试。

`event-transitions.test.mjs` 组合真实事件登记、SQL、枚举和报告依赖，验证列分类、分支和状态覆盖、历史归属及行权限反例。它验证设计检查器，不执行登记引用的业务校验，也不证明生产事件能够共同提交或恢复。

`test_camctl_process_recovery.py` 在 Linux 和部署版 Python 3.11 上构建当前工作区的 C 接入模块，以真实 CLI 和受管工具验证独立组、保留退出记录、原组终止、完整核验、最终回收和下一次 run。信号和扫描同步点只位于 C 系统接口边界；测试覆盖未知状态、提前回收、待启动要求、共享预算、并行 submit 以及模拟服务端脱离与终止的竞争，不使用 Python 谓词代替 C 模块。目标实际工具及第三方主程序仍单独联调。

`test_camctl_host_deployment.py` 验证默认配置与显式缺失配置下的真实 init/run 使用同一状态库，并按独立交付的 README 和 TOML 准备部署，使用真实 CLI 发布报告、C 模块同步领取及原始字节比对。它不连接设备；组件内的交付集成测试另行验证构建、安装、包内容和资料引用，两者共同覆盖第三方接入所需的配置与文件交接。

`test_camctl_directory_switch.py` 使用真实 CLI 和 C 模块完成旧目录报告发布、领取与主程序清理，再由显式 `init` 切换三个绑定目录。新会话向新 `ready` 发布报告，重新配置的 C 模块从该目录领取至新 `processing`；测试核对原始字节、数据库身份及报告编号继续增长。组件内的目录切换测试另外验证历史、ACK、绑定提交中断和目录资格。

`test_action6_builtin_demo_roundtrip.py` 在源码目录之外构建并独立安装 wheel 和锁定依赖，从安装包提取 Action6 录像预设，经真实 C host 完成十秒录像、独立取回、文件与报告领取、客户端导入及累计确认。新请求明确提交必填 `aperture=f2.8` 和 `bitrate=high`；测试核对启动记录中的版本化调用历史保留 `e3` 光圈结果且效果未知，之后仍按序执行码率与 START，并将码率命令只记录为本地已接受。START 返回 `00` 后保存 `SENT` 语义和未知实际开始时刻，1000 毫秒启动余量、至少 11 秒后发出的唯一 STOP、STOP 后实际五秒等待及原请求重送不重复派发保持；结果关联 MP4 与同目录 LRF，照片不生成预览。测试只在 ADB 进程边界提供受接口约束的响应和文件，不连接真实相机；样例媒体不证明设备分辨率、帧率或时长，目标主机验收见[演示操作说明](../../docs/hardware/camera-demo-validation.md#arm-linux-的完整验收)。

### Action6 原生有限延时安装版联合链

`test_action6_builtin_timelapse_roundtrip.py` 使用独立安装 wheel、真实 C host、CLI、SQLite 和受约束的 ADB 文件替身，验证 Action6 原生延时从原 `SENT` 恢复自然完成期限：冻结业务时钟在第 329 秒仍不发 STOP 或读取，第 330 秒后进入结果核实；设备目标保持 300 秒、启动余量为 0、结果余量为 30 秒。`video_jpeg` 输出登记非空 MP4、可选配对 LRF 和独立 JPEG 原片，并分别取回；按精确 output ID 删除 LRF 后 MP4 仍在，删除 MP4 后 JPEG 仍在。该链无真实相机，不证明实际 STOP 或设备端采集；合成预览／照片字节不证明媒体格式质量。

```bash
apps/camctl/.venv311/bin/python -m pytest tests/integration/test_action6_builtin_timelapse_roundtrip.py -q
```

### Action6 分段视频安装版联合链

`test_action6_builtin_segmented_roundtrip.py` 为普通录像和原生延时分别显式配置 `video.result_mode="segmented"`，从仓库外构建并安装 wheel，经真实 C host、CLI、SQLite、客户端报告及媒体导入和累计 ACK 验证完整交付。原单文件用例继续使用默认模式。分段样本跨越 `DJI_001/0999` 到 `DJI_002/0001`，核对全部视频、配对 LRF、native 独立 JPEG 和仅含视频的默认范围。默认取回包含全部原片，预览取回包含配对 LRF 及独立照片；正常任务不执行分段检查拷贝或媒体探测。

测试使用不等长字节样例，只替换受约束的 ADB 进程边界，不连接相机；它核对实际传输、客户端导入和摘要，不证明媒体可解码、实际时长或帧数。每例保存 wheel 摘要、安装模块来源、来源覆盖、产物及交付摘要、客户端导入和 ACK 收据。使用部署版 Python 构建 wheel，避免继承容器中其他解释器：

```bash
UV_PYTHON="$PWD/apps/camctl/.venv311/bin/python" \
  UV_PROJECT_ENVIRONMENT="$PWD/apps/camctl/.venv311" \
  apps/camctl/.venv311/bin/python -m pytest tests/integration/test_action6_builtin_segmented_roundtrip.py -q
```

`test_timelapse_time_parameters.py` 使用 Action6 与 OSMO 360 II 的生产候选 Schema，经测试任务工厂、真实 `describe`、Python 精确校验、Chromium 中的客户端共享模块及 Node.js 后端真实导出，遍历同源时间域并核对精确词元、严格关系、型号曝光及输出。最短参数经过受理、未启动取消、报告发布和客户端导入，验证估算信息不进入生效参数或报告；该路径不执行设备命令。生成器在临时资料副本中验证漂移检查和重新生成。此时间域测试不直接调用正式合同；Action6 和 OSMO 360 II 的正式安装版业务链分别由[Action6 原生有限延时安装版联合链](#action6-原生有限延时安装版联合链)和[OSMO 内置合同联合链](#osmo-360-ii-正式内置合同安装版联合链)验证。

```bash
UV_PROJECT_ENVIRONMENT="$PWD/apps/camctl/.venv311" uv run --frozen --project apps/camctl --group test --python 3.11 pytest tests/integration/test_timelapse_time_parameters.py -q
apps/camctl/.venv311/bin/python apps/camctl/scripts/render_timelapse_time_parameters.py --check
```

`test_hasselblad_photo_parameters.py` 构建并独立安装当前 wheel，由安装版 `describe` 导出 Hasselblad 能力，再将同一份有限参数用例交给安装版 camctl、Chromium 中的客户端共享校验和 Node.js 后端导出。用例只保留有代表性的 RAW/JPEG、M/A/S/P、白平衡、对焦、短快门和色温边界，以及互斥字段、tint、缺项与 `null` 反例；每项保存原始 `params_text` 和独立预期，不复制完整档位。计划只受理，不运行设备工具。

```bash
UV_PROJECT_ENVIRONMENT="$PWD/apps/camctl/.venv311" uv run --frozen --project apps/camctl --group test --python 3.11 pytest tests/integration/test_hasselblad_photo_parameters.py -q
```

### 正式源文件清理的安装版联合链

`test_device_file_cleanup_roundtrip.py` 从仓库外构建并独立安装 wheel，核对正式内置登记、DELETE／FILE_QUERY 工厂、端口及模块来源，经真实客户端导出、C host、CLI、SQLite、文件、报告导入及累计 ACK 验证已有独立动作 `delete_action_outputs`。该模块的 10 个分区覆盖 repaired／OSV／LRF／主机原图／主机预览逐份精确清理、不支持设备源与主机源混合、已清理产物的新请求，以及删除中断、独立预算、原配置保存、提交回执未知、终态伴随和在途取消恢复。外部 ADB 模型只识别已审阅的精确脚本及原目标，不替换生产文件工厂、解释器或状态机。

正式修复样本含两路 H.264 主视频，源与成品均实际运行 ffprobe 和全流解码；STOP 后的 30 秒等待使用真实单调钟。每例独立数据库、远端树和进程组，保存原请求／票据／结果／文件及报告事实。取消分区在结束旧组前断言目标取消已提交、DELETE 仍 RUNNING、结果事件为空、门闩进程仍活动且未发 QUERY，再由新 host 沿原尝试恢复 UNKNOWN，只核实一次，不重放删除。

此清理模块中的 OSMO 来源由测试入口显式声明 `result_policy=null` 和精确一份 OSV，并核对实际冻结值，继续验证单原片修复、修复成品与清理链。正式分段任务由下方独立模块验证。

此模块与原 OSMO 模块可以在同一命令中顺序运行。需要部署版 Python 3.11、uv 缓存及构建依赖、客户端 Node.js 依赖、C 构建工具和可用的 ffmpeg／ffprobe；缺少媒体工具会明确跳过，不能据此宣称完整验收。执行命令为：

```bash
apps/camctl/.venv311/bin/python -m pytest \
  tests/integration/test_device_file_cleanup_roundtrip.py \
  tests/integration/test_osmo_builtin_timelapse_roundtrip.py -q
```

ADB／Hasselblad 文件是受合同约束的软件样例；本链不连接真实相机，也不证明 ARM 工具、实际设备媒体或物理删除。具体软件结果及版本边界见[验证记录](../../docs/camctl/verification.md#设备文件清理的软件联合验收2026-10-18-utc)，现场结果由[清理计划 R7](../../docs/superpowers/plans/2026-10-18-device-file-cleanup.md#r7用已验发行物完成-osmo-目标设备验收)单独跟踪。

### OSMO 360 II 正式内置合同安装版联合链

`test_osmo_builtin_record_segmentation_roundtrip.py` 使用安装 wheel 的正式分段任务，经真实客户端计划导出、C host、CLI、SQLite、领取、报告导入、`Files.upload` 和累计 ACK 验证以下八组软件场景。ADB 进程边界按清单提供多份文件；业务时钟仍采用相邻既有联合链的边界，STOP 后的实际文件等待保持。

| 场景 | 核对内容 |
| --- | --- |
| 正常多段 | 两份原片及两份预览分别登记和交付；扫描提交后终止旧组，再由新进程恢复。 |
| 异常多录 | 六份原片全部保留，默认只交付前三份，明确 ID 另取范围外原片；终态提交后恢复身份不变。 |
| 多录且文件少于选段数 | 可靠控制确认多录，但只有两份原片时交付实际两份；尾段时长保持未知，原始字节完整交付。 |
| 中间缺段 | 来源保持可定位的缺段失败，缺口前两段仍可成功取回；选择固定后恢复不新增条目。 |
| 中间零字节原片 | 空原片及其不合格预览不成为产物，保存真实空文件身份的缺口；默认仍只取缺口前两段。 |
| 首段为空 | 空原片不建立产物，默认来源失败不创建文件目标；报告字节登记后恢复原报告身份、边界、大小和摘要。 |
| 文件顺序未知 | 合格原片保留，默认范围未确认；明确 ID 仍可独立取回。 |
| 原生延时空尾 | 保留两份非零原片及预览，空尾不成为产物；沿原 300 秒目标和 30 秒余量自然完成，拷贝中断后沿原交付与偏移恢复。 |

各组先完整应用报告并发送 ACK，再逐份导入媒体，核对同摘要文件仍按独立交付关联；原请求重送必须实际完成后才检查交付身份不变。设备键与产物编号不同的排序反例由组件的 [test_segment_selection_order.py](../../apps/camctl/tests/integration/outputs/test_segment_selection_order.py) 单独验证：跨批读取按已保存范围序号返回，已清理成员保留自己的失败，不选范围外文件。安装端当前按设备键分配产物编号，两项证据分别记录。

```bash
apps/camctl/.venv311/bin/python -m pytest tests/integration/test_osmo_builtin_record_segmentation_roundtrip.py -q
```

这些场景使用软件字节样例与确定的提交观察门闩，不连接真实相机。ARM、真实文件时长、解码、段间边界和现场异常多录仍按分段计划 P9 独立验收。

每轮将 wheel、锁定依赖清单、命令日志、时钟／ADB 记录、提交同步点，以及各场景的状态库、报告和客户端凭据归档到 `tmp/osmo-installed-chain-archive-<pytest-pid>/`，避免后续 pytest 临时目录轮换移除联合链证据。

`test_osmo_builtin_timelapse_roundtrip.py` 从源码目录外构建并安装当前 wheel 与锁定运行依赖，比较源码及安装版 `describe`，并核对安装 wheel 内的 OSMO 配置、普通录像和延时样例与权威资料逐字节一致。该模块共四个用例：一个源码／安装版能力与资源核对，以及三个业务组合——普通录像配 LRF、原生延时配 LRF、原生延时不配 LRF。

业务链使用真实客户端计划导出、C host、安装版 CLI、SQLite、独立取回、报告导入及累计 ACK；只替换 PATH 中的 ADB 子进程边界和已声明的业务时钟入口。测试从整秒 UTC 与单调钟冻结值开始，让同一对读数供 ADB START 响应和保存 `sent_at` 使用；恢复阶段另设 epoch（时钟状态代次）并记录 CLI PID、实际读取的业务读数、真实单调时刻及已安装 `camctl.capture.handlers` 的调用位置。普通录像使用十秒目标与 0 毫秒启动余量，只发送一次 START；恢复进程在 `A+9/U+9` 决策点至少跨一个真实 0.5 秒轮询周期读取同一冻结值，期间不发送 STOP、不尝试结果核实、不生成结果页、文件或输出。测试随后在仍运行的同一进程中释放到 `A+10/U+10`，核对唯一正常 STOP；取得 `00` STOP 确认后，driver 仍使用真实单调钟实际等待完整 30 秒再读取文件。原生延时样例采用 2 秒间隔、300 秒设备目标和 0 毫秒启动余量，另保留 30 秒结果余量；测试先在 `A+329/U+329` 观察恢复决策持续读取且没有 STOP、结果列举、结果页、文件或输出，再于同一进程释放到 `A+330/U+330` 后核对结果核实。ADB 日志分别保存冻结业务 UTC／单调读数和真实进程单调时刻。对原片和可选预览分别取回后，测试通过 `client_driver.ts import-media <客户端存储目录> <已领取文件路径> <输出凭据路径>` 调用客户端真实 `Files.upload`；保存回执核对报告中的 delivery、output 与源动作身份，并将导入文件大小和 SHA-256 与对应交付事实比对。测试还验证取回中断及同一请求恢复时沿用原交付和已提交偏移。

该普通录像恢复场景只有一份非零尾段，跨会话没有可靠控制时长，也没有充分分段下界；来源按 `capture_result_unconfirmed/completion_unknown` 结束，已确定的原片范围继续允许独立取回。正常控制成功和充分分段下界的成功分区由八场景模块及组件测试分别验证。结果原页保留未配对的 LRF 观察，完成扫描后以实际配对事实关联原片。

ADB 提供的 OSV／LRF 是合成字节样例，不证明真实媒体可解码、设备画质、帧率或采集时长；受控时钟边界验证也不等于真实设备计时。本链的执行命令为：

```bash
apps/camctl/.venv311/bin/python -m pytest tests/integration/test_osmo_builtin_timelapse_roundtrip.py -q
```

`client_driver.ts validate-parameters <客户端存储目录> <参数用例路径> <凭据路径>` 是测试接缝。存储目录应含真实 `describe` 输出的 `device-capabilities.json`；用例 JSON 显式包含非空字符串 `action_type`、`device_id`、`action_name`，以及 `scheduled_at` 和 `cases`。每项用例有 `name`、保存完整原数字词元的 `params_text` 字符串和独立预期。浏览器共享校验与客户端后端共享校验都使用输入中的动作类型和设备标识，真实导出使用同一身份及动作名；身份字段缺失或为空时命令明确报出字段错误。凭据按同序记录 `browser_valid`、`shared_valid`、`export_valid`、两处未改写的参数正文，以及成功导出的完整 JSON 正文或明确的 `invalid_plan`。测试命令不接收产品候选能力开关，也不维护设备完整参数枚举。

### Hasselblad 网页与文件上传接缝

`test_hasselblad_photo_roundtrip.py` 的网页定向用例把实际安装 wheel 的能力交给当前构建首页，在真实控件选择三个短快门，保存预设后重建 Application、Files、HTTP 和浏览器，再重开原草稿并将预设应用到独立草稿，最终保留网页下载原请求。上传定向用例从权威正常报告派生小型图片映射，实际执行 `Files.createBatch/upload/idle`，分别观察报告先到和媒体先到的等待与核验，以及网页导出自动累计 ACK；这两组定向证据不代表已经执行相机拍摄或 C host 交付。

正常链矩阵把 RAW/JPEG、host/device 与三个精确短快门组合为十二个独立软件世界，每个世界只有一次 capture。各行由真实网页导出，经当前 C host-demo、仓库外 wheel 安装 CLI 和受 hb_cli 契约约束的进程替身，核对 SQLite、生效参数、固定 H 历史、报告和实际 argv。原图与预览各经两份新请求取回；host 工具离线后仍从正式源交付，device 等生产回收工作副本后按原 camera basename 和 original/thumbnail 下载。JPEG 同内容双角色也分别保留身份。报告与媒体随后实际上传，分别覆盖报告先到、媒体先到和缺预览三种顺序，再由网页自动导出累计 ACK，经 C host 核实原水位持久化。此链仍是软件证据，不连接物理相机。

正常用例还按实际下载 request_id 定位原 START、双文件完成和产物登记各自的完整事务 H，核版本化结果、同 activity 的照片与 entered 观察、实际步骤和诊断双流。安装入口的私有历史观察模式通过真实 HistoryRepository 恢复登记 owner 的完整关联行；晚期取回、原请求重送及 ACK 后，新安装子进程再读同三个 H 并逐项比较 before/after。晚于该 H 创建的实体保持不存在。窄反例验证中间事件不能冒充 H，缺失观察、错误 activity、角色和摘要会被拒绝。

五个恢复用例在同一安装链使用测试专用入口包装真实仓储。两个 START 已提交用例通过明确门闩终止真实旧执行组，由新 host 重送原请求；两个完整照片事务回执丢失用例保留真实 COMMIT，并由不同的只读连接真实核实原 request/key/H；一个 START 结果未保存用例仅观察实际 HOST_LOCAL_SETTLED 和固定恢复 horizon，核对 UNKNOWN、未知占用与工作文件保留。各世界只执行一次 capture，成功恢复后原图与预览各经两份独立新请求取回，设置和对焦不重放。门闩、原事务、进程组收场、实际字节与 argv 分别保存，测试不注入恢复依据。

报告重建用例在真实 publish intent 保存后，以原冻结 H 恢复成功照片及两个独立 output owner，再暂停发布。父进程领取既有 ready 文件、将空 ready 目录替换成普通文件，并明确释放门闩；真实目录列出失败由生产流程保存到原报告。移出失败 staging 文件后，新安装进程沿原 H 重建相同 report_id、文件名和 bytes/size/SHA。原报告真实保存 PUBLISHED 后的观察门闩供 C 领取，随后明确释放；两处门闩不改变发布返回、覆盖范围或水位。原冻结 H 与 INTENT/PUBLISH 管理事件的完整事务/key 分别保存。

运行前准备仓库外的准确客户端候选副本（包括根 protocol 和当前 `tests/integration/client_driver.ts`），按客户端锁文件安装依赖并实际执行普通 build，设置 `CAMCTL_HB_TEST_CLIENT_ROOT` 指向该副本根。Node 24 和 Chromium、Python 3.11、uv 离线缓存、CMake 和 C 编译器是前置。运行数据与 wheel 安装使用仓库外 pytest 临时目录，结束后再归档证据：

```bash
CAMCTL_HB_TEST_CLIENT_ROOT=/absolute/private/candidate \
  UV_PYTHON="$PWD/apps/camctl/.venv311/bin/python" \
  UV_PROJECT_ENVIRONMENT="$PWD/apps/camctl/.venv311" \
  apps/camctl/.venv311/bin/python -m pytest tests/integration/test_hasselblad_photo_roundtrip.py -q
```

测试驱动新增三个显式入口：`browser-photo-export <store> <input.json> <plan> <receipt>`、`browser-plan-export <store> <body.json> <plan> <receipt>` 和 `upload-files <store> <manifest.json> <receipt>`。照片输入含 `device_id/action_type/action_name/case_name/params_text/scheduled_at`，精确参数以正文字符串传递。普通计划入口也操作真实网页并下载文件。上传清单是非空 `[{path,kind:"report"|"media"}]`，图片使用 media；媒体先到不要求已有报告。凭据包含实际 import、媒体、报告覆盖和原始快门正文；原有 import 命令仍是直接报告应用接缝，与上传证据分别解释。

`test_video_size_estimate.py` 使用同源虚构驱动，经真实 `describe` 导出、客户端整份加载及 HTTP 状态交接验证视频估算，再通过客户端计划导出、camctl 受理和执行、报告发布及客户端导入核对实际文件事实。它覆盖精确小数、数学整数帧数、非法声明的整份失败和重载保留、合法但无法估算的导出，以及估算不进入生效参数或报告。`client_driver.ts` 的估算命令只提供测试凭据，没有新增产品接口。该文件无需 C 构建或真实设备；安装物资源由组件的包测试核对，真实相机的固定假定参考值另按[估算设计](../../docs/superpowers/specs/2026-10-10-video-size-estimate-design.md#实施状态与设备接入前提)解释。

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv" uv run --frozen --project apps/camctl --group test --python 3.11 pytest tests/integration/test_video_size_estimate.py -q
```

跨组件验收沿计划导出、模块交接路径、CLI 执行与报告发布、同步领取到 `processing`、客户端导入追踪业务结果，契约见[接入模块验收要求](../../docs/host-demo/design.md#验收要求)。报告同步主链已由真实客户端导出、真实 C 主程序递交与领取（WSL 构建的 host-demo）、camctl CLI 执行及客户端导入串联验证；录像、照片、延时摄影、取回、取消、清理等场景已全部经同一 C 模块递交链复验（`test_camctl_c_module_roundtrip.py`，带设备链经部署装配桥接入受契约约束的设备替身），会话恢复、媒体修复与日志副本等场景经 camctl CLI 直接驱动验证，命令与环境见[集成计划验证记录](../../docs/superpowers/plans/2026-09-30-camctl-integration.md)。剩余为真实第三方主程序接入及目标环境执行，边界见[部署交接与待核验项](../../docs/camctl/verification.md#部署交接与待核验项)与[模块验证记录](../../docs/host-demo/verification.md)。

`test_camctl_motor_notifications.py` 构建真实 C 主程序替身，通过单参数回调观察位置，串联客户端导出、CLI 发送、报告领取／导入和累计确认。它还验证慢回调期间的 submit、下一次 run、相机共存，以及保存意图后和写入后中断时 host 的自动恢复。

`test_camctl_motor_recovery.py` 在真实 SQLite 事务和管道写入边界安排进程中断或提交结果未知，分别核对实际消息、发送记录、动作终态和报告；覆盖意图及结果保存前后、通道缺失／已满／断开、过期、四种取消寻址和原请求重送。`_motor_fault_entry.py` 仅提供这些测试的确定故障点，生产 CLI 与仓储仍执行真实逻辑。命令、环境与分区结果见[电机控制验证记录](../../docs/camctl/verification.md#电机控制与单向通知验证2026-10-08)。测试不操作真实电机。
