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
  update-camctl.py
  update-worker.py
  update_transaction.py
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

README 说明安装参数与产物、客户端默认及自定义数据目录、camctl 常用配置和日志、host-demo 的使用、自有 C 主程序的链接与调用、两端配置对应关系、声明同步及失败诊断。完整配置参考从 `docs/camctl/configuration-reference.md` 和 `apps/host-demo/README.md` 的配置章节导出，公开头文件直接复制；不另维护一套完整参数清单。导出时保留正文、内部锚点和外部链接，未交付的仓库链接转为原标签文本。构建验证检查 README 链接目标、C 示例的编译和 TOML 示例的正式加载，并验证客户端自定义数据目录。

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

## 停机离线更新

包内 `update-camctl.py` 更新已有实例的原虚拟环境。操作者先停止 host 和全部 CLI，准备 Python 3.11、uv 并保留旧包，再将完整新包解压到新的空目录；更新所用的包目录须未安装客户端依赖、未运行客户端，原客户端数据和旧包保留在原位置。默认使用 `<target>/config.toml`，可通过 `--config` 指定原配置文件的绝对路径。

更新器先恢复未完成更新，再校验本次完整摘要与发行记录，并持有同一环境的更新锁及原状态库稳定会话锁、接纳锁。可靠备份原环境和一致数据库后，以带哈希的锁定依赖及本包 wheel 离线安装。新发行物在独立副本上按登记路径迁移数据库，验证实例、目录绑定和业务数据后发布。自动维护从 V9 开始，当前 V9 只验证。每次执行保留日志、收据、原安装信息及恢复材料。

提交前失败时自动回滚环境与数据库；断电或强制终止后，下次调用先完成恢复。恢复不能核实时保留未完成责任并拒绝新更新。恢复入口须独立于被修改的环境和 Python 包，不能要求先人工修好原虚拟环境。可靠完成点和故障边界由[更新规格](../../../../docs/architecture/versioning-and-updates.md)定义。只有明确指定 `--client-data` 时才同步能力说明，并将该文件纳入回滚；客户端数据库和媒体保留。更新结束保持停机。

现有发行验证不证明更新入口、跨架构依赖安装、真实数据库迁移或中断恢复；这些真实组合须另行验证。
