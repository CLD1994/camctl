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

## 局部验证与评审边界

普通生命周期和诊断的固定源码已由独立评审接受。批准范围内的五个 C 文件分别运行通过，其中停止入口 36 项、诊断格式与发布边界 2 项；其余三个入口退出 0。Python 的元数据、普通更新控制、路由、布局、发布及初始化局部入口采用逐文件或逐节点独立进程，并在导入前隔离真实协作者；实际数目以各阶段收据及最终汇总为准。

一次 C 结果单元的 Release 构建实际调用 `__fprintf_chk`，越过预期替身并写入真实 stderr，退出 134。这次隔离批准不成立，结果不计作通过。补充真实链接符号包装并独立复核后，该目标重新运行通过；失败工具输出留在 `/tmp/camctl-unattended-unit-build-20261102/test_update_result_isolated-failure.txt`，未重建工具未保留的原始二进制字节。

两个原 bootstrap 节点在审核时发现顶层导入读取真实 `_resources`，未获批准且没有执行。其维护布局契约已改用导入前隔离的独立文件验证。

## 尚未取得的证据

包校验拒绝、提交后丢失结果、强制重建、旧环境归档与清理中断仍待实际执行。真实 ARM64、第三方主程序、真实设备和目标存储物理断电没有由此次容器结果证明。
