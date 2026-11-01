# 完整部署包契约

打包入口为 `scripts/package.py`。构建需要 Linux、Python 3.11、uv、Git、CMake、CPack、C 编译器和满足客户端开发依赖要求的 Node.js / npm。Python 与 npm 应用依赖分别从 `uv.lock`、`package-lock.json` 安装；构建工具版本记录在 RELEASE.json。

## 输入与确定性范围

输入为 `--repo` 指向的 Git 工作树当前文件、可选 `--config` 和尚不存在的 `--output`。脚本先冻结受影响组件、公共协议、camctl 构建资源与技能文件；包含已跟踪文件的当前修改及未忽略的新文件，保留当前删除。输出在仓库内时限于 `tmp/`。已存在的输出目录直接拒绝，旧包和验证环境保留。

同一组输入使用固定的步骤、目录、模板、依赖锁、声明来源及验证门槛。ZIP 条目按路径排序，时间与权限固定。外部构建工具版本和嵌套 CPack 压缩包可能影响字节；这里保证交付结构与操作确定，不承诺不同工具环境构建的 ZIP 摘要相同。源码整体摘要由逐文件摘要计算，不能仅凭 HEAD 判断工作树内容相同。

条件 | 设备声明来源与处理
--- | ---
未传 `--config` | 复用设备发现脚本的占位配置策略，用本次 wheel 的 `describe` 导出声明；RELEASE.json 标为 placeholder。
提供有效配置 | 用本次 wheel 的 `describe` 导出对应设备声明；RELEASE.json 标为 configured，记录配置摘要。配置本身不交付，不改变目标主机现场发现规则。
配置不可读、无效，或 `describe` 失败 | 停止打包，保留日志和失败收据，不生成最终 ZIP。
任何构建或验证步骤失败 | 停止交付，保留输出目录、构建收据和验证证据；修复后使用新输出目录。

## 交付目录

ZIP 内只有一个顶层目录 `camctl-deploy/`：

```text
camctl-deploy/
  README.md
  RELEASE.json
  source-manifest.json
  SHA256SUMS
  install-host.py
  bundle_support.py
  deploy_support.py
  camera_discovery.py
  docs/
    camctl-configuration.md
    host-configuration.md
    camctl_host.h
  camctl/
    camctl-<version>-py3-none-any.whl
    requirements.txt
    wheelhouse/x86_64/*.whl
    wheelhouse/aarch64/*.whl
  host/
    camctl_host-<version>-Source.tar.gz
  client/
    start-client.sh
    start-client.ps1
    data/device-capabilities.json
    apps/client/
      package.json
      package-lock.json
      src/server/、src/shared/、src/domain/
      dist/web/
    protocol/schemas/
    protocol/errors/workflow-codes.json
```

客户端保持实际运行代码使用的协议相对路径，与组件 Docker 运行目录一致。交付构建后的网页和 TypeScript 服务源码，目标电脑只安装生产依赖。构建机器的 node_modules 不进入 ZIP，以支持在目标平台安装。Node.js 版本要求只由组件 package.json 维护。包内 data 仅含能力说明，数据库由用户在网页显式初始化。

README 与两种客户端启动脚本的权威来源在 `assets/bundle/`，逐字复制；源码版本和验证事实统一保存在 RELEASE.json。输出目录的 build.log、build-receipt.json、源快照、虚拟环境和构建目录用于诊断，不进入 ZIP。SHA256SUMS 精确覆盖包内除清单自身外的全部文件；最终 ZIP 的摘要在包外同名 `.sha256` 文件中。

README 说明安装参数与产物、客户端默认及自定义数据目录、camctl 常用配置和日志、host-demo 的使用、自有 C 主程序的链接与调用、两端配置对应关系、声明同步及失败诊断。完整配置参考从 `docs/camctl/configuration-reference.md` 和 `apps/host-demo/README.md` 的配置章节导出，公开头文件从 `apps/host-demo/include/` 导出；不另维护一套完整参数清单。导出时保留正文、内部锚点和外部链接，未交付的仓库链接转为原标签文本。构建验证检查 README 链接目标、C 示例的编译和 TOML 示例的正式加载，并验证客户端自定义数据目录。

## 构建与验收步骤

1. 核对前提，冻结源码并计算逐文件摘要。后续构建只读取快照，避免构建钩子修改原工作树。
2. 导出带哈希的锁定依赖，下载 Linux x86_64 和 ARM64 的 Python 3.11 兼容 wheel，最低目标为 manylinux2014 / glibc 2.17；下载失败停止交付。构建 camctl wheel，在独立 Python 3.11 环境安装并检查依赖，用这个 wheel 按所选配置导出客户端声明。
3. 在独立构建目录调用 CMake 配置，再调用 CPack 生成 host 源码包。版本读取组件构建配置，不另写固定版本。
4. 用 npm ci 安装锁定构建依赖，构建客户端网页，收集运行源码、网页、协议与声明，复制说明及启动模板。
5. 写出源码、版本与校验信息，形成候选 ZIP。在仓库外解压，精确核对文件集合与摘要；使用身份查询替身实际运行安装器，检查状态库完整性、最终配置、启动路径和声明同步。
6. 在解压目录安装客户端生产依赖，从包内启动脚本运行服务。通过 HTTP 检查首页、实际 data 目录和完整声明加载；服务退出后保留日志。验证不初始化或携带客户端数据库。
7. 将实际环境、检查范围和通过结果写入 RELEASE.json，刷新校验清单，生成最终 ZIP 及摘要。失败不能转为缩减范围后的成功交付。

主机安装器支持 `--client-data`，默认写到新实例的 `client-data`；只同步能力说明。该参数不自动推断个人电脑路径，也不覆盖客户端数据库或媒体。配置变更后的声明同步与网页加载继续遵守正式客户端部署契约。

这些验收证明软件部署路径可执行。完整测试套件、最低 CMake 版本、Windows、ARM64 和真实相机操作分别保留证据，不能从当前构建机的安装成功推断。

## 初次安装与已有实例更新

发布版本与已有实例的操作流程见[版本发布与现场更新操作](../../../../docs/camctl/software-updates.md)；交付接收方仍按包内 README 操作，不要求目标机器具有源码仓库。

完整部署包用于初次安装。安装器初始化 camctl 后，调用新 wheel 的发布器建立 `<target>/maintenance/camctl-updater`，将路径记录到收据并传给生成的 host-demo 启动入口。维护实现由组件提供，不在完整部署包顶层维护另一份更新程序。

已有实例使用 `apps/camctl/scripts/build_update.py` 构建的 Python 专用更新包。它与完整包共用 Python 构建和依赖收集能力，交付 `camctl-update.zip`、包外摘要及同结构目录。ZIP 只有 `camctl-update/` 顶层目录，包含 `RELEASE.json`、`source-manifest.json`、`SHA256SUMS` 和 `camctl/` 下的单个 wheel、锁定依赖与双架构离线 wheel；不携带 host 或客户端。`RELEASE.json.update` 包含更新模式；强制包还提供完整新配置，构建时使用 `--force --config`，普通包不接受配置替换。元数据及重建范围以组件更新规格为准。

主程序通过 `camctl_host_update` 直接传入更新 ZIP 的绝对路径及可选能力同步目标。接口使用已初始化实例的配置，组件停止本模块普通业务，先恢复旧事务，再核对当前包并完成备份、迁移或强制重建，失败时恢复原环境。核实允许后自动建立普通运行周期。返回值只表示本次更新成功或失败；诊断直接作为独立文件进入 processing。输入也可为已解压包目录。普通主程序启动自动恢复未完成更新。首次接入旧环境须先从新 wheel 准备稳定入口，并升级主程序所链接的 C 接入模块；Python 更新包不替换静态库。

V9 起点、备份、可靠完成点和故障边界由[更新规格](../../../../docs/architecture/versioning-and-updates.md)定义；稳定入口、部署绑定和公共结果由[更新接入规格](../../../../docs/architecture/software-update-interface.md)定义。随包 README 提供接收方操作说明。现有发行验证不证明真实数据库迁移、中断恢复或目标架构更新；这些真实组合须另行验证。
