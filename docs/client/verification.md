# 客户端验证范围

本页记录软件组件的实际验证结果，并区分部署、设备联调和物理断电验证。完整业务验收依据为[客户端六组场景](acceptance.md)，实现进度由[客户端规格同步计划](../superpowers/plans/2026-10-09-client-spec-sync.md)跟踪。

## 计划下载文件名与创建时间（2026-10-26）

Windows、Node.js 24.22.1 和系统 Chrome 中，`creation-time`、`protocol`、`reports`、`request-id`、`http`、`requests` 六个测试文件的 572 项通过。创建时间用例先观察到原实现拒绝三位毫秒、导出正文丢失毫秒，再验证合法日期、零与非零毫秒、旧秒级记录及非法精度。HTTP 用例在导出后推进到另一日期，确认正文时间与文件名固定，旧秒级记录的名称补 `000` 且正文不改写。

`requests` 用例确认首次导出 `created_at` 为 `2026-10-26 07:08:23.047`，关闭并重新打开真实数据库后原请求保持一致。浏览器导出与再次下载场景通过，核对实际建议名称、正文时间及复制后的新请求身份。类型检查与生产构建退出 0，构建保留既有主包超过 500 kB 的提示。camctl 的相关验证单独记录在[软件验证记录](../camctl/verification.md#计划创建时间的毫秒精度2026-10-26)。

日志位于 `tmp/created-at-ms-client-red.log`、`tmp/created-at-ms-client-green.log`、`tmp/created-at-ms-browser.log`、`tmp/created-at-ms-typecheck.log` 和 `tmp/created-at-ms-build.log`。测试使用独立临时数据，未运行完整客户端测试套件。

## 草稿输入响应与保存布局（2026-10-25）

Windows、Node.js 24.22.1 和系统 Chrome 中，类型检查和生产构建通过；构建保留既有主包超过 500 kB 的提示。`capability-observation`、`state-observations`、`preview-reload`、`web-session`、`draft-session-conflicts`、`validation-presentation`、`current-validation`、`preview-editor` 和 `video-estimate-state` 九个单元文件的 661 项通过。同版本能力复用用例先在原入口失败，覆盖版本切换、版本缺失、不可用变化与本次错误保留。

新增 `browser-editor-responsiveness` 的三项使用真实客户端、独立临时数据库和 HTTP 保存接口。1416 与 390 像素视口的用例分别先复现 20 和 46.59375 像素移动；修复后，已保存、等待保存、保存中及后续轮询时的输入字段文档位置相同，且核对完整输入确实写入。另一项更改能力文件并成功重载，确认新版本仍重新校验当前输入。三个既有浏览器测试入口补齐可选的 `CAMCTL_TEST_BROWSER_CHANNEL`，与组件内其他入口一致；未设置时仍使用默认 Chromium。

既有 `browser-validation`、`browser-video-estimate`、`browser-draft-conflict-ui`、`browser-previews` 和 `browser-workflow` 五个文件的 179 项全部通过，命令使用 `--maxWorkers=1` 与 `CAMCTL_TEST_BROWSER_CHANNEL=chrome`。该范围与新增三项合计 182 项浏览器用例，验证现有的未完成输入、保存恢复、冲突选择、预览协调、重载暂停和业务操作。独立只读评审核对能力版本来源、会话内容替换、报告校验依据和保存恢复按钮，未发现需要修复的问题。

性能复测只复制当前服务草稿与能力到独立临时目录，未改写原资料。前后均使用 1416×1244 视口、同一包含录像和自动预览的草稿，等待页面就绪后记录 2.4 秒空闲，再依次输入计划名称和最大延迟。基线空闲时观察到三次 62、66、66 毫秒长任务；后续输入及等待阶段另有五次 58–67 毫秒长任务。修复后相同观察阶段均未出现超过 50 毫秒的长任务。计划名称字段在保存状态切换期间的文档纵坐标变化从 20 像素变为 0。输入事件至下一帧的采样前后仍约为 7–17 毫秒，不能把该指标解释为完整输入延迟或将单次结果外推到所有草稿与环境。

本地服务提供新版构建，Codex 内置浏览器已刷新 `http://localhost:4310/`，重新打开原“新计划”草稿，核对保存依据和原参数。日志位于 `tmp/editor-cache-red.log`、`tmp/editor-cache-green.log`、`tmp/editor-layout-red.log`、`tmp/editor-layout-green.log`、`tmp/editor-unit.log`、`tmp/editor-browser-regression.log`、`tmp/editor-typecheck.log` 和 `tmp/editor-build.log`。性能原始数据分别为 `tmp/editor-input-before.json` 与 `tmp/editor-input-profile.json`，页面截图为 `tmp/editor-input-final.png`。文档本地文件链接检查的 5591 个链接通过，Git 差异空白检查通过。本项没有向设备递交请求，未执行完整客户端测试套件。

## 产物技术详情字段展示（2026-10-25）

Windows、Node.js 24.22.1 和系统 Chrome 中，类型检查与生产构建退出 0，构建保留既有主包超过 500 kB 的提示。`output-facts` 与 `web-facts` 两个单元文件的 10 项通过，覆盖源文件 SHA-256、未检查与未知时长、零秒和精确数值、检查完成仍保留问题，以及清理与检查错误的原始诊断。首轮 7 项回归用例均在原嵌套呈现方式下失败，确认字段含义与表格层级问题。

`browser-media-download` 的 11 项通过，覆盖技术详情的明确字段与单层双列、390 像素窄屏下长校验值换行，以及既有的可读名称、完整字节、LRF/OSV 播放和范围下载。首轮浏览器测试在准备阶段生成空 MP4，11 项未执行；装配改为读取项目生成的固定 32618 字节 VP9 MP4 夹具后，保留原播放和下载断言并全部通过。测试使用独立临时目录。

本地服务继续使用原数据目录，在 Codex 内置浏览器的 `http://localhost:4310/` 展开真实 OSV 产物确认“源文件 SHA-256／尚未取得”“主机媒体检查／未执行”和“媒体时长／未知”均直接显示，没有中间状态列。日志位于 `tmp/output-facts-red.log`、`tmp/output-facts-unit.log`、`tmp/output-facts-typecheck.log`、`tmp/output-facts-build.log` 和 `tmp/output-facts-browser-final.log`，页面截图为 `tmp/output-facts-final.png`。本项没有递交设备请求，未运行完整客户端测试套件。

## 终态取消入口与计划标识（2026-10-25）

Windows、Node.js 24.22.1 和系统 Chrome 中，类型检查与生产构建退出 0；`browser-workflow` 和 `browser-previews` 两个文件、45 项通过。新增用例覆盖三种计划状态、运行中计划内动作状态的独立判断，以及四种动作终态；终态取消入口隐藏后，取回入口仍可见。既有精确取消操作改用含待执行动作的运行中计划，仍核对导出目标 ID、原报告不变及草稿写入。

首轮用例证实已结束计划与成功动作仍错误显示取消按钮。测试准备时，执行中录像携带正式产物的夹具被公共协议拒绝，已改为保留完成的录像并加入待执行同步动作。动态画布录制曾只产生 110 字节的不可解码文件；媒体装配改用仓库内固定、可播放的 WebM 夹具，保留真实导入、核验、播放、进度和范围下载断言。该装配函数的全部浏览器调用方均在上述两文件中完成验证。

使用原数据目录的本地服务已提供新版网页。Codex 内置浏览器在 `http://localhost:4310/` 确认真实已结束计划和成功动作均无取消快捷入口，主机报告卡片明确显示“计划实例 ID”。原请求与报告状态仍分别展示。证据位于 `tmp/terminal-cancel-red.log`、`tmp/terminal-cancel-build.log`、`tmp/terminal-cancel-final-browser.log` 和 `tmp/terminal-cancel-final.png`；文档本地文件链接与 Git 差异格式检查通过。本项没有向设备递交请求，也未运行完整客户端测试套件。

## 产物卡片与可读下载名称（2026-10-25）

Windows 环境使用 Node.js 24.22.1 和系统 Chrome，在 `implement_camctl` 验证单产物操作行、折叠技术详情和交付显示名称下载。浏览器测试使用独立临时目录，不向当前服务的数据目录导入夹具。

| 验证范围 | 实际结果 |
| --- | --- |
| `result-model`、`media` 单元及 `media` 集成 | 三个文件、44 项通过；包含其他交付副本的名称关联、媒体类型和真实 HTTP 文件访问。 |
| `browser-media-download`、`browser-workflow`、`browser-recovery` | 最终三个文件、33 项通过；核对卡片及大图的中文下载名称、HTTP 附件名称和完整字节，保留 LRF/OSV 播放与范围读取、核验失败恢复和精确产物操作。 |
| `browser-media` | 八项通过；包含跨交付副本查看、混合来源设备的清理资格及大量媒体的分页浏览。与上述最终复测合计四个浏览器文件、41 项。 |
| 类型检查、生产构建及文档文件链接 | 均退出 0；生产构建保留主包超过 500 kB 的体积提示。 |

产物可用时省略状态徽标并展示适用的清理入口；已清理或未知时保留状态和本地下载，隐藏清理按钮。浏览器断言同时核对取回与下载互斥、默认可见的清理操作、单一技术详情入口和详细事实保留。首轮未知状态夹具缺少协议要求的错误字段，补齐后导入并通过；混合导入的视频夹具仅生成文件头，画布加入文档后取得真实视频帧并通过播放断言。MP4 夹具明确请求画布帧，仍核对真实播放和下载字节。

本地服务已在 `http://localhost:4310/` 使用现有数据目录重新部署，并在 Codex 内置浏览器核对实际 OSV 取回、LRF 下载和清理按钮，以及收起后的卡片布局。命令输出保存于仓库临时目录 `tmp/product-card-*.log`，页面截图为 `tmp/product-card-final.png`。本项未运行完整客户端测试套件，也不包含新的主机报告或设备操作验收。

## 结果操作与主机状态时间（2026-10-25）

Windows 环境使用 Node.js 24.22.1、系统 Chrome、Python 3.11.12 和 SQLite 3.54.1。客户端修改在 `implement_camctl` 集成；独立主机工作树完成后按提交合并。媒体测试明确选择 VP9 编码的 MP4，并主动请求画布帧；网页混合导入等待文件实际核验完成后再断言播放，报告覆盖水位不充当媒体核验完成条件。

| 验证范围 | 实际结果 |
| --- | --- |
| 客户端完整单元 `vitest run tests/unit --maxWorkers=2` | 68 个文件、3260 项通过；随后新增的两个默认取回单元与报告单元一起运行，449 项通过。 |
| 客户端非浏览器集成 `vitest run tests/integration --exclude 'tests/integration/browser-*.test.ts' --maxWorkers=1` | 22 个文件、1066 项通过，包含文件即时可读取性、删除后状态更新，以及中间报告缺字段和重启后拒绝改写状态时间。 |
| `browser-media-download`、`browser-previews` | 两个文件、31 项通过，包含真实下载字节、LRF/OSV 播放和范围读取、所选过滤、全收到零写入及桌面和窄屏时间展示。 |
| `browser-media`、`browser-navigation`、`browser-workflow` | 最终三个文件、31 项通过，包含其他交付副本打开大图、跨来源已移走文件仍被取回、默认操作与弹窗焦点恢复。 |
| `browser-recovery`、`browser-segment-results` | 两个文件、9 项通过。 |
| `browser-media` 新增的计划与动作默认取回导出 | 两项定向运行通过，核对来源范围与本地排除列表。七个相关浏览器文件累计 73 项；其他浏览器文件未在此范围重跑。 |
| 类型检查、生产构建与公共登记检查 | 全部退出 0；构建保留主包超过 500 kB 的体积提示。协议 Schema、报告依赖、事件登记和文档链接通过。 |
| 集成后的主机时间、报告格式升级与默认排除专项 | 分目录顺序运行，分别为 6、6、23 项通过。 |
| 主机默认取回相关验证 | `outputs` 单元 862 项、相关集成 276 项，以及测试连接代理事务 33 项通过。 |
| 主机时间相关验证 | 单元 2079 项通过、4 项已在基线重现的 Windows 路径用例排除；报告目录 449 项通过、2 项在基线重现的状态准备断言失败。 |

两项主机报告基线失败位于 `test_generation.py`：`test_generation_writes_schema_valid_staging_file` 预期计划已完成但得到执行中，`test_failed_source_error_rebuilds_at_the_frozen_history_boundary` 预期来源失败但得到执行中。四项单元排除来自 `test_capture_preparation_init.py` 的既有 Windows 路径绑定夹具。对照使用独立 `ab088fa2` 源码快照，没有修改这些断言或生产规则。

Python 在真实临时 SQLite 中生成格式 2 的连续两份报告，客户端通过正式导入入口依次接受，覆盖推进至 8，计划和动作完整的三步状态历史均保留，墙钟回退没有改变数组顺序。浏览器导出的计划与动作默认取回 JSON 均通过主机正式 Schema 校验、参数解析和产物选择函数，排除已收到的产物后只选择未收到的默认产物。独立审查核对了默认选择与修复替代、全部排除、历史知识、格式传递、旧字节重建、显式升级和零时间占位识别。服务使用原客户端数据目录部署到 `http://localhost:4310/`，在 Codex 内置浏览器核对按钮、取回/下载互斥及全收到提示。原计划报告缺少时间数组，页面明确显示“报告未提供”；没有升级用户的主机状态库或向设备递交请求。

命令输出及基线证据保存于仓库临时目录 `tmp/result-operations-*.log` 和 `tmp/result-operations-evidence/`。专项输出为 `tmp/report-status-integrated.log`、`tmp/report-upgrade-integrated.log`、`tmp/default-exclusions-integrated.log`。这些结果不包括目标设备性能、实际设备控制、物理断电或新的安装包验收。

## 开发容器重建后的客户端验证（2026-10-24 UTC）

使用 Node.js 24.22.1、npm 11.21.1 和原 `package-lock.json` 安装开发依赖。类型检查与生产构建退出 0；完整单元测试 67 个文件、3229 项通过，51 项报告导入集成和 19 项预览浏览器集成分别通过，均退出 0。测试使用 `--maxWorkers=2`，按单元、报告导入、浏览器的顺序运行。记录器总耗时分别为 61.447、5.510、58.663 秒；它包含测试进程启动和收尾，不等同于用例耗时。

浏览器为配套 Google Chrome for Testing 153.0.8010.12。首次版本探针缺少 `libnspr4.so`，退出 1；安装 Playwright 系统依赖后版本探针和浏览器测试均通过。原失败与成功记录分别保存在 `.local/verification/client-refresh-20261025/`，类型检查与构建记录位于 `.local/verification/environment-recovery-20261025/client-build`。这些目录保留命令、工作目录、实际 UTC、退出码及完整输出。

统一交付的 ZIP 包含 53 个文件，不含依赖安装目录或业务数据库；原锁文件在解压包内安装 92 个生产依赖，退出 0。独立数据目录通过真实包 `start.sh` 完成 19 项 HTTP 检查，包括首页及构建资源、显式初始化、两台相机能力、录像计划编辑和本地导出、同一份 1005 字节来源失败报告的上传、导入及原始字节读取。报告被接受，覆盖水位为 16；来源说明函数单独从与构建提交字节一致的仓库文件运行并显示原错误，该函数检查与包内编译网页的浏览器呈现分别记录。另在隔离目录通过实际安装包导出两台相机的录像与延时摄影四个动作，计划仅保存和下载，没有递交设备。服务正常停机且端口关闭。脚本执行期间遇到的记录器及断言汇总错误均保留原始日志，最终成功结果位于 `.local/verification/installed-client-smoke-20261025/`；用户的新数据目录单独初始化，具体入口及验证范围见[统一交付记录](../camctl/verification.md#独立安装与手工验证入口)。

## 来源动作失败且没有产物的客户端处理（2026-10-24 UTC）

客户端识别 `obtain_action_outputs.result.failures` 中的 `source_action_failed`，核对错误阶段、详情来源 ID 与失败项来源 ID，并拒绝把该错误用于显式产物 ID 取回。同一报告或相同 `to_wm` 边界的合并索引内出现来源动作时，客户端要求来源已失败且该边界没有已知正式产物实体。`outputs` 省略时允许导入，但省略不能删除历史已知产物；较早或较晚边界的历史实体不参与新边界的来源判断。客户端不靠字段省略推断完整产物目录为空，依赖来源错误本身表达该事实。报告摘要显示来源失败、没有可取回产物或预览、来源名称和 ID，以及已提供的来源错误码；展开技术详情仍可查看原始取回失败项和来源动作错误。该码没有进入分段错误处理，因此不会显示缺口或默认范围未知提示。

先添加省略 `outputs` 的生产报告形态反例，原校验在真实 Python 报告导入、同报告导入和同边界合并三处均错误地拒绝。夹具来自生产 `generate_report_file`：[Python 报告 JSON](../../apps/client/tests/fixtures/source-action-failed/source-action-failed-report.json) 中来源动作状态为 `failed`，`outputs` 字段省略，取回失败项使用新来源错误；报告 SHA-256 为 `5f5feafddbbd6a5547429b4e056aa73c839a788b9061fd92331d5a8d370df579`。另有非空来源产物、非失败来源、显式 ID 与跨边界历史字段用例。客户端原页面基线构建后，浏览器用例未显示来源失败说明。修改后记录如下：

| 检查与实际命令（均在 `apps/client` 执行） | 实际结果 |
| --- | --- |
| `node node_modules/typescript/bin/tsc --noEmit` | 退出 0。 |
| `node node_modules/vite/bin/vite.js build` | 退出 0，转换 469 个模块；主包 891.79 kB、gzip 后 265.07 kB，保留超过 500 kB 的既有体积提示。 |
| `node node_modules/vitest/vitest.mjs run tests/unit --maxWorkers=2` | 67 个文件、3229 项通过，退出 0。 |
| `node node_modules/vitest/vitest.mjs run tests/integration/reports.test.ts tests/integration/segment-reports.test.ts tests/integration/scalar-schema-import.test.ts --maxWorkers=1` | 3 个文件、51 项通过，退出 0。 |
| `node node_modules/vitest/vitest.mjs run tests/integration/browser-previews.test.ts --maxWorkers=2` | 1 个文件、19 项通过，退出 0；包含来源错误摘要、自动预览来源定位及原始详情展示。 |

为取得上述 Python 报告样本，只运行了一个使用临时状态库的后端报告生成集成用例；该用例通过不代表完整后端门禁。客户端验证未运行完整客户端集成套件、安装版联合验证或真实设备操作；后端新错误处理仍需独立审查和完整验证。客户端结果只证明当前代码的导入、校验和页面展示，不证明真实主机或相机行为。

## Hasselblad 命名选项与安装版参数验收（2026-10-21 UTC）

在 `39b01db` 候选上，以 Node.js 24.20.0、pnpm 12.3.4 和 Chromium 验证通用中文常量选项、曝光／白平衡／对焦分支及精确短快门。仓库外客户端从原 `package-lock.json` 严格安装 157 个依赖，279 个受控源文件与仓库逐一匹配；直接依赖版本与原锁相同。`pnpm import` 仅为本地运行生成配套环境，转换文件不作为新的依赖权威来源。

独立类型检查和完整构建均退出 0；`VITEST_MAX_WORKERS=2 pnpm --dir /tmp/camctl-hb-20261022-locked/apps/client test` 顺序完成 65 个单元文件的 3197 项及 40 个集成文件的 1566 项，退出 0。构建转换 469 个模块，主包 886.44 kB，保留超过 500 kB 的体积提示。实际浏览器覆盖三个短快门的选值、预设重开和导出；安装版根组合进一步核对真实 `Files.upload`、双角色媒体和累计 ACK，完整软件链及未通过的驱动门禁见[联合续验记录](../camctl/verification.md#hasselblad-完整参数的安装版联合续验2026-10-21-utc)。

首份副本被 pnpm 自动安装后，三个直接依赖版本偏离原锁。该次单元阶段通过，集成阶段在确认版本不符后中断，整个命令退出 130；原日志保留，不计作完整门禁通过。锁定副本的最终命令、候选比对、UTC 和日志保存在 `.superpowers/sdd/2026-10-21-hasselblad-photo-parameters/resume-20261022/locked-client-*`。本项没有部署 ARM 或操作真实相机。

## 当前部署包与两设备能力交付（2026-10-19 UTC）

从干净的 `937b134` 构建客户端 ZIP，包含当前网页、后端、公共 Schema、锁文件及 Windows／POSIX 启动脚本；版本与 Node.js 范围从当前 package.json 取得，包内没有 node_modules 或用户数据。实际解压包在仓库外以 Node.js 24.20.0 严格安装 92 个生产依赖，真实 HTTP 与 Chromium 完成显式初始化、两设备指南、OSMO 两任务的曝光说明与码率选项查看、哈苏单张及 OSMO 录像／延时混合计划导出、停止及重启后的原请求保持，全部通过。

能力文件来自[本次 TX2 安装版导出](../camctl/verification.md#当前驱动的-tx2-部署与能力同步2026-10-19-utc)，客户端 HTTP 内容与其一致。ZIP 内部文件清单、归档完整性、当前 Node.js 范围、发行物提交及两个下载文件的 SHA-256 均已核对。交付文件与命令证据保存在 `.local/deployments/20261019002145Z-937b134/`，最终结果为 `evidence/client-smoke-result.json`；Windows 和 macOS 本次未实际运行。

初次部署探针将选中标记计入选项文本，随后一次未等待弹出列表挂载；改为只读取选项标签并明确等待列表可见及选项数量后，使用新的隔离目录完成最终验证。原始失败日志保留，ZIP 与生产代码未因此改变。开发环境未提供裸 npm 命令，实际安装使用已存在的 npm CLI 配合 Node.js 执行；包内说明仍使用目标环境 Node.js 自带的 npm。

## 能力字段说明与元数据用例修复（2026-10-19 UTC）

在 Linux x86_64、Node.js 24.20.0、pnpm 12.3.4 和 Chromium 中，按[复核记录的两项修复依据](../superpowers/notes/2026-10-19-client-capability-parsing-review.md)修复通用对象控件与能力源测试。对象说明、当前判别字段的标题和说明、当前分支标题和说明均从对应 Schema 读取；未选择模式时只使用公共信息。能力源元数据用例逐任务核对实际存在的字段，覆盖固定任务、仅模式对象及带成员对象，样例仍经真实后端完整校验。

新增浏览器回归分别使用仅模式和带成员的任意命名对象，覆盖说明中的特殊字符、分支切换、刷新及能力元数据重载。两项最初均因对象说明不可见而失败；实现后通过。当前 OSMO／Hasselblad 两设备文件注入既有元数据目标，先复现 undefined 解构错误，修复后与级联说明及设备指南目标合计 5 项通过、98 项未选中，退出 0。

| 最终检查 | 实际结果 |
| --- | --- |
| `VITEST_MAX_WORKERS=2 pnpm --dir apps/client test` | 59 个单元文件、2890 项通过；随后类型检查、当前网页构建及 35 个集成文件、1369 项通过，总计 4259 项，退出 0。完整集成用时 548.11 秒；保留既有依赖模块指令提示。 |
| `apps/camctl/.venv311/bin/python -m pytest tests/integration/test_device_capability_declarations.py::test_installed_metadata_reaches_parameter_form_and_device_guide -q --tb=short` | 1 passed，10.35 秒，退出 0。离线安装当前 wheel，实际导出 Action6、OSMO 及哈苏两种正式源的目录，经当前表单和设备指南消费；元数据目标遍历全部任务。 |
| 真实两设备页面复测 | 三个任务的保存与导出通过；OSMO 两个任务的对象及当前模式说明均实际可见，时间双向过滤及刷新保持，退出 0。页面截图保存为 `frontend-fixed-*.png`，实际查看录像页确认说明直接显示。 |
| 格式及独立复核 | 两个 TypeScript 文件的 Prettier 检查通过。独立评审者只读核对五文件差异、红绿证据与当前元数据来源，未发现遗留缺陷；实际目录、index 和 HEAD 未被评审者修改。 |

命令、UTC、基线、差异摘要、日志和退出码保存在 `.local/verification/osmo-settings-20261019/frontend-fix-*`。元数据目标首轮修复验证因 Playwright 嵌套定位器的相对作用域找不到说明容器而失败；改用相对容器可解释的字段定位后通过，没有放宽元数据断言。本项未修改驱动或公共协议，也未部署 TX2 或操作真实相机。

## 设备能力解析与选项联动复核（2026-10-18 UTC）

在 Linux x86_64、Node.js 24.20.0、pnpm 12.3.4 和 Chromium 中，以 `b8bb4f3` 当前网页及本地 OSMO／Hasselblad 两设备能力文件复核，哈苏采用主机正式源。生产路径、行为结论和两个待修复问题见[复核记录](../superpowers/notes/2026-10-19-client-capability-parsing-review.md)。

| 检查 | 实际结果 |
| --- | --- |
| 当前类型检查及网页构建 | `pnpm --dir apps/client run build`，退出 0；保留依赖中既有的 `use client` 指令提示。 |
| 参数选项、对象控件、参数类型草稿和设备选择单元用例 | 4 文件、297 passed，退出 0。 |
| 联动和相机参数浏览器用例 | 2 文件、52 passed，退出 0。 |
| 完整有限组合相关浏览器目标 | 5 passed、64 未选中，退出 0；覆盖设备联动、本地引用、必填与原值、非法组合修正及重载。 |
| 真实两设备 JSON 计算探针 | 544 个时间候选组合中 504 个合法；另有普通录像的 4 个曝光／码率组合通过。全部候选的合法性、双向选项及输入保持符合规则。 |
| 真实两设备页面探针 | 哈苏单张、OSMO 录像和延时三项保存及导出通过；OSMO 刷新、时间双向过滤、曝光和码率保持通过，退出 0。 |
| 真实两设备文件注入现有“能力源”目标 | 1 passed、1 failed、30 未选中，退出 1；元数据用例在页面创建前因额外子成员假设抛出 TypeError，仅模式曝光／码率用例通过。 |

复核时页面探针另确认，级联控件没有显示对象及当前模式说明，原始说明仍在折叠的完整规则中。后续修复与验收见[字段说明与元数据用例修复](#能力字段说明与元数据用例修复2026-10-19-utc)。探针首轮因 pnpm 改变进程目录而找不到输入文件，固定工作目录后通过；首轮未打开页面。

命令、工作目录、起止 UTC、基线、差异摘要、日志和退出码保存在 `.local/verification/osmo-settings-20261019/frontend-audit-*`；一次性探针和三张实际页面截图位于同目录。本次未修改客户端行为，未部署 TX2 或操作真实相机。

## Node.js 连续版本范围的安装验证（2026-10-18 UTC）

客户端部署运行的版本范围由 [package.json](../../apps/client/package.json) 定义。使用 npm 随附的 semver 校验器核对 24.16.0、25.0.0、25.9.0、26.0.0、26.11.0 和 26.12.0 均被接受；24.15.0、26.12.1、27.0.0 和预发布版本被拒绝，两个端点之间为连续范围。

Linux x86_64 上的 Node.js 24.20.0、25.9.0 和 26.12.0 分别使用当前锁文件执行 `npm ci --omit=dev --engine-strict --no-audit --no-fund`，各安装 92 个生产依赖，均退出 0。锁文件内容比较确认仅根包的运行时声明变化。25.9.0 的下载归档与官方同目录 `SHASUMS256.txt` 一致，实际 SQLite 为 3.51.3。

包含开发依赖的 Node.js 25.9.0 严格安装返回 `EBADENGINE`，错误来自 Vitest 5.0.1 自身的 Node.js 要求。生产部署与开发测试分别遵守各自依赖要求，步骤见[客户端运行说明](running.md)。命令、UTC、成功与失败日志及版本边界记录保存在 `.local/deployments/20261019-node24-26-range/`；本节不表示区间内每个补丁版本均单独实测。

## Node.js 26 的兼容性与安装验证（2026-10-18 UTC）

客户端支持的运行时范围由 [package.json](../../apps/client/package.json) 的 `engines.node` 定义，运行步骤见[客户端运行说明](running.md)。支持范围调整涉及包元信息和运行说明，应用源码及依赖版本保持一致；锁文件由 npm 生成，完整内容比较确认仅根包的 Node.js 版本声明变化。

在 Linux x86_64 上使用 Node.js 26.12.0、npm 11.20.0 和 SQLite 3.54.0，在隔离源码副本中执行以下检查。Node.js 下载归档与官方同目录的 `SHASUMS256.txt` 一致。

| 检查 | 实际结果 |
| --- | --- |
| 按锁文件安装依赖、类型检查和网页构建 | 全部退出 0；构建 464 个模块，保留既有主包超过 500 kB 的提示。 |
| 完整单元测试 | 59 个文件、2890 项通过，21.01 秒，退出 0。 |
| 指定集成测试 | 8 个文件、356 项通过，28.14 秒，退出 0；覆盖数据库、草稿及固定请求、真实 main 启动与重启、状态完整性、参数保存与恢复、报告导入、文件导入和电机精确数字。 |
| 部署包运行 | 实际 ZIP 解压后安装生产依赖，Chromium 完成显式初始化和两设备说明展示；OSMO 360 与 Hasselblad 混合计划导出、正常停止和重启后的原请求保持均通过。 |
| 支持范围的严格安装 | Node.js 24.20.0 和 26.12.0 分别使用同一锁文件执行 `npm ci --omit=dev --engine-strict --no-audit --no-fund`，各安装 92 个包，均退出 0。 |
| 当前工作区类型与构建 | 使用 Node.js 26.12.0 分别执行 TypeScript 与 Vite 入口，均退出 0。 |

兼容性检查的命令、UTC 时间、退出码和日志保存在 `.local/verification/node26-compat-17eff82/`，支持范围调整后的严格安装与构建记录保存在 `.local/deployments/20261019-node26-support/evidence/`。本节属于 Linux 快速兼容性验证；Node.js 26 的完整集成套件、Windows 和 macOS 实际运行，以及 Docker 容器运行分别保留独立验收边界。

## 设备能力声明的安装版消费验证（2026-10-18）

在 Linux x86_64／WSL2、Node.js 24.20.0、pnpm 12.3.4 和 Python 3.11.16 中，执行[安装版联合用例](../../tests/integration/test_device_capability_declarations.py)，6 项通过，退出 0。用例在仓库外离线安装当前 wheel，使用真实配置导出全部内置拍摄任务，并将原始 JSON 交给客户端；组件测试的默认夹具仍独立于 Python。

客户端共享加载器、实际 `Application` 和 HTTP 状态完整保留设备删除布尔值、任务说明及 Schema 精确数字。`null`、数字、字符串、对象和数组这五类非法删除声明使整份重载失败，已有有效目录保持；同一非法文件在新启动时没有有效目录。旧 JSON 缺少删除字段时仍保持缺失，有效空设备目录能整体替换。联合用例先构建当前网页，再通过测试专用 `CAMCTL_TEST_CAPABILITIES_FILE` 将安装版导出注入 `browser-camera-parameters.test.ts` 与 `browser-editing.test.ts` 的“能力源”目标，2 项浏览器用例通过；参数表单读取同源字段说明，设备指南逐项显示全部任务说明，查看页面不改草稿。

独立页面联合用例使用安装版导出的全部内置设备和任意驱动，检查指南声明、动作级及单产物级清理入口。Hasselblad 当前配置从主机源切换为设备源再切回后，入口随对应声明更新；其他同驱动设备保持自身值。非法字段及重复键重载失败后仍按旧 false 目录隐藏入口并显示错误；首次加载失败与有效空目录均按未知保留入口。混合取回依据各产物的实际拍摄来源判断，false 的原片／预览隐藏快捷清理，修复成品继续保留；产物状态、草稿及真实导出的原请求保持不变。

组合后以 `VITEST_MAX_WORKERS=2` 串行执行 `pnpm --dir apps/client run test:unit` 和 `pnpm --dir apps/client run test:integration`：59 个单元文件的 2890 项通过（25.10 秒），35 个集成文件的 1366 项通过（492.43 秒），均退出 0。集成脚本前置类型检查和网页构建通过；主包 854.75 kB，保留超过 500 kB 的构建提示。根目录两个 TypeScript 测试入口另外通过严格类型和格式检查。

命令、UTC、日志和退出码保存在任务工作树 `.local/acceptance/c5/`；Python 发行物与组件门禁见[camctl 安装链记录](../camctl/verification.md#设备能力声明的安装版联合验证2026-10-18)。软件结果不证明 ARM 部署或真实设备操作。

## 2026-10-16 延时参数保存与恢复验证

客户端领域的 C3 保存矩阵使用本工作树的真实 HTTP、SQLite 和 Chromium；任务入口见[延时客户端计划](../superpowers/plans/2026-10-16-osmo360ii-timelapse-client.md)。生产实现满足新增断言，本阶段只扩展测试。合成能力仅提供两个间隔、两个时长以及两类输出结构，分别验证保存场景；生产两款 Schema、各自 134 对合法时间及跨语言判定由 P3 的根接缝单独验证。

[参数保存测试](../../apps/client/tests/integration/parameter-variants.test.ts)覆盖最短数值 `1.0/3e2`、Auto/manual ISO 800、Action6 三输出与 OSMO 仅视频，经 HTTP 保存、预设和导出后关闭服务及数据库，再以同一隔离目录重建后端。数值与原数字词元保持，能力收窄或移除不改写草稿、预设及固定请求；非法草稿可保存和重开，非法预设更新保留原记录，导出仍由后端完整校验。组内间隔、组外曝光成员与公共策略的未完成输入分别保留并阻止导出；停用曝光模式及成员原文随重启恢复。修正时间后，未完成的曝光仍不能保存为预设或导出。

[联动页面测试](../../apps/client/tests/integration/browser-linked-parameters.test.ts)覆盖能力更新后的完整草稿和版本保持、独立曝光恢复及预设全文替换。应用预设清除所属参数输入，保留公共字段及其未完成原文；之后修改草稿不改变原预设。旧 Action6 `5400/6000` 秒草稿与预设在新规则下保留原值并显示错误，固定请求下载正文保持。独立合成录像与单张规则继续由各自 Schema 决定，没有由时间联动增加统一限制。

验证环境为 Linux 开发容器，Node.js `v24.20.0`、SQLite `3.53.4`、TypeScript `7.0.2`、Vitest `5.0.1`、Vite `8.3.0`、Playwright `1.63.0`、Chromium `153.0.8010.12`。依赖使用本工作树已有独立安装；命令从 `apps/client` 执行，直接使用 Node 入口，对应类型检查及完整测试脚本的顺序。测试使用两个工作进程，先单元，再构建当前网页，最后完整集成。

| 检查与实际命令 | UTC 时间、退出码及结果 |
| --- | --- |
| 专项 `node node_modules/vitest/vitest.mjs run tests/integration/parameter-variants.test.ts tests/integration/browser-linked-parameters.test.ts --maxWorkers=2` | 08:17:15→08:18:02，exit0，2 文件、169 项，46.72 秒。 |
| 类型 `node node_modules/typescript/bin/tsc --noEmit` | 08:18:02，exit0。 |
| 完整单元 `node node_modules/vitest/vitest.mjs run tests/unit --maxWorkers=2` | 08:18:02→08:18:27，exit0，59 文件、2883 项，25.07 秒。 |
| 构建 `node node_modules/vite/bin/vite.js build` | 08:18:27→08:18:28，exit0，464 modules；主包 854.24 kB、gzip 255.80 kB，保留超过 500 kB 提示。构建前已通过上述类型检查。 |
| 完整集成 `node node_modules/vitest/vitest.mjs run tests/integration --maxWorkers=2` | 08:18:28→08:26:21，exit0，35 文件、1350 项，472.90 秒。 |

最终完整测试共 4233 项，不叠加专项及开发过程的数量。

独立审查通过后，在包含 P3 和 C3 的组合工作区使用 Python 3.11.16、SQLite 3.53.1 和 Node.js v24.20.0，运行 `UV_PROJECT_ENVIRONMENT="$PWD/apps/camctl/.venv311" uv run --frozen --project apps/camctl --group test --python 3.11 pytest tests/integration/test_timelapse_time_parameters.py -q`，3 项通过，18.95 秒，退出码 0。两款生产候选 Schema 经测试合同的实际 `describe` 导出，再由 Python、真实 Chromium、Node 后端和客户端导出共同消费；各 134 对合法时间、精确数值及型号曝光和输出边界通过。C3 的跨语言共同项据此闭合；该结果不表示正式内置延时能力或设备完成契约已验收。组合日志为主工作区忽略目录中的 `integrated-c3-p3.log` 和 `.exit`。

首轮测试的六项失败来自新增夹具的空 `allOf` 与浏览器请求地址，修正后原目标 158 项首次通过；随后补全的最终目标为 169 项。夹具失败不作为产品缺陷或产品测试红灯。实际命令、UTC 时间和退出码保存在本工作树忽略目录 `.superpowers/sdd/2026-10-16-osmo360ii-timelapse/task-C3-final-*.log`。上述后端重建和浏览器重开是客户端软件证据，正式安装版 `describe`、ARM、真实相机与物理断电分别验收。

## 2026-10-12 并发草稿与完整版本选择验证

验证环境为 Windows、PowerShell、Node.js `v24.16.0`、TypeScript `7.0.2`、Vitest `5.0.1`、Vite `8.3.0`、Playwright `1.63.0` 和 Chromium `153.0.8010.12`。命令直接使用已有 Node 入口，没有安装依赖。有效规则分别见[自动保存与并发冲突](storage-and-recovery.md#草稿的自动保存与并发冲突)、[另存的确认与重试](storage-and-recovery.md#另存的确认与重试)、[采用与替换的异常处理](storage-and-recovery.md#采用与替换的异常处理)、[键盘与焦点](page-interactions.md#键盘与焦点)和[动作显示状态与追加接纳](../architecture/client-editing.md#动作显示状态与追加接纳)。实施范围由[并发草稿计划](../superpowers/plans/2026-10-11-client-draft-conflicts.md#任务七真实双浏览器组合完整门禁与交付)跟踪。七项实施均已通过逐任务独立审查，整体审查及统一修复后的限定复审结论在下述当前端点登记。

新增双浏览器测试为每项用例创建隔离临时目录、一个 `Application`、真实 `Files`、SQLite Store、`RequestLifecycle` 和随机 `127.0.0.1` HTTP 端口，加载当前 `dist`，A、B 使用两个独立 `browser.newContext()`。程序化装配的远端版本通过 A 上下文的真实 HTTP 请求保存；该装配不能代表 A 在表单中手工输入。版本拒绝由真实事务产生；保存和创建响应丢失先用 `route.fetch()` 完成后端操作再中断响应。新夹具清理先释放闸门，随后关闭浏览器上下文、请求、Files、服务与 Store，最后核验绝对临时路径再删除目录。既有保存与导出、录像估算页面测试分别使用其真实后端和单页面夹具；它们不作为双浏览器证据。

组件测试使用 Vite 临时独立缓存，注入源显示、目标会话、Map、导航准备和提交前校验故障；它们证明受控组件与真实领域函数共同工作的分区，不能作为真实 Application 的故障证据。可靠 `not_created` 的首个 HTTP 响应也是受控替身，后续同身份创建与终态查询经过真实后端。测试保留页面错误、失败请求和 HTTP 错误诊断。早期 Task6 夹具首次加载缺少 HTTP 诊断，无法追溯其确定根因；后来独立缓存的验证不改变这一证据限制。

任务七在整体审查前取得的门禁端点如下；整体审查修复后的源码端点在本节后续单独登记。开发阶段、失败门禁和窄验证不与完整测试数量累加。任务七日志位于 `.superpowers/sdd/2026-10-11-client-draft-conflicts/task-7-*.log`，日志记录命令、实际日期时间和退出码；空的类型检查 stdout 不单独作为通过依据。

| 命令与范围 | 2026-10-12 北京时间、实际退出码及结果 |
| --- | --- |
| 客户端 `node node_modules/typescript/bin/tsc --noEmit` | 01:46:30.568→01:46:31.163，exit0，stdout 空；日志为 `task-7-final-types-closed.log`。 |
| 客户端 `node node_modules/vite/bin/vite.js build` | 01:06:54.091→01:06:54.530，exit0，463 modules，主包848.13 kB、gzip254.11 kB；保留超过500 kB notice。日志为 `task-7-save-entry-build.log`，对应整体审查前的源码。 |
| 客户端 `node node_modules/vitest/vitest.mjs run tests/unit --maxWorkers=2` | 01:09:21.517→01:09:41.866，exit0，54 files、2667 tests、20.03 s；日志为 `task-7-current-unit.log`。 |
| 客户端 `node node_modules/vitest/vitest.mjs run tests/integration --maxWorkers=2` | 01:38:56.412→01:45:59.953，exit0，34 files、1282 tests、423.20 s；日志为 `task-7-final-integration-closed.log`。 |
| 暂停计时 Select 的两个调用补证 | 完整集成后只补回原帮助函数的 option label 断言；01:46:31.804→01:46:35.817，exit0，2 passed/36 skipped、3.66 s，日志为 `task-7-final-select-assertions.log`。完整端点对应补断言前测试文件，补证覆盖此后唯一测试差异；没有累加这两项或重跑未变 build/unit。 |
| 根目录 `node scripts/check-protocol.mjs` | 00:53:25.415→00:53:25.933，exit0，4 schemas、27 reports、19 plans、5 capabilities、166结构正反例、23 errors、76 motor夹具、246 video估算夹具；此后协议未变。 |
| 根目录 `node scripts/check-doc-links.mjs` 与标题核对 | 01:21:31.418→01:21:31.895，exit0，3989本地文件链接；01:21:52.815→01:21:52.937，exit0，8个实际引用标题存在。链接脚本不检查标题锚点；此后修改说明与计划勾选，没有改变引用目标。 |
| 根目录 `git diff --check` 与 Task7 新增文件空白检查 | 实际 exit0；最终命令、日期和结果记录于 `task-7-final-diff-check.log`。检查保留 Git 的 LF/CRLF 提示；没有空白错误。 |

失败记录单独保留。首完整集成 `task-7-final-integration-2.log` 为1275项中5项失败，包含组件入口 `ERR_NO_BUFFER_SPACE`、私有资料同内容无 PUT 的夹具前提、旧保存自动恢复预期、具体导出核实接入和历史导航；不是包含全部后加测试的当前端点。后续 `task-7-current-integration.log` 为1282项中既有保存入口一项超时，原单例和两个文件91项诊断随后通过，但该次确定根因未证明。`task-7-current-integration-final.log` 为1282项中追加准备一项失败；原运行缺少请求归属诊断，不能追溯确定由哪个 GET 消费配额。

追加的受控诊断随后证明：在夹具授予两次 GET 后，合法普通900ms poll 可以先消费一次，第二次准备被中断，实际追加 POST=0、Store仍只有一项动作。要求操作自身固定读回的保存、递交标记、删除、导出、追加、查看目标、上传错误核实和旧能力拒绝后的核实八种调用，统一使用已有 controlledClock 冻结计时轮询；有意依赖 allow=true 普通轮询恢复的用例保持真实计时。38项录像估算针对验证通过，原配额、超时、追加次数和结果断言保持。这一反例证明夹具竞争机制，不能替代早先完整失败的请求归属证据，也不能解释没有有限 GET 配额的保存入口失败。最终通过不消除这些证据限制；首次 Task6 装配、`ERR_NO_BUFFER_SPACE` 和保存入口失败的确定根因仍未证明。

以下编号对应[并发编辑与冲突解决](acceptance.md#并发编辑与冲突解决)的 28 行。真实页面入口为[双浏览器测试](../../apps/client/tests/integration/browser-draft-conflicts.test.ts)，受控组件入口为[异常组件测试](../../apps/client/tests/integration/browser-draft-conflict-ui.test.ts)，局部领域入口为[六部分选择测试](../../apps/client/tests/unit/draft-six-part-conflicts.test.ts)、[恢复基线测试](../../apps/client/tests/unit/draft-restored-baseline.test.ts)、[独立责任矩阵](../../apps/client/tests/unit/draft-responsibility-matrix.test.ts)及[既有会话冲突测试](../../apps/client/tests/unit/draft-session-conflicts.test.ts)。表中的测试名称包含参数化分区名称；输入事实、当前保存事实和过去请求结果分别断言。

| 行 | 具体测试与输入 | 实际断言及证据层次 |
| --- | --- | --- |
| 1 | 会话 `450毫秒自动保存与连续修改重计时`；双浏览器 `清洁同步完整远端版本，曾修改后恢复基线=false不多PUT`；[完整普通接纳单元](../../apps/client/tests/unit/draft-clean-complete.test.ts)。 | 计时单元分别推进 449/1 ms 并重计时；真实 B 显示 R 的计划名称、pending、保存事实及后端 `updatedAt`，后端保存完整六部分 R 和 revision，B 的 PUT 为 0。B 的六部分完整接纳由独立字面量单元断言证明；后端断言不作为 B 私有资料的浏览器证据。 |
| 2 | `恢复已确认完整基线后450毫秒不再写相同内容且保存事实成立`；双浏览器上述 `restore=true`；`只修改并恢复%s` 七个分区；上述完整普通接纳单元的 `restore=true`。 | 真实 B 输入 temporary 再恢复 base 后显示 R 的计划名称、pending、保存事实及后端时间，后端完整 R 保持且 B 零 PUT。领域单元验证恢复完整 B 后六部分 R 共同接纳；逐部分恢复时编辑代次仍增加，但 `confirmedVersion` 不伪造，saved 成立且没有同内容 PUT。 |
| 3 | `真实纯版本推进保留完整本页输入，普通重试使用实际新revision`。 | A 的真实 HTTP 保存只推进 B 基线版本；B 的六部分 L 保留，不显示内容冲突，普通重试的请求 revision 为 `[1,2]`，最终记录为 revision 3 的完整 L。 |
| 4 | `双浏览器完整copy取消保持两侧，成功重开往返所属类型模式和原数字`；领域 `普通失败派生后真实用户写回固定旧B仍为冲突，flush不能覆盖`。 | 三方 B/L/R 不同；真实差异两侧和原后端 R 保留，取消不写；领域普通 flush 不能覆盖冲突目标。 |
| 5 | `真实UI仅pending-number不同` 与 `pending-json`；三种完整选择。 | 真实 UI 分别保留 `+`/`1e` 和 `[`/`"` 原文，其他部分相同；保存反馈、未完成原文和业务校验分别存在，选择后的原文按所属分支恢复。 |
| 6 | `被拒替换后的可靠R=L只确认当前已存在，不伪造替换成功`；完整采用分区。 | 单元可靠 R=L 只确认当前保存事实，旧替换仍明确未写入；普通完整接纳不新增 PUT。该请求归属分类由单元证明。 |
| 7 | `真实普通保存请求在途仍可继续输入，较早回执不覆盖第二份输入`；`固定unknown期间恢复B仍保留原写入核实责任`。 | 真实请求闸门期间仍能输入第二份内容，回执不覆盖它；单元相同完整输入不能清除固定 unknown，具体核实之前 saved=false。 |
| 8 | `真实保存核实先读取失败后证实未写入，只读按钮零额外PUT`。 | HTTP 未到后端的 abort 是明确的未写入测试安排；读取故障与恢复均保留固定责任，反复核实 PUT 数仍为 1，随后明确普通保存才产生新 PUT。 |
| 9 | `真实旧Y保存响应丢失后被不同Z隔离，adopt/replace/copy完成且旧请求不重发`。 | B 的 Y 真实写入后丢失响应，A 真实表单保存 Z；三种选择分别成功，B 请求数为 1/2/1，原目标及另存目标按所选版本保存，旧请求不重发。 |
| 10 | `真实B写Y回执丢失，A先Z后Y取代依据，R=L仍须明确采用且零重发`。 | 真实 revision 链为 1→2→3→4，最后 `lastWrite.revision=4`；B 仍显示旧结果不可确认，明确采用后当前保存成立且 PUT 只有旧一次。 |
| 11 | `双浏览器完整copy取消保持两侧，成功重开往返所属类型模式和原数字`。 | 新草稿身份独立、原草稿 R 不被覆盖、六部分 L 转交成功；没有异常弹窗，焦点到实际目标 Editor。 |
| 12 | 真实目标终态矩阵 `真实另存响应丢失后目标%s×来源%s再%s保留首次事实和完整输入`。 | 创建经真实后端完成后丢失回执，再中断核实；首次明确重试按同 ID 查询，unknown 时放弃不可用，不提前创建第二份。 |
| 13 | `真实另存未创建×来源%s后%s固定全输入且不改原事实`。 | 首响应受控为 `not_created`；真实后续重试保持同 attemptId 和固定完整正文，成功只产生一个创建事实；放弃无残留同份独立恢复条目。 |
| 14 | 真实目标 deleted/exported × 源 conflict/deleted/exported × retry/abandon 十二分区。 | 第一次核实只呈现终态待决定，不创建；下一明确重试才以新 ID、固定输入创建，旧 creation 与终态保持；放弃不复建旧目标。 |
| 15 | `双浏览器完整adopt取消保持两侧，成功重开往返所属类型模式和原数字`。 | 取消保留双方完整事实；确认使用 R 后不 PUT，重开所选草稿并恢复其动作、参数、对象和 pending 原文。 |
| 16 | 上述完整 replace 分区。 | B 以展示的 R revision 条件写入，查看与返回比较不写；真实后端完整 L、页面 L 与可靠保存版本一致。 |
| 17 | `真实完整替换事务前A%s后%s只处理所选责任并保留完整输入`；会话旧采用确认失效矩阵。 | 请求闸门期间 A 更新/导出/删除原目标，真实 CAS 拒绝；重试返回新比较而不抬版本重发，放弃保留完整 L 和终态。采用的陈旧资格由领域/组件验证。 |
| 18 | 会话 `%s使完整选择快照失效，旧确认不得执行`；组件旧按钮与 UI 比较更新。 | 本页编辑、远端版本或 epoch 改变均撤销旧资格；旧 callback 零副作用，不把旧比较与新输入拼接。 |
| 19 | `双浏览器可靠差异后读取不可用仍显示此前两侧且禁用覆盖`。 | 真实 GET 失败后仍显示此前可靠 L/R，禁止替换、不推断删除；原后端 revision 保持。 |
| 20 | 真实另存目标终态矩阵中的源 deleted/exported 分区；`真实App来源%s放弃已删除另存目标后保留可见只读输入及焦点`。 | 原终态不复活，保留输入仍可明确另存；有额外输入时实际源 Editor 只读并可聚焦，没有额外输入的普通导出进入记录。 |
| 21 | `Task7公开 export/delete/append 待定期间来源 updated/exported/deleted 不越过独立责任` 九分区。 | 公共领域入口产生导出未知、删除未知或追加锁；可靠观察不消除操作自身责任，不能编辑、开始另存或准备完整选择。该交叉矩阵为单元证据。 |
| 22 | `只有%s独立不同仍为完整三方冲突`；`%s整份%s保留所选事实及写入边界`；`真实UI仅%s不同` 七分区。 | 单元分别隔离 text、两种 pending 和四类资料及组合，并执行三个选择；真实 UI 构造 L，A HTTP 装配 R，其他五部分相同，面板恰有一项差异、五项一致，技术详情逐份等于完整输入。空对象与缺省资料按已定义等价比较，正文字符串仍精确比较。 |
| 23 | 三个 `双浏览器完整%s取消保持两侧，成功重开往返所属类型模式和原数字`。 | 保存后实际 reload、重开；当前 `1.0000000000000001`/`1e-999`、停用动作直接/嵌套数值、所属参数和对象模式、非法 pending 及可靠 preview sourceId 全部按独立字面量核对，未选侧 marker 不进入所选版本。 |
| 24 | `双浏览器身份可靠=%s的%s接纳远端%s按实际动作重建局部状态` 十二分区。 | 无身份/可靠身份 × clean/adopt × add/delete/reorder；实际 DOM node、数量、顺序、折叠和 JSON 模式分别核对，无身份不被动补资料。 |
| 25 | `真实两页独立新增动作后adopt/replace，持久身份及局部模式属于所选动作`。 | A、B 实际表单新增取得不同 persistent ID；所选版本动作顺序正确，B 的局部 DOM/mode 只归属于可靠对应动作。 |
| 26 | `真实A新增后B旧版本替换再新增，跳过中间版本仍保持独立身份和实际组件`。 | Aadd→Boldreplace→Badd 的真实 revision 为 2→3→4，新 ID 不复用 A；另一页跳过中间状态后不会沿用 A 的 DOM/JSON 模式。 |
| 27 | 组件 `完整差异两侧独立顺序，键盘关闭只隐藏，另存成功直接进入目标`；差异呈现单元。 | 同名、改名、重排和不可解释原文各自按本侧顺序展示；不按名称猜测共同动作，原文及诊断可查看。 |
| 28 | 上述键盘组件测试、真实三个选择焦点、真实终态放弃焦点和旧回调测试。 | Tab/Shift+Tab/Enter/Escape、执行中不能关闭、隐藏后可重开、领域锁及旧 callback 由组件验证；成功后的实际目标 Editor/记录入口与终态源焦点由真实 App 验证。 |

以下编号对应[异常弹窗补充矩阵](acceptance.md#并发选择异常弹窗补充矩阵)。

| 编号 | 具体入口与输入 | 断言及边界 |
| --- | --- | --- |
| M1 | 真实完整 copy/adopt/replace 三分区。 | 无异常弹窗，共同成功后可见目标拥有焦点；真实重开与所属模式往返保持六部分。 |
| M2 | `组件核实在途两个按钮禁用且Escape不能隐藏，旧按钮不增加GET`；真实创建响应未知。 | 执行中禁止放弃、重复请求与关闭；固定输入和领域写锁保持。 |
| M3 | 组件 `unknown/wrong_identity/wrong_input`；真实创建成功后 GET 中断。 | 错误身份、完整输入不匹配和不可用读回分别保持原 ID、资料及核实责任，不许可放弃或新建。 |
| M4 | 真实 App 中受控 not_created × 三种源状态 × retry/abandon。 | 同 ID/同 input 重试；放弃交回完整资料、原冲突/终态限制保持。首个拒绝响应属于 HTTP 替身。 |
| M5 | 组件源显示、target、validate、navigation 失败；`Task7受控组件 copy 来源 changed/deleted/exported 执行 retry/abandon`。 | 首个共同 commit 前拒绝，Map/源内容/导航没有部分提交；重试只完成本地转交、create 不增加；放弃保留完整输入及已创建事实。所有准备故障均为组件注入。 |
| M6 | 真实目标 deleted/exported 十二分区。 | 创建事实、第一次快照和目标终态保持；同次核实后待决定，下一次明确重试用新 ID 固定 input，放弃不复建。 |
| M7 | M4/M6 的源 conflict/deleted/exported；M5 的来源组件矩阵。 | 所选责任结束不解除源冲突或终态，完整输入仍归原会话；组件与真实 HTTP 证据分别保留。 |
| M8 | 真实替换条件检查前 updated/deleted/exported × retry/abandon 六分区。 | 只一 PUT；可靠拒绝撤销旧确认，重试更新比较并再次确认，放弃保持固定 L。 |
| M9 | 组件 adopt 来源 changed/deleted/exported × retry/abandon；采用资格失效后恢复。 | 旧确认不复活，重试返回确认、零 PUT；可靠放弃保留完整输入，unavailable 时放弃不可用。来源故障由组件装配。 |
| M10 | 组件 replace 来源 changed/deleted/exported × retry/abandon 与既有正常资格分区。 | 已可靠写入的 input/版本和本地接纳责任分开；重试/放弃均不 PUT，可靠更高 R 的接纳按领域规则执行，不撤销先前写入。该本地显示故障没有冒充真实 Application 故障。 |
| M11 | 公开责任矩阵、whole acceptance 48 项、目标 prepare/validate 和旧 preparedEdit/导航 callback。 | 首 commit 前再次核对代次、身份、完整 input、观察和导航；旧 snapshot 对 save/read/create/prepare/commit 均零副作用，当前普通 B 资格仍能正常完成。 |
| M12 | 公开 export/delete/append 九分区；公开另存终态关联链；私有装配的独立输入组件测试。 | 当前自然业务路径 selectedCopy 阻止第二次独立 beginCopy；终态新尝试复用同一 recovery，未处理条目仍为 1。放弃后 returned、完成后 transferred 的历史创建不是 pending。多个独立 pending copy 只能由现有私有 fake 装配证明 getter/呈现及责任隔离，不能称为真实双浏览器可达组合。 |
| M13 | 组件 Escape 隐藏、同观察、旧回调及重开；真实冲突取消焦点。 | 背景不关闭、Tab 受限、执行中关闭被拒；隐藏不解除领域锁，重开入口与资格保持，旧 callback 不能创建或导航。 |
| M14 | `真实保存核实先读取失败后证实未写入，只读按钮零额外PUT`；`保存回执和首次核实均丢失后能保存更新输入`；`真实普通保存请求在途仍可继续输入，较早回执不覆盖第二份输入`。 | 仅核实不调用普通保存；真实核实 A 后 PUT=1、revision=2，B 输入保持并可通过“保存当前输入”明确保存；点击后才 PUT=2、revision=3。450 ms 保持，较早回执不覆盖新输入。 |
| M15 | 真实 source export/delete 放弃、页面切换；组件超过三秒仍保留；真实 beforeunload reload/close。 | Editor 卸载和导航不消除责任；取消真实刷新/关闭提示后页面和未完成输入仍存在。独立 fake 输入的 beforeunload 为合成 handler 证据，与真实浏览器提示分开。取消 reload 的 Playwright 等待可以返回 TimeoutError，不据此声称导航完成。 |

导出结果核实另由[保存与导出真实页面测试](../../apps/client/tests/integration/browser-review.test.ts)的 `导出回执和即时核实丢失期间锁定编辑，具体核实恢复固定原记录且零重发` 与 `实际导出核实按钮%s只读取并保持对应输入资格` 覆盖。普通轮询读到已导出或原完整未导出基线，均保留该次 unknown、固定输入和编辑锁；明确核实后只有一个 POST，PUT 不增加，实际原请求整份记录保持。具体核实可靠未导出且对应原基线时恢复编辑；unavailable/invalid 保留固定输入、锁和再次核实入口。`Task7具体导出核实等待期间%s新观察阻止旧读回降级事实` 的四个领域分区验证新 updated/exported/unavailable/invalid 观察使旧核实结果失效。[导出读取归属单元](../../apps/client/tests/unit/draft-export-owned.test.ts)进一步区分普通 token、其他会话 owned 读取与本会话 checkExport，并验证能力重载预检使用具体核实。实际导出成功及核实成功后的记录导航按会话和 requestId 消费一次，[录像估算页面测试](../../apps/client/tests/integration/browser-video-estimate.test.ts)的 `导出核实接纳实际原请求并恢复其他草稿能力` 进一步验证用户随后选择草稿页及另一会话不会被历史事实抢回记录页。

整体审查统一实施补充了[追加首提交复核单元](../../apps/client/tests/unit/draft-append-precommit.test.ts)和[删除责任分区单元](../../apps/client/tests/unit/draft-deleted-responsibilities.test.ts)。真实 FollowOperation 形成转换后，外部显示准备中的 updated/exported/deleted/unavailable/invalid 及失效恢复均在首个 commit 前拒绝，保留输入、显示键、具体追加锁和后端已追加事实；正常重复观察、释放后基础绑定和同转换幂等仍成立。准备重入与字段资格变化是领域／受控组件证据，不能表述为真实浏览器自然发生的线上数据丢失。当前生产构建的四个真实 App 追加分区另行验证旧草稿 0／2 项动作以及 Editor 从未挂载／曾挂载释放。

双浏览器新增清洁删除验证由 A 实际点击删除按钮，B 的 Editor 结束，实际焦点回到可见草稿入口，B 零 POST／PUT、无合成离页责任，reload 不重建旧 ID。配对验证保留 save 在途、export/delete unknown 的完整只读输入与具体处理入口；领域分区补齐 recoveryContent、append、selection、copy 的独立责任。unavailable/invalid 不按删除导航，迟到 deleted 不抢已经选择的其他草稿／记录／设备页。copy 断言比较实际 `c.local` 全部源动作 ID 集合，目标全部 ID 与之不相交，sourceId 指向目标自身来源，所选业务内容、私有分支和精确数字保持。

当前源码门禁如下。2026-10-12 的独立限定复审完整核对 12 个文件的修复差异及实际门禁日志，F1、F2、F3、M1、M2 全部为 `ADDRESSED`，结论为 `Approved`，没有新增或残留的阻断项。本节保存该次复审结论及对应门禁结果。历史 Task7 端点不与当前门禁数量相加。

| 当前命令与范围 | 2026-10-12 北京时间、退出码与结果 |
| --- | --- |
| 客户端 `node node_modules/typescript/bin/tsc --noEmit` | 02:31:21.238 +08:00→02:31:21.793 +08:00，exit0；final-fix-closed-types.log。 |
| 客户端 `node node_modules/vite/bin/vite.js build` | 02:31:22.514 +08:00→02:31:22.939 +08:00，exit0，463 modules、850.60 kB/gzip254.72 kB；final-fix-closed-build.log。保留超过500 kB notice。 |
| 客户端 `node node_modules/vitest/vitest.mjs run tests/unit --maxWorkers=2` | 02:32:44.820 +08:00→02:33:04.501 +08:00，exit0，58 files、2720 tests、19.38s；final-fix-full-unit.log。 |
| 客户端 `node node_modules/vitest/vitest.mjs run tests/integration --maxWorkers=2` | 02:35:48.618 +08:00→02:43:24.548 +08:00，exit0，34 files、1291 tests、455.62s；final-fix-full-integration.log，加载上述最终 dist。 |

最初五文件针对集成为274项中1项新夹具前提失败：revision=0使整个状态先归类unavailable；改为结构合法但createdAt与已知基线不一致后，invalid/unavailable两分区通过，完整门禁使用最终输入。测试入口／日志装配错误分别登记，未用它们充当有效产品RED。协议未改变，沿用已核验Task7 protocol端点；本波受影响文件链接与BASE相对空白检查在实施报告和freeze中逐项记录。

这些验证没有重启或重新加载用户的 `localhost:4310` 服务，没有读取或修改用户 `data`，不能说明现场运行问题已经消失。软件测试不证明相机、motor、host、Docker、TX2、目标主机性能、真实电源中断或第三方 transport 的行为。上述时间均为北京时间。

## 2026-10-11 对象参数级联验证

验证环境为 Windows、PowerShell、Node.js `v24.16.0`、Vitest `5.0.1`、Vite `8.3.0`、Playwright `1.63.0` 和 Chromium `153.0.8010.12`，使用仓库锁定依赖。先同步[对象参数级联编辑](../architecture/client-editing.md#对象参数级联编辑)、草稿保存、交互和验收规格，再实施共享状态与控件；执行范围由[级联实施任务](../superpowers/plans/2026-10-11-client-parameter-fields.md#任务七对象参数级联与分支保存)跟踪。

| 门禁 | 实际结果 |
| --- | --- |
| 类型检查 | `node node_modules/typescript/bin/tsc --noEmit` 在客户端目录通过，退出码为 0。 |
| 当前网页构建 | `node node_modules/vite/bin/vite.js build` 通过；451 个模块，主包 781.09 kB，gzip 后 237.40 kB。保留既有主包超过 500 kB 的提示。 |
| 全部单元测试 | `node node_modules/vitest/vitest.mjs run tests/unit --maxWorkers=2` 通过；14:56:43 开始，43 个文件、2033 项，用时 17.72 秒。 |
| 全部集成测试 | `node node_modules/vitest/vitest.mjs run tests/integration --maxWorkers=2` 使用上述最终网页通过；14:57:47 开始，31 个文件、1066 项，用时 297.23 秒。其中包括录像参数浏览器测试的全部 30 项。 |
| 总计 | 3099 项客户端测试通过，不叠加开发阶段、独立复核或先前门禁的测试数量。 |
| 独立复核 | 评审者核验实际代码和回归矩阵，并独立运行 3 个文件、165 项测试通过；范围限定的最终复核没有剩余缺陷。 |

[对象模式状态测试](../../apps/client/tests/unit/object-variants.test.ts)覆盖首次选择不补子值、当前模式零写入、成员原文与数字事实恢复、明确重填、父级替换、类型归属、复制删除及完整保存比较。[级联控件测试](../../apps/client/tests/unit/object-branch-controls.test.ts)验证同源资格、各设备字段、不可表达原文和判别字段变化的恢复入口。[级联诊断测试](../../apps/client/tests/unit/object-branch-validation.test.ts)验证当前模式责任、缺省模式定位、成员与对象输入阶段、特殊 JSON Pointer、兄弟字段及不合格 Schema 边界；呈现前后完整原始诊断序列保持。

[参数分支服务器集成测试](../../apps/client/tests/integration/parameter-variants.test.ts)验证对象资料的四个保存仓位、身份唯一性、当前正文唯一权威、可解析与语法未完成正文的归属区别、保存重启、导出和从导出时编辑内容继续编辑。[录像参数浏览器测试](../../apps/client/tests/integration/browser-camera-parameters.test.ts)使用临时 Application、HTTP 服务和 Chromium，验证首次模式选择、双向恢复、刷新、高级 JSON 明确替换、取消与确认重填、子成员未完成原文、当前分支红框与摘要焦点、完整技术及 HTTP 诊断、不同设备选项和判别字段变化。整数秒及精确数值显示的既有回归继续通过。

现场核验将 `http://localhost:4310/` 的既有服务更新为最终代码，仍使用原 `data` 目录。重启前后，3 份草稿及其保存版本、2 份原请求、5 份报告、7 份导入、2 份媒体、预设、快照和累计确认依据逐项相同；能力版本按启动规则更新，设备能力事实保持。重新加载网页并只读打开原草稿后，完整状态读取结果与重启后的结果相同。原草稿中的 `exposure: 1` 和未完成原文保持，页面提供该字段的重新填写确认入口，没有代替用户选择曝光模式。实际页面截图为 `.superpowers/sdd/2026-10-11-client-parameter-fields/exposure-cascade-runtime.png`。

合法 `auto` 与 `compensation_ev: 0.7` 的界面示例使用隔离测试草稿，截图为同目录的 `exposure-cascade.png` 和 `exposure-cascade-field.png`；最终集成门禁重新捕获并实际查看。类型、构建、单元、集成及独立评审记录保存在同目录的 `cascade-*.log` 和 `cascade-final-review.md`。

这些结果证明客户端软件、持久化和浏览器之间的协作行为，不证明实际相机执行、目标嵌入式主机性能或物理断电。本项没有改动相机执行或公共能力 Schema；对应组件及跨组件验证由各自记录维护。上述时间为北京时间。

## 2026-10-11 JSON 字段说明与输入阶段验证

验证环境为 Windows、PowerShell、Node.js `v24.16.0`、Vitest `5.0.1`、Vite `8.3.0`，使用已有依赖。行为分类见[根据 Schema 生成表单](../architecture/client-editing.md#根据-schema-生成表单)和[校验与导出](../architecture/client-editing.md#校验与导出)，实施范围见[参数字段实施计划](../superpowers/plans/2026-10-11-client-parameter-fields.md#任务六高级字段说明与输入阶段)。

| 门禁 | 实际结果 |
| --- | --- |
| 类型检查 | `node_modules/.bin/tsc.cmd --noEmit` 在客户端目录通过。 |
| 当前网页构建 | `node node_modules/vite/bin/vite.js build` 通过；448 个模块，主包 765.07 kB，gzip 后 233.42 kB。保留既有主包超过 500 kB 的提示。 |
| 全部单元测试 | `node node_modules/vitest/vitest.mjs run tests/unit --maxWorkers=2` 通过；12:58:55 开始，40 个文件、1955 项，用时 14.41 秒。 |
| 全部集成测试 | `node node_modules/vitest/vitest.mjs run tests/integration --maxWorkers=2` 使用上述当前网页通过；12:59:29 开始，31 个文件、1016 项，用时 299.90 秒。 |
| 总计 | 2971 项客户端测试通过，不叠加先前门禁或开发阶段窄验证数量。 |

[字段规则说明单元测试](../../apps/client/tests/unit/json-field-rules.test.ts)验证直接对象与 `oneOf` 的同源事实、引用与未覆盖组合的原规则、布尔 Schema、精确数值、说明页模式及查看零写入。[中文诊断单元测试](../../apps/client/tests/unit/validation-presentation.test.ts)验证已声明直接字段在没有标题、标题为空白、合法布尔规则和公共字段同名时使用真实名称，并保持未知属性、继承属性及身份不匹配的未识别状态。

[录像参数浏览器测试](../../apps/client/tests/integration/browser-camera-parameters.test.ts)覆盖缺失与非法曝光值的明确多行 JSON 编辑、两种对象分支事实、非法旧值 `1` 与裸 `auto` 原文并存、字段／参数 JSON／整份计划 JSON 的分段诊断、独立枚举选择、保存与刷新，以及用户显式填写完整自动曝光对象后的重新校验和其他参数保持。[规则展示浏览器测试](../../apps/client/tests/integration/browser-rule-display.test.ts)经真实编辑与说明入口核对本地 `$ref`、兄弟约束、`true` 和 `false` 原规则，查看前后内容与保存版本保持。[校验浏览器测试](../../apps/client/tests/integration/browser-validation.test.ts)覆盖不可解析正文与根未完成原文同时保留，诊断数量、原始机器码及两个输入的焦点定位。

运行页面使用 `http://localhost:4310/` 的现有服务，重新加载当前构建后打开原草稿。曝光显示 JSON 标记、多行编辑区、两个分支的字段事实与允许值，并区分当前未完成文本和草稿正文诊断。只读查看前后，完整状态读取结果相同；原 `exposure: 1` 与 pending `auto` 原文保持，没有为用户补曝光对象。页面截图保存在 `.superpowers/sdd/2026-10-11-client-parameter-fields/exposure-json-editor.png`。

这些结果只证明客户端软件与本地页面行为，不证明实际相机、目标主机性能或物理断电。本项没有改动 Python 执行或跨组件交接；它们的实际结果由各自验证记录维护。上述时间为北京时间。

## 2026-10-11 参数字段与精确数值验证

验证环境为 Windows、PowerShell、Node.js `v24.16.0`、Vitest `5.0.1` 和 Vite `8.3.0`。使用已有依赖，直接执行相应工具入口。实施范围和行为分类见[参数字段实施计划](../superpowers/plans/2026-10-11-client-parameter-fields.md)。

| 门禁 | 实际结果 |
| --- | --- |
| 类型检查 | `node_modules/.bin/tsc.cmd --noEmit` 在客户端目录通过。 |
| 当前网页构建 | `node node_modules/vite/bin/vite.js build` 通过，构建 447 个模块；主包 760.95 kB，gzip 后 232.24 kB。 |
| 全部单元测试 | `node node_modules/vitest/vitest.mjs run tests/unit --maxWorkers=2` 通过；11:33:01 开始，39 个文件、1931 项，用时 16.90 秒。 |
| 全部集成测试 | `node node_modules/vitest/vitest.mjs run tests/integration --maxWorkers=2` 使用上述当前网页通过；11:33:01 开始，31 个文件、999 项，用时 296.94 秒。 |
| 总计 | 2930 项客户端测试通过，不叠加开发阶段的窄测试或审查临时检查数量。 |

构建保留主包超过 500 kB 的提示，没有修改阈值或隐藏提示。上述时间为北京时间。

[参数选项单元测试](../../apps/client/tests/unit/parameter-options.test.ts)验证完整目录优先、逐字段独立资格、复杂兄弟字段及不相关数值精度损失、空允许集、根跨字段约束与未完成输入。[数值边界测试](../../apps/client/tests/unit/numeric-boundaries.test.ts)和[精确步进测试](../../apps/client/tests/unit/numeric-multiples.test.ts)验证原数字比较、整倍数、数组、本地引用、条件分支和根标量上下文，以及无效规则的拒绝。[诊断规范化测试](../../apps/client/tests/unit/schema-issues.test.ts)与[中文诊断测试](../../apps/client/tests/unit/validation-presentation.test.ts)验证四类范围错误在复制和展示时保留原边界。

[录像参数浏览器测试](../../apps/client/tests/integration/browser-camera-parameters.test.ts)使用临时 Application、真实 HTTP 服务和 Chromium，验证五个独立枚举、整数时长、曝光 JSON、可选字段缺省、未完成原文保存及刷新、超界和非整数导出拒绝，并核对 HTTP 诊断、字段错误及顶部摘要的原上界。[规则展示浏览器测试](../../apps/client/tests/integration/browser-rule-display.test.ts)验证编辑页、说明页和技术详情中的包含／严格边界、直接／引用规则、嵌套默认值及数字字符串；查看及切换视图不改变草稿内容或保存版本。

现场核验使用 `http://localhost:4310/` 的本地服务与 `data` 目录。普通录像时长规则从 Python 共享驱动定义取得后替换运行能力中的对应字段，其余设备身份与能力内容保持。服务重启前后核对草稿、原请求、预设、快照、报告、导入和媒体等保存内容相同。当前设备说明页实际列出五个枚举的允许值和精确上界 `9223372036854775`，已查看并保存全页截图；只读校验入口对 `1`、`1.0`、`1e0`、最大整数秒返回 200，对零、小数秒和上界加一返回 400，越界诊断仍返回原上界。

这些结果证明客户端软件组件和当前本地页面的行为，不证明实际相机、目标主机性能或物理断电。Python 组件与跨组件结果按其各自验证记录维护。

## 2026-10-11 Linux 协议同步复验

客户端分支合并后，在 Linux x86_64 容器使用 Node.js 24.20.0、pnpm 12.3.4 和 `VITEST_MAX_WORKERS=2` 依次运行 `pnpm --dir apps/client run test:unit` 与 `test:integration`。全部单元 1864 项通过，全部集成与浏览器 972 项通过；集成入口的前置类型检查和网页构建通过。构建保留主包体积及依赖模块指令提示，具体数值和范围见[协议同步验证](../camctl/verification.md#视频大小估算的协议同步验证2026-10-11)。

视频估算的 Python 同源导出、包内资源、真实 HTTP 草稿与能力交接、计划导出、CLI 受理执行及报告导入使用单独的跨组件用例核验。其实际文件事实与估算值分别断言，完整证据由上面的协议同步验证记录维护。这组软件结果不证明 ARM 主机或实际相机已经通过演示验收。

## 自动化检查入口

下列命令在仓库的 `apps/client` 目录执行。

| 检查 | 命令与内容 |
| --- | --- |
| 类型检查 | `pnpm run typecheck`，检查服务端、网页和共享规则的接口。 |
| 构建 | `pnpm run build`，检查类型并构建随本地服务交付的网页。 |
| 单元测试 | `pnpm run test:unit`，验证独立函数的行为契约；使用受真实接口约束的替身控制协作者输入和故障。 |
| 集成测试 | `pnpm run test:integration`，先构建当前网页，再验证真实 SQLite、文件、HTTP、服务进程和 Chromium 浏览器之间的协作。 |
| 完整测试 | `pnpm test`，依次执行单元测试和上述集成检查。 |

浏览器测试使用本次源码生成的网页。公共协议检查在仓库根执行 `node scripts/check-protocol.mjs`；文档文件链接检查在仓库根执行 `node scripts/check-doc-links.mjs`，该脚本不校验标题锚点。

## 2026-10-10 完整软件验证

验证环境为 Windows、PowerShell、Node.js `v24.16.0`、pnpm `11.25.0` 和 Vitest `5.0.3`。`pnpm run typecheck` 与 `pnpm test` 均以退出码 0 结束。

| 门禁 | 实际结果 |
| --- | --- |
| 独立类型检查 | `tsc --noEmit` 通过。 |
| 单元测试 | 04:59:19 开始，28 个文件、1053 项通过，用时 1.70 秒。 |
| 集成前构建 | `tsc --noEmit && vite build` 通过；Vite `8.3.3` 构建 341 个模块，主包 653.75 kB，gzip 后 199.14 kB。 |
| 集成测试 | 04:59:22 开始，22 个文件、539 项通过，用时 123.18 秒，其中包含真实 Chromium 浏览器测试。 |
| 总计 | 1592 项测试通过；此数不再叠加开发阶段的窄测试数量。 |

构建保留 Base UI 模块中 `"use client"` 指令的提示，以及主包超过 500 kB 的提示，没有通过抑制提示修改门禁结果。上述时间均为北京时间。

### 默认服务首次导出与重启

[默认运行入口集成测试](../../apps/client/tests/integration/default-runtime.test.ts)直接启动 `node --import tsx src/server/main.ts`，由 main 创建使用默认随机源的 Application。初始数据目录仅包含有效空设备能力目录，进程日志放在数据目录之外。测试先确认 `uninitialized`，再显式初始化、保存合法 `report_status` 草稿，并使用同次状态响应中的能力版本首次导出。

完整测试中的运行记录为 2026-10-10 04:59:27：保存 revision 为 2，首次固定请求 ID 为 `4246265514487217159`，原请求下载为 256 字节。ID 是 1～9223372036854775807 范围内的规范十进制字符串，正文身份与记录一致。进程终止后使用同一数据目录重新启动，无需初始化即可进入 `ready`；同一 ID 的下载字节与首次完全一致，再次导出仍返回原固定请求。能力版本从 `e7053ef1-66b1-4a67-b74a-2b1635ac359a` 变为 `95c51048-8609-4dff-92f9-d18862559a97`，没有改变已固定请求。

该测试在 Windows 下验证的是进程终止后的持久化恢复。它没有证明优雅停机、设备物理断电或主机实际执行 `report_status` 的效果。[身份分配单元测试](../../apps/client/tests/unit/request-id.test.ts)和[请求集成测试](../../apps/client/tests/integration/requests.test.ts)另行覆盖候选边界、有限占用重选、随机源失败、首次查询失败、已确认占用后的查询失败，以及固定请求优先返回；查询故障停止分配，并保持整个草稿和原请求集合。

### 输入、自动预览与后续计划

输入验证覆盖私有编辑原文的保存、未完成输入、原数字的数学分类，以及保存、重开、导出和复制之间的衔接；规则分别见[公共编辑规则](../architecture/client-editing.md)和[原请求保存](storage-and-recovery.md#草稿预设与原请求的保存)。整组动作确认替换、移除、取消、集合外输入保留及逐项显示状态由动作集合单元测试、网页编辑和身份浏览器测试共同验证。

[类型切换单元测试](../../apps/client/tests/unit/web-editing.test.ts)覆盖直属专属字段、共用字段、停用类型值及嵌套字段的原数值事实，区分可靠恢复原文、旧资料的实际保存值和用户明确替换的新值。类型候选按数学事实匹配；仅 Number 投影相同的候选保持，真正歧义的恢复保留输入并拒绝。[输入保存集成测试](../../apps/client/tests/integration/plan-input.test.ts)验证 Session、HTTP、SQLite 保存、重开及类型恢复的完整组合，并覆盖复制和能力协调删除后的资料映射。合法当前类型可以固定导出；恢复为非法当前类型后仍拒绝导出且不新增请求。保存故障保持旧完整记录和页面输入。

[动作身份浏览器测试](../../apps/client/tests/integration/browser-editor-identity.test.ts)验证真实类型选择保存及恢复直属原数值，并保持其他动作的折叠和局部 JSON 模式。恢复原文解析或结构非法、候选歧义时，Editor 显示诊断并保持正文、pending 和停用资料，不发送保存请求，也不产生未处理的页面异常。损坏恢复原文例使用状态响应故障注入，后端原记录仍是有效私有结构；该验证不代表服务端接受这些损坏结构。

自动预览测试分别覆盖 `unset`、`enabled`、`disabled`，可靠关联、能力故障、当前类型、名称和时间的独立事实，以及能力重新加载时开放编辑会话的保存屏障。能力故障时的保留与停止、明确非拍摄或不支持预览时的移除、名称恢复和支持确认后的派生，按[自动预览规则](automatic-previews.md)分别判断。

共同追加测试覆盖状态同步、按计划取回、动作取回和指定产物取回的准备、确认、未知结果核实及本地接纳。没有身份资料时按已固定追加位置映射新动作；全长可靠资料保留原身份；资料项数不对应时在形成 expected 和发送追加请求前拒绝。追加已提交而本地接纳失败时保留本地恢复责任，不重发追加请求。对应规则见[动作显示状态与追加接纳](../architecture/client-editing.md#动作显示状态与追加接纳)。

### 报告、媒体与页面结果

[网页业务流程测试](../../apps/client/tests/integration/browser-workflow.test.ts)分别验证首次导出、再次下载同一请求、复制后导出新请求，以及混合导入真实报告与录制视频后的关联、核验、播放和 Range 下载。[报告集成测试](../../apps/client/tests/integration/reports.test.ts)覆盖合法乱序、相同水位事实一致、取消或过期前已执行事实的保留、来源矛盾拒绝和重启恢复。[恢复浏览器测试](../../apps/client/tests/integration/browser-recovery.test.ts)覆盖 20→40→45 的同步进度，以及核验成功但不能解码的媒体仍可下载。

[媒体集成测试](../../apps/client/tests/integration/media.test.ts)覆盖报告与文件两种到达顺序、核验状态、原字节、响应媒体类型和 `nosniff`；[文件集成测试](../../apps/client/tests/integration/files.test.ts)覆盖不完整上传、正确副本补发、登记结果未知、暂时不可读、完整目录恢复及冲突依据的保留。[媒体浏览器测试](../../apps/client/tests/integration/browser-media.test.ts)用报告与图片夹具验证百张图片的分页、选择、刷新、解码与预览操作。各测试共同证明客户端组件协作，不能合并表述为一次 main 进程执行全部报告、媒体和设备业务。

页面反馈、记录折叠与导入分页由[网页评审测试](../../apps/client/tests/integration/browser-review.test.ts)验证：提示归属发起页面，计划与动作独立折叠，轮询保留选择，23 个文件分为 10、10、3 条，轮询保留当前页。

### 页面阶段视检

2026-10-10 的网页阶段验证实际查看了媒体页桌面全页截图（1280×3601，视口 1280×720）和窄屏全页截图（390×2911，视口 390×844），以及 390 像素编辑页的 enabled、unset 和动作复制截图。自动预览三态在桌面与窄屏的两个交互测试、窄屏动作复制交互测试均通过。该记录说明本阶段实际查看的页面和交互，不代表最终全部页面都经过截图检查。

## 2026-10-10 界面交互验证

界面交互实施范围见[界面交互计划](../superpowers/plans/2026-10-10-client-ui-interactions.md)。验证环境与当日完整软件验证一致，网页测试使用独立临时数据目录，不连接真实设备。以下结果来自界面改动完成后的完整客户端测试，不与当日上午的测试数量叠加。

| 门禁 | 实际结果 |
| --- | --- |
| 独立类型检查 | `pnpm run typecheck` 通过。 |
| 单元测试 | 14:49:53 开始，29 个文件、1067 项通过，用时 1.81 秒。 |
| 集成前构建 | `tsc --noEmit && vite build` 通过；Vite `8.3.3` 构建 386 个模块，主包 685.06 kB，gzip 后 208.86 kB。 |
| 集成测试 | 14:49:56 开始，25 个文件、561 项通过，用时 124.57 秒。 |
| 总计 | `pnpm test` 以退出码 0 结束，54 个文件、1628 项测试通过。 |

上述时间均为北京时间。构建仍有 Base UI 模块中 `"use client"` 指令提示和主包超过 500 kB 的提示。

[导航浏览器测试](../../apps/client/tests/integration/browser-navigation.test.ts)覆盖弹窗焦点进入、循环和恢复，Escape 关闭，多入口共享行为，提交挂起期间保持操作，以及未知追加结果的恢复。计划页签验证方向键、Home、End、唯一 Tab 停靠点及页签与面板关联。弹窗内的选择列表先响应 Escape 关闭列表，随后才关闭弹窗。

[校验呈现单元测试](../../apps/client/tests/unit/validation-presentation.test.ts)和[校验浏览器测试](../../apps/client/tests/integration/browser-validation.test.ts)覆盖中文摘要、公开字段、相机参数、内置引用、报告范围、折叠动作和删除前项后的定位。未完成数字、JSON、不可解析的原始键及根路径保留原文并定位各自控件；无准确表单控件时展示整份 JSON。定位操作保持草稿正文和未完成输入，不发送保存请求。字段错误通过 `aria-invalid` 和 `aria-describedby` 关联，技术详情保留原始路径、错误码和说明。

[样式浏览器测试](../../apps/client/tests/integration/browser-ui-styles.test.ts)验证原生文件输入的键盘焦点可见，以及取消文件选择不上传或新增资料。实际视检覆盖 390、520、800、1440 像素宽度的标题、按钮、长名称和文件焦点，未观察到页面横向溢出。520 像素下的 200% 检查使用 CSS 放大，不作为浏览器原生缩放的验证证据。

实际浏览器另行核验了中文摘要、点击错误后展开并聚焦执行时间、Escape 关闭弹窗并返回入口，以及 390×844 窄屏编辑页。独立代码复审与未完成输入反例验证通过；上述范围不表示所有页面或其他浏览器均已完成视检。

## 2026-10-10 视频大小估算验证

实施范围与独立组件分工见[视频大小估算计划](../superpowers/plans/2026-10-10-video-size-estimate.md)，行为依据见[批准设计](../superpowers/specs/2026-10-10-video-size-estimate-design.md)。验证环境为 Windows、PowerShell、Node.js `v24.16.0`、pnpm `11.25.0`、Vitest `5.0.3` 和 Vite `8.3.3`；命令工作目录为 `apps/client`。使用明确虚构的能力说明，不连接真实相机。

下列完整结果来自最终客户端源码的 `pnpm test`，当前命令环境设置 `VITEST_MAX_WORKERS=2`，结束后恢复原环境。该命令依次执行 `pnpm run test:unit`、`pnpm run test:integration`，后者的前置脚本执行 `pnpm run build`。文档收尾没有修改生产或测试源码，直接核对并复用同日任务记录目录中的 `task-4-fix-r1-full-test.log` 与 `task-4-fix-r1-report.md`，没有在文档阶段重新执行客户端套件。

| 门禁 | 实际结果 |
| --- | --- |
| 单元测试 | 北京时间 20:08:04 开始，33 个文件、1220 项通过，用时 10.50 秒。 |
| 集成前构建 | `tsc --noEmit && vite build` 通过；构建 392 个模块，主包 697.36 kB，gzip 后 212.21 kB。 |
| 集成测试 | 北京时间 20:08:17 开始，27 个文件、842 项通过，用时 203.17 秒。 |
| 总计 | `pnpm test` 退出码 0，60 个文件、2062 项通过；不叠加开发阶段专项数量。 |

构建保留依赖模块的 `MODULE_LEVEL_DIRECTIVE`（`"use client"` 指令可能无法保留）警告，以及主包超过 500 kB 的警告。通过范围是上述两个工作进程条件；默认并发的完整集成运行曾在页面启动、连接或关闭边界失败，具体资源机制未核实，不能据此记录默认并发已稳定通过。

[计算单元测试](../../apps/client/tests/unit/video-size-estimate.test.ts)和[指针单元测试](../../apps/client/tests/unit/json-pointer.test.ts)验证精确作用域、自身成员读取、三种时长方法、码率来源、无默认补齐、非整除采集比例、原始帧数整数性、数值溢出或下溢及十进制格式。[能力集成测试](../../apps/client/tests/integration/video-estimate-capabilities.test.ts)验证 246 条共同原始文本、完整演示计算、JSON 交接及失败重载保留旧目录。录像示例得到 975 MB，延时示例得到 8 秒与 175 MB；这些是虚构规则的计算结果。

[编辑状态单元测试](../../apps/client/tests/unit/video-estimate-state.test.ts)覆盖相关、无关和范围未知的未完成输入及状态优先级。[完整观察单元测试](../../apps/client/tests/unit/state-observations.test.ts)验证两种准备失败起点、请求结束后的观察资格、成功接纳次序和核实责任。[估算浏览器集成测试](../../apps/client/tests/integration/browser-video-estimate.test.ts)的 29 项覆盖表单、JSON、预设、类型与设备切换、动作增删、草稿重开、真实 HTTP 数字词元交接、重载暂停与恢复，以及计算、模式切换和折叠不新增保存。

浏览器反例分别从刷新、保存与导出核实、删除核实、导出记录、递交标记、上传失败核实、后续动作准备与核实、查看目标取得完整观察，证明恢复使用同份能力、诊断与草稿。再次准备失败保留既有待核实责任；准备观察不解除暂停；旧成功晚到不覆盖新成功；观察成功不补造保存、导出或追加事实。实际编辑仍正常保存，自动预览仍按原契约协调。

本节证据只确认公共格式与客户端的软件行为。Python 同源导出、发行物及真实 describe → 客户端 → 受理、执行、报告闭环由 Linux 环境 Agent 验证，不能从 Windows 客户端测试推断通过。真实码率、档位有效性、成片帧数关系和统计范围仍属于设备接入前提。

## 公共协议验证

2026-10-10，在同一 Windows、Node.js `v24.16.0` 环境执行 `node scripts/check-protocol.mjs`，退出码 0。检查通过 4 份 Schema、27 份报告及文件名摘要、19 份计划、4 份能力、166 项结构正反例、23 项动作错误分类和 76 项电机共同精确数值例。

协议脚本验证公共样例与结构、数字及错误分类；跨实体关系、报告合并与客户端恢复由组件测试验证。客户端读取公共夹具不等于 C host 或 camctl 已实际执行对应动作，跨组件与设备证据分别记录。

视频大小估算的客户端文档收尾在同日同一环境执行 `node scripts/check-protocol.mjs`，退出码 0：4 份 Schema、27 份报告及摘要、19 份计划、5 份能力说明、166 项结构正反例、23 项动作错误分类、76 项电机共同精确输入，以及 246 项视频大小估算能力正反例通过。随后执行 `node --test scripts/check-protocol.test.mjs`，247 项通过，无失败、取消或跳过，退出码 0。这些根门禁由文档收尾实际执行，与复用的客户端完整测试分别记录。

## 文档引用检查

2026-10-10，在上述 Windows、PowerShell、Node.js `v24.16.0` 环境执行 `node scripts/check-doc-links.mjs`，退出码 0，3630 个本地文件链接通过。该脚本不校验标题锚点；新增引用的公共编辑、自动预览、保存和主机规则标题已人工核对，既有“电机控制接入”标题继续保留。

当日界面交互验证记录补齐后再次执行同一命令，退出码 0，3644 个本地文件链接通过。界面交互计划引用的本页标题已人工核对。

当日视频大小估算客户端责任文档和验证记录补齐后，执行 `node scripts/check-doc-links.mjs`，3756 个本地文件链接通过，退出码 0。新增估算设计、编辑专题、页面提示和验证记录之间的标题引用已人工核对。`git diff --check` 用于检查本次文档差异的空白错误，实际结果记录于客户端文档收尾报告。

## 验收对应关系

| 业务组 | 软件证据 |
| --- | --- |
| 首次部署与准备录像 | [数据库](../../apps/client/tests/integration/database.test.ts)、[应用](../../apps/client/tests/integration/application.test.ts)和[网页编辑](../../apps/client/tests/integration/browser-editing.test.ts)测试验证启动分类、显式初始化、能力重载、草稿、预设和编辑。 |
| 导出、交接与重送 | 请求、默认运行入口和网页业务流程测试验证可靠保存版本、首次固定、重复下载、复制新请求及同目录重启。 |
| 报告与媒体导入及结果 | 报告、文件、媒体及网页业务流程测试验证原文、到达顺序、核验、播放和下载。 |
| 准备后续计划 | [网页后续操作单元测试](../../apps/client/tests/unit/web-followup.test.ts)、网页业务流程与评审测试验证准确引用、追加位置、未知结果核实及本地接纳。 |
| 缺少历史与补齐 | 恢复浏览器与报告测试验证业务进度、缺口目标、反序到达以及完整和局部同步。 |
| 中断与恢复 | 数据库、文件、[状态完整性](../../apps/client/tests/integration/state-integrity.test.ts)和请求测试验证事务回滚、不完整上传、完整副本恢复、正确补发及原请求恢复。 |

## 历史验证记录

2026-09-16，在 Windows 从 `apps/client` 独立安装锁定依赖并执行 `npm test`，14 个单元文件的 515 项和 14 个集成文件的 217 项全部通过，总计 732 项；集成前的类型检查和网页构建通过。目录调整验证记录还包括 37 份协议与部署样例、演示生成器的 15 份报告导入和视频核验，以及使用显式原数据路径打开既有资料。文档文件链接检查通过，其范围不含标题锚点。

该阶段另有表单与设备指南验证记录：501 项单元测试和 211 项集成测试通过，其中编辑与评审浏览器场景为 73 项。历史数量来自不同执行记录，不相加，也不作为 2026-10-10 的当前测试数量。历史页面检查包含 1440 像素桌面和 390×844 窄屏的表单、指南、导入分页及输入修正入口；本次页面视检范围以本页的日期记录为准。

## 电机控制接入

2026-10-08，在 Linux 开发容器、Node.js 24 和 Playwright Chromium 中验证电机动作编辑、导出、报告导入及重启恢复。`pnpm --dir apps/client typecheck` 通过；单元测试为 16 个文件、530 项通过，集成测试为 17 个文件、370 项通过，无跳过。独立复核重跑 166 项电机测试和 2 项 Chromium 原参数展示测试，并复现派生恢复的回滚与重试。这是历史增量记录，2026-10-10 的 Windows 完整测试数量见前表。

位置与最大延迟按原 JSON 数字精确验证。受理失败的任意形状原参数保留类型、层次及数字表示，覆盖长尾小数、`1e999`、零的等价写法、旧报告与相同边界报告、SQLite、HTTP、网页及重启。原报告、派生身份、业务状态和覆盖进度在故障时共同回滚，恢复失败后可以重试。成功展示“控制通知已发送”，不解释为电机到位。

真实客户端导出、C host 回调及报告领取导入的组合证据见[电机控制实施计划](../superpowers/plans/2026-10-08-camctl-motor-control.md)。运行时需要原生 RawJSON 支持；上述软件测试不证明真实电机到位、TX2 性能或物理断电恢复。

## 外部验证边界

客户端软件测试使用独立临时资料，不能代替现场设备与部署验收。当前设备交接中，Action6 普通录像演示已于 2026-10-11 由用户现场确认通过：用户确认客户端生成取回计划并完成独立取回，原始报告、媒体文件和确认水位未归档，因此不补写逐项检查或媒体测量值，详见[T9 现场验收状态](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#现场验收状态与部署入口)及[现场验收记录](../camctl/verification.md#action6-普通录像演示的现场验收2026-10-11)。Hasselblad CFV 100C 单张 RAW 拍摄、配对 JPEG 预览登记、分别独立取回及最终报告已完成软件和 ARM 真机业务验收；重复取回后，ARM 主机上的正式原图及关联预览仍保持，详见[接入计划](../superpowers/plans/2026-10-12-hasselblad-integration.md#arm-真实业务验收状态)和[ARM 现场记录](../camctl/verification.md#hasselblad-arm-真实拍摄与取回验收2026-10-12-utc)。这不证明相机端保留原片、物理拔线或第三方实际领取。

截至本记录对应的当前真机调试范围，仍待完成的是 Action6 原生延时摄影调试和 OSMO 360 II 完整真机调试；普通录像及 Hasselblad 单张拍摄的通过结果不能代替这两项验收。归档的 2026-10-10 Action6 延时现场采样未取得可确认完成的新增媒体，OSMO 360 II 完整设备链也没有完成，当前交接进度见[T9 设备事实与 ARM 验收计划](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#t9-设备事实补齐与-arm-linux-完整验收)。

容器部署仍需按实际部署结果单独确认，本机测试不作为 Docker 运行成功的证据。目标主机性能、物理断电、真实第三方领取及其保留文件名和原字节的能力，仍作为后续外部验收范围保留；它们不因上述两项当前真机调试的完成而自动通过。其他浏览器及大型历史库首次恢复耗时也没有由客户端验证记录证明。

`apps/client/tests/helpers/media.ts` 使用浏览器录制短 WebM，计算实际字节数与 SHA-256，并生成字段匹配的报告，用于验证网页视频播放和文件一致性；图片夹具用于验证展示及文件流程。这些夹具不是相机实拍产物。客户端核验与浏览器解码分别判断：浏览器使用原生 `<video>`，即使核验通过，也不能据此推断当前浏览器支持媒体编码；仍可下载原文件。协议样例中的占位摘要不能证明任意同名文件匹配。
