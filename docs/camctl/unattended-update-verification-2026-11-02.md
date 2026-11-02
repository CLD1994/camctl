# 无人值守更新验证记录（2026-11-02）

实施范围见[专项计划](../superpowers/plans/2026-11-02-unattended-updates.md)，行为依据见[组件版本与更新](../architecture/versioning-and-updates.md)和[主程序接口](../architecture/software-update-interface.md)。本记录持续补充实际结果，未执行场景不能由已通过部分推断。

## 普通更新的容器实际见证

环境为当前 Linux x86_64 容器、Python 3.11.17、SQLite 3.53.1。隔离目录为 `/workspaces/camctl/tmp/unattended-validation-20261102`。没有操作现存部署、客户端数据库或相机，没有执行集成测试套件。

旧 wheel 来自 `cd895697` 的独立源码快照，仅将快照中的项目版本改为 `0.9.0`，作为实际安装的旧版本输入；正式源码版本保持 `1.0.0`。旧库由真实 `camctl init` 建立，并通过 `submit` 保存一份引用未配置设备的计划，没有设备调用。初始库为 V9，含一份计划及一个动作；另准备配置、额外环境文件、主机媒体和主程序待传文件作为比对对象。

候选更新包由 `build_update.py` 正式入口从 `3d1b8aea` 构建，版本 `1.0.0`，包含锁定离线依赖。C 模块由 `19b874ed` 以 Release、`HOST_BUILD_TESTS=OFF` 构建；两源码点的生产代码一致，后者仅推进计划记录。新 wheel 的发布器通过原虚拟环境之外的 CPython 发布维护入口，退出 0。

使用实际 `host-demo --apply-update` 调用公开更新入口。外部 `sitecustomize` 门闩仅暂停普通 `camctl run`，维护解释器以 `-I` 执行且清除 `PYTHONPATH`，恢复和安装不被门闩替代。门闩使数据比对发生在新 `run` 已启动、业务尚未打开状态库之前。

| 实际检查 | 结果 |
| --- | --- |
| 离线安装、更新提交及公开调用 | host-demo 报告更新完成，持久记录为 committed，原路径版本为 1.0.0。 |
| 数据库及历史保留 | 实例身份、目录绑定及完整逻辑转储摘要相同，计划和动作各一条。 |
| 配置与文件保留 | 配置、原环境额外文件、staging 媒体、ready 文件及 processing 原文件的内容摘要、大小、权限和所有者均相同。 |
| 自动运行 | 新版本 `run` 进程已经进入门闩，释放后正常退出 0。 |
| 普通业务继续 | 同一 host 继续接受重复提交并确认处理完成，CLI 正常退出 0；随后 quit 使该 host 结束。 |

主程序最初的旧 `run` 在更新收场中异常结束并产生独立诊断；该事实保留在日志，不影响本次更新和后续新进程的真实成功。主程序退出已观察，原监督脚本没有保留其最终 wait 返回值，因此不将 host 进程退出码记为已知 0。

可审计结果见[普通更新](verification-data/2026-11-02-unattended-updates/ordinary-success.json)、[运行前数据比对](verification-data/2026-11-02-unattended-updates/ordinary-before-run.json)、[包身份](verification-data/2026-11-02-unattended-updates/ordinary-candidate-release.json)、[旧 wheel 构建](verification-data/2026-11-02-unattended-updates/baseline-build.json)及[C 构建](verification-data/2026-11-02-unattended-updates/host-build-ordinary.json)。完整原始日志、命令及收据留在上述隔离目录，当前候选只作为阶段验证输入。

## 普通安装失败的实际回滚

在另一个全新独立环境安装同一旧 wheel 和初始数据。通过 PATH 中的外部 uv 包装程序真实执行锁定依赖安装、camctl 安装及依赖检查；只有真实检查成功后才注入退出码 73。包装保留安装 worker 继承的资格描述符，未替代真实安装和回滚。

结果：接口报告本次更新失败，持久记录 rolled_back；原路径重新输出 0.9.0。恢复后、普通 run 打开数据库之前，实例身份、逻辑转储摘要和五个配置/文件对象全部相同。新的旧版本 run 自动启动，释放外部门闩后正常退出 0；同一 host 随后继续提交成功并以 quit 正常退出 0。

独立诊断直接出现在 processing，包含注入工具的原始失败标记和更新日志/收据内容，没有仅留下本地证据路径。见[场景结果](verification-data/2026-11-02-unattended-updates/ordinary-install-failure-scenario-result.json)及[运行前恢复比对](verification-data/2026-11-02-unattended-updates/ordinary-install-failure-rollback-before-run.json)。

## 更新与回滚连续中断后的普通启动

第三个独立环境在真实离线安装及依赖检查结束后暂停，核对持久记录为 updating、修改意图为 true，实际安装版本已为 1.0.0。对该更新进程组发送 SIGKILL，监督进程取得返回 -9。

随后直接启动普通 host-demo，不传恢复选项。外部 LD_PRELOAD 门闩在真实 rename 已将完整旧环境放回原 venv 路径后暂停，此时数据库恢复尚未执行。记录 host 及其维护子进程树和组身份，仅对该次树中的组发送 SIGKILL，host 返回 -9。该门闩不代替 rename，不向生产程序新增测试开关。

再次正常启动同一 host 时，组件继续原冻结事务，最终记录 rolled_back，原版本 0.9.0 恢复。新 run 进入业务之前，实例身份、逻辑数据及所有比对对象与更新前一致。恢复诊断直接发布到 processing；释放门闩后 run 和后续 submit 均正常退出 0，host 最终 quit 退出 0。

见[第一次中断](verification-data/2026-11-02-unattended-updates/ordinary-repeated-interruption-interruption.json)、[恢复再次中断](verification-data/2026-11-02-unattended-updates/ordinary-repeated-interruption-recovery-interruption.json)、[恢复数据比对](verification-data/2026-11-02-unattended-updates/ordinary-repeated-interruption-recovery-before-run.json)及[最终结果](verification-data/2026-11-02-unattended-updates/ordinary-repeated-interruption-scenario-result.json)。该结果验证真实软件恢复和普通入口接线，不证明物理断电时目标存储的同步行为。

## 提交后丢失结果与包校验失败

在独立场景中，外部维护入口包装程序先真实执行新包应用，确认维护退出成功且持久记录 committed 后，保留原结果证据，再仅将交给 C 的 stdout 替换为非法结果。C 返回协议失败并自动调用内部恢复；组件根据可靠提交事实保留 1.0.0，没有回滚新部署。数据库及原文件仍相同，新 run 自动启动并正常退出 0，同一 host 随后提交成功并 quit 退出 0。丢失结果的实际文本进入 processing 诊断。见[场景结果](verification-data/2026-11-02-unattended-updates/ordinary-committed-result-loss-scenario-result.json)及[运行前比对](verification-data/2026-11-02-unattended-updates/ordinary-committed-result-loss-commit-loss-before-run.json)。

另一个新环境接收格式无效的更新 ZIP。组件报告包解析错误，没有发布事务修改意图，原版本仍为 0.9.0，逻辑数据库及比对对象均未改变。原版本自动运行，后续提交及正常退出成功，BadZipFile 原始诊断事实直接进入 processing。见[包拒绝结果](verification-data/2026-11-02-unattended-updates/ordinary-invalid-bundle-scenario-result.json)及[原部署比对](verification-data/2026-11-02-unattended-updates/ordinary-invalid-bundle-invalid-before-run.json)。

## 局部验证与评审边界

最终生产实现及检查点修复均由独立代码评审接受，最终容器证据另经只读复核。C 生产库与 host-demo 的 Release 编译通过，私有协议生成检查通过。隔离单元在实际执行前逐项审核导入、替身、编译选项和可达调用；C 首先通过五个受影响文件，停止 36 项、诊断 2 项及三个维护结果/演示入口退出 0。归档监督增量在其独立固定提交中通过停止 38 项及维护入口，两个统计覆盖重叠，不相加当成独立场景总数。

Python 按文件或指定节点独立运行，禁用自动插件、字节码和缓存。最终受影响文件包括 engine 36 项、router 17 项、force snapshot 7 项、force record 3 项、publisher 3 项、log registry 2 项、metadata/helper 13 项、布局初始化 2 项。归档 helper 在独立接受的固定提交中通过 12 项；启动器、配置检查、新库准备、检查点 busy 拒绝及同库覆盖拒绝另有窄节点证据。它们证明局部契约，不替代上述真实容器场景。各次工具执行命令及退出结果在会话保留，部分没有单独重定向日志，不能声称所有单元均有独立日志文件。

一次 C 结果单元的 Release 构建实际调用 `__fprintf_chk`，越过预期替身并写入真实 stderr，退出 134。这次隔离批准不成立，结果不计作通过。补充真实链接符号包装并独立复核后，该目标重新运行通过；失败工具输出留在 `/tmp/camctl-unattended-unit-build-20261102/test_update_result_isolated-failure.txt`，未重建工具未保留的原始二进制字节。

两个原 bootstrap 节点在审核时发现顶层导入读取真实 `_resources`，未获批准且没有执行。其维护布局契约已改用导入前隔离的独立文件验证。其他中间单元失败包括缺少真实枚举字段、断言误选第二次文件打开等替身问题，保留失败事实后修正并重验，没有删除有效分区或放宽生产校验。

## 强制重建与诊断归档的实际结果

完整强制链路、归档监督和 Python 生产者使用 root `af199e70` 的源码组合，来自独立评审接受的 `12057abe`。第一次强制尝试真实发现新空库创建后留下 0 字节 WAL 和 32768 字节 SHM，发布入口按原规则拒绝，组件完成回滚。该失败没有被当成成功，见[原始失败及恢复](verification-data/2026-11-02-unattended-updates/force-success-first-force-attempt-failure.json)。

修复 `79922796` 集成为 root `131091b6`：同一 RW 连接完成新库完整性及绑定核验，再执行并检查 WAL TRUNCATE 检查点，关闭连接并同步主库和父目录后返回。没有直接删除 WAL，也没有放宽调用方的残留文件拒绝规则。之后实际构建的各强制包均来自 `131091b6`；C 生产代码未因这笔 Python 修复改变。

| 场景 | 实际结果与证据 |
| --- | --- |
| 全新部署与旧环境归档 | 新配置逐字节匹配；新库实例身份改变、计划和动作均为零；旧额外环境文件、媒体及 ready 内容移除；processing 原文件保留；新 run 成功。压缩包包含旧配置、原库、环境、冻结日志与清单，排除 staging/ready 正文及 processing 内容。首份压缩包 6181661 字节、1093 个成员，完成后完整备份和恢复命名空间释放。见[运行前检查](verification-data/2026-11-02-unattended-updates/force-success-v2-force-before-run.json)。 |
| 强制安装失败回滚 | 真实目标环境安装及依赖检查完成后注入失败。恢复配置、数据库、WAL/SHM 的内容、权限、所有者及修改时间，环境标记、媒体、ready 和 processing 均保留；原 WAL/SHM 刻意为不可解析字节，恢复不要求解析它们。没有生成成功归档。见[原数据比对](verification-data/2026-11-02-unattended-updates/force-failure-force-rollback-before-run.json)。 |
| 强制更新及回滚重复中断 | 修改意图后终止更新；普通启动进入恢复，旧环境原子发布后再次终止；第二次普通启动完整恢复冻结对象的文件内容、链接及权限，旧 run 和后续 submit 成功。见[第一次中断](verification-data/2026-11-02-unattended-updates/force-repeated-interruption-interruption.json)、[恢复中断](verification-data/2026-11-02-unattended-updates/force-repeated-interruption-recovery-interruption.json)及[恢复比对](verification-data/2026-11-02-unattended-updates/force-repeated-interruption-force-recovery-before-run.json)。 |
| 旧配置、库及包不能使用 | 发布维护绑定后，刻意破坏旧配置 TOML、数据库字节及旧 CLI 导入。强制更新仍完成新配置、新空库和新环境，并把旧坏配置、旧坏库及坏 CLI 原字节放入归档。见[结果](verification-data/2026-11-02-unattended-updates/force-invalid-originals-force-invalid-before-run.json)。 |
| 原环境、配置及库整体缺失 | 保留稳定维护入口与控制目录，移除旧 venv、配置文件和数据库文件；通过部署共享命令生成的 shell 入口直接进入 C，强制重建成功。归档清单保留原对象缺失事实，没有伪造旧文件正文；新 run 成功。见[结果](verification-data/2026-11-02-unattended-updates/force-missing-originals-missing-originals-before-run.json)。 |
| 错误新配置 | 把绑定另一组路径的强制包交给已有新实例，前置校验报告布局不符，未保存本次修改意图，原配置和上一可靠提交保留，普通运行恢复。见[前置拒绝](verification-data/2026-11-02-unattended-updates/force-missing-originals-wrong-config-result.json)。 |
| processing 在更新期间变化 | 真实安装暂停期间，主程序模拟方删除一个旧待传文件并增加新文件，再触发更新失败。回滚不重放已删文件、不覆盖新文件，原 camctl 工作文件恢复。见[主程序变化](verification-data/2026-11-02-unattended-updates/force-failure-processing-changes-during-update.json)及[保留检查](verification-data/2026-11-02-unattended-updates/force-failure-processing-preservation-result.json)。 |

强制失败回滚只恢复原状；刻意破坏的旧文件恢复后，不能宣称旧业务已经变健康。该分区在运行门闩之前完成字节核对；可正常运行的重复中断场景另有旧 run 和 submit 成功见证。

### 归档与清理中断

在同一独立部署后续强制更新中，外部门闩在压缩文件真正改名完成、归档任务尚未更新阶段时暂停。只终止归档进程组，普通主程序和新 run 保持运行，软件提交仍为 committed。普通主程序正常退出后，通过生成的普通 shell 入口重新启动；同一事务归档完成、摘要核对通过、完整备份释放。见[中断](verification-data/2026-11-02-unattended-updates/force-success-v2-archive-interruption.json)、[业务继续](verification-data/2026-11-02-unattended-updates/force-success-v2-archive-interruption-business-continued.json)及[续做](verification-data/2026-11-02-unattended-updates/force-success-v2-archive-resume-result.json)。

再一次强制更新中，压缩包已经可靠发布且任务为 complete，展开备份已经删除、恢复命名空间尚未清理完时终止归档进程组。下一次普通启动完成清理，保留原已发布归档，任务变为 cleaned，新部署正常运行。见[清理中断](verification-data/2026-11-02-unattended-updates/force-success-v2-cleanup-interruption.json)和[清理续做](verification-data/2026-11-02-unattended-updates/force-success-v2-cleanup-resume-result.json)。这些重复使用独立验证实例的场景各自保留独立事务标识和原始日志，不能把不同运行周期的 PID 混为同一次运行。

## 修改意图已发布、目录同步失败

最终 Python 候选使用外部系统调用包装，在真实修改意图 rename 成功后，使一次更新目录 fsync 返回 EIO。组件从持久记录确认合法的 updating 提案，立即完成回滚并安排原版本运行，原错误阶段 `save_install_intent` 进入 processing 诊断。见[检查结果](verification-data/2026-11-02-unattended-updates/ordinary-intent-sync-failure-intent-sync-before-run.json)及[正常退出](verification-data/2026-11-02-unattended-updates/ordinary-intent-sync-failure-scenario-result.json)。

外部注入器第一次只读取 JSON 开头 64 KiB，未扫描到大环境清单之后的 state 字段，因此没有注入故障。该尝试只得到普通成功，不计作故障恢复通过；事实见[未命中注入记录](verification-data/2026-11-02-unattended-updates/ordinary-intent-sync-failure-injection-missed-first-attempt.json)。修正注入器后，实际 marker、原错误收据和 rolled_back 记录共同确认上述故障确实发生。

## 最终源码与进程收场

[交付物身份清单](verification-data/2026-11-02-unattended-updates/package-identities.json)保存每个实际更新 ZIP、wheel、源码提交、工作树状态及摘要，明确普通初版、首次强制失败候选与检查点修复候选的区别；这些包面向独立验证目录，不作为现场通用强制配置包。[C 构建身份](verification-data/2026-11-02-unattended-updates/host-finalize-build.json)记录真实二进制、静态库摘要和日志。

本次所有已登记验证 host、camctl、维护路由及冻结引擎进程均已结束，见[进程核对](verification-data/2026-11-02-unattended-updates/owned-processes-at-end.json)。已有其他部署和设备未被操作。验证目录、所有实际命令、构建收据、故障注入工具及失败日志保留在隔离 tmp 目录。

## 文档与交付模板

实际调用打包入口的配置文档导出函数，导出了三处模板链接目标，三个目标均存在；公开头文件没有私有协议头依赖。随包 C 示例以 `-std=c11 -Wall -Wextra -Werror` 静态编译通过。更新教程的 13 段 shell 命令通过 `bash -n` 语法检查，没有因此执行教程中的部署或更新命令。见[导出及编译收据](verification-data/2026-11-02-unattended-updates/documentation-receipt.json)及[语法收据](verification-data/2026-11-02-unattended-updates/documentation-guide-shell-syntax.json)。

通用仓库文档检查仍对源模板中尚未生成的三处 docs 链接报错；实际导出后的链接核对通过，未修改或放宽通用检查器。本项不是完整部署包构建、安装或客户端运行验收。

## 验证范围

本计划要求的隔离容器升级、失败回滚、重复中断恢复、强制重建、诊断归档及清理续做已有上述实际证据，见[15 项实际场景汇总](verification-data/2026-11-02-unattended-updates/final-summary.json)。没有新增或运行集成测试套件。当前 V9 没有后续迁移步骤，未把格式核验称为新版本数据库迁移验收；未实际运行 ARM64、第三方主程序、真实相机或目标存储物理断电。容器强杀及受控系统调用故障不能替代目标硬件同步行为的验收。

未实际执行 client_data 能力同步、所有符号链接或硬链接组合等参数矩阵；这些边界的静态和隔离单元结论不能扩大为真实部署矩阵全验。
