# 版本发布与现场更新操作

本页供发布人员、现场操作者和 C 主程序接入人员执行版本更新。版本号及数据库规则以[组件版本与软件更新](../architecture/versioning-and-updates.md)为准，维护接口及失败语义以[更新接入规格](../architecture/software-update-interface.md)为准。

## 1. 先确定要更新的对象

| 目标 | 交付物与更新方式 |
| --- | --- |
| 更新已有主机的 Python camctl 及运行依赖 | 构建 `camctl-update.zip`，通过 `camctl_host_update` 或 host-demo 应用。 |
| 首次安装主机及准备客户端 | 构建 `camctl-deploy.zip`，按包内 README 安装到新目录。 |
| 更新编入第三方主程序的 C 接入模块 | 在匹配目标架构的环境编译新模块，重新编译、链接并部署第三方主程序。 |
| 更新个人电脑上的客户端 | 停止客户端服务，保留整个数据目录，部署新客户端程序并重新启动。 |

Python 专用包不替换 C 静态库或客户端。完整包的 `install-host.py` 要求目标目录不存在，用于首次安装；已有实例通过维护入口原地更新。普通包保留配置、状态库和媒体；强制包按下文约定完整备份后重建。软件版本号、数据库格式及协议格式分别管理，不能通过修改数据库格式号来配合软件版本。

## 2. 发布人员调整版本号

从[版本来源表](../architecture/versioning-and-updates.md#版本来源与发布)定位发生变化的组件，只修改对应权威来源。随后同步其锁文件中的项目元数据。三个组件独立编号，不要求每次一起递增。

Python camctl：修改 `apps/camctl/pyproject.toml` 的 `project.version` 后，在仓库根目录执行：

```sh
uv lock --project apps/camctl --python 3.11
uv lock --locked --project apps/camctl --python 3.11
```

只改发行版本时，检查 `uv.lock` 的变化限于项目自身版本；依赖升级按其实际变更另外核对。已有可用离线缓存时可以附加 `--offline`，缺少缓存应明确处理构建环境。

客户端：在 `apps/client` 执行 npm 的版本命令，让 `package.json` 和 `package-lock.json` 一起更新。下面的 `1.0.1` 仅为新发行版本示例，替换为本次确定的版本：

```sh
npm version 1.0.1 --no-git-tag-version --ignore-scripts
```

C 接入模块：修改 `apps/host-demo/src/host_version.h` 的唯一版本定义。CMake、CPack 和启动日志从该定义读取，不在构建文件另填一份版本号。

发布前检查差异、锁文件一致性及受影响组件的验证结果，再保存发布源码身份。同一已经交付的版本不重新发布不同代码。构建脚本记录提交、工作树状态与摘要；版本查询读取安装元数据或编入程序的版本，修改源码不会更新已经安装的程序。

## 3. 构建 Python 专用更新包

构建机使用 Linux、Python 3.11、uv、Git，并能取得构建工具和锁定依赖。以下命令从仓库根目录执行，`release_output` 必须是尚不存在的目录；仓库内输出限于 `tmp/`：

```sh
release_output="$(pwd)/tmp/camctl-update-release-new"
python3.11 apps/camctl/scripts/build_update.py \
  --repo "$(pwd)" \
  --output "$release_output"
```

构建结束须退出 0，`build-receipt.json` 的 `status` 为 `complete`。输出包括：

| 输出 | 用途 |
| --- | --- |
| `camctl-update.zip`、`camctl-update.zip.sha256` | 交给现场的更新包和包外摘要。 |
| `camctl-update/` | 与 ZIP 同结构的交付目录，可直接用于维护调用。 |
| `build.log`、`build-receipt.json` | 构建步骤、失败位置及最终状态。 |
| `source/`、`build-venv/` | 冻结源码与构建环境，留在构建机用于排查。 |

ZIP 内只有 `camctl-update/` 顶层目录，包含 `RELEASE.json`、源码摘要清单、`SHA256SUMS`，以及 `camctl/` 下的 wheel、锁定依赖和双架构离线 wheel。`RELEASE.json.components.camctl` 是该交付物的 Python 版本；现场以收到的发行记录为准。

普通包的 `RELEASE.json.update.force` 为 `false`。需要丢弃原业务状态并重新部署时，提供完整新配置构建强制包：

```sh
python3.11 apps/camctl/scripts/build_update.py \
  --repo "$(pwd)" \
  --output "$(pwd)/tmp/camctl-force-update-new" \
  --force --config /absolute/path/to/new-config.toml
```

构建器将新 TOML 正文和强制标志一起写入经过摘要校验的 `RELEASE.json`。配置面向该部署，状态库及 `staging`、`ready`、`processing` 必须继续使用原部署路径；设备和其他业务配置可以整体替换。普通包不能传 `--config`，避免把普通更新误当成配置替换。

强制包清空重建 Python 环境、配置、状态库及其伴随文件、staging 和 ready，先完整备份，失败自动恢复原内容。processing 中的待传文件保留；回滚不重新放回已被主程序传输并删除的文件。强制提交后，组件非阻塞将旧环境整理为 `camctl-force-diagnostic-<事务标识>.tar.gz` 并直接发布到 processing，照片、视频和工作副本只保留清单。归档中断由下次普通启动继续；归档可靠发布后释放展开备份，不撤销已提交部署。日志、恢复材料、host、客户端数据库及相机存储继续保留。强制模式可以处理旧库无法解析、旧环境不能导入或旧配置正文损坏的情况，定位依据是首次接入时保存的部署绑定。详见[强制重建](../architecture/versioning-and-updates.md#强制重建)。

构建失败时保留输出及日志，修正原因后换一个新目录重试。构建成功说明交付物已经生成和完成构建入口的检查，不代表真实升级、回滚及断电恢复已经验收。

若要交付首次安装的完整包，使用[完整部署包入口](../../.agents/skills/camctl-real-device-deploy/references/packaging.md)：

```sh
python3.11 .agents/skills/camctl-real-device-deploy/scripts/package.py \
  --repo "$(pwd)" \
  --output "$(pwd)/tmp/camctl-deploy-release-new"
```

## 4. 现场接收与停机准备

将更新 ZIP 及 `.sha256` 放到原虚拟环境、状态库、媒体及交接目录之外的独立发布目录。在 Linux 接收目录执行：

```sh
sha256sum -c camctl-update.zip.sha256
```

摘要检查成功后保留两个文件，不改名或修改 ZIP 内容。组件应用时还会核对包内完整集合和摘要。主程序可以直接传 ZIP，无须自行实现解包。

确认更新使用原实例的运行账户、普通入口和配置来源。后面的例子使用：

```sh
deploy_root=/absolute/path/to/existing-instance
update_zip=/absolute/path/to/releases/camctl-update.zip
```

现场须已提供 uv，以及与原环境配套、位于该虚拟环境之外的基础 CPython 3.11。稳定入口绑定基础解释器，更新和恢复时该解释器必须持续可用。不要把目标环境的 `venv/bin/python` 当作维护基础解释器。

C 更新接口会停止本模块接收新工作，并等待其进程、通知、回调和在途调用收场。其他独立 host、CLI 及操作相同实例的进程仍由操作者停止。用 host-demo 维护时先停止原主程序并确认退出；仅关闭终端界面或停止提交新计划不足以证明写入已经结束。

正常实例可在停机后记录其 `venv/bin/camctl --version` 和当前配置、路径及部署收据。已经存在中断更新或普通入口损坏时，使用带稳定维护入口的普通主程序启动，组件先自动恢复，不要求原 CLI 先能运行。更新器不连接相机执行业务；内部恢复普通运行后，camctl 继续正常业务。

## 5. 准备稳定维护入口

完整包首次安装成功后，安装器已建立 `<target>/maintenance/camctl-updater`，安装收据记录 `updater_path`，生成的普通启动命令带 `--updater`。已有当前维护入口的实例直接正常启动并调用更新；存在未完成记录时由启动流程自动恢复，不重新发布入口。

旧实例首次接入时，主程序须先链接支持当前维护接口的 C 模块。操作者保持停机，从新 wheel 发布维护入口，再执行 Python 更新；发布入口不会应用新版本。

从专用 ZIP 解压到一个新的独立目录，找到其中实际的 `camctl/camctl-<version>-py3-none-any.whl`。下面变量均填写绝对路径；示例 wheel 文件名须换成实际交付名称：

```sh
base_python=/absolute/path/to/base-python3.11
new_wheel=/absolute/path/to/unpacked/camctl-update/camctl/camctl-1.0.1-py3-none-any.whl
"$base_python" -B -I -c \
  'import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module("camctl.update_engine.publisher", run_name="__main__")' \
  "$new_wheel" --wheel "$new_wheel" --target "$deploy_root" \
  --base-python "$base_python" --camctl-path "$deploy_root/venv/bin/camctl" \
  --config "$deploy_root/config.toml"
```

基础解释器须为虚拟环境外的 CPython 3.11。发布器核对普通入口、目标、原配置和目录归属，先可靠保存材料，最后发布可执行入口；退出 0 后保存其输出路径。失败时查看 stderr，不先覆盖原 Python 包来取得更新器。

上述例子使用显式配置。原普通入口使用其他配置时填写其实际路径；原普通入口使用默认来源时省略 `--config`。两种来源不能混用。普通 CLI 允许配置文件缺失时采用默认值，维护也保存这个缺失事实；因此操作者必须确认路径，不能以没有报文件缺失来证明选对了配置。

## 6. 使用 host-demo 更新并继续运行

先停止原主程序，避免两个主程序同时管理同一部署。使用支持当前接口的 host-demo：

```sh
"$deploy_root/host/bin/host-demo" \
  --camctl "$deploy_root/venv/bin/camctl" \
  --updater "$deploy_root/maintenance/camctl-updater" \
  --ready "$deploy_root/ready" --processing "$deploy_root/processing" \
  --log "$deploy_root/host.log" --config "$deploy_root/config.toml" \
  --apply-update "$update_zip"
```

例子采用部署根目录内的数据路径；若首次部署指定了外部 ready、processing 或日志路径，填写实际值。默认配置来源时省略 `--config`。演示程序正常初始化后调用更新接口，完成后继续运行，可继续提交计划、领取文件或接收下一次更新。

运行中的终端也可输入：

```text
update /absolute/path/to/releases/camctl-update.zip
```

终端命令中包路径不加引号、不展开环境变量，整段剩余文本作为路径。调用失败时查看 processing 内的新 diagnostic；组件根据实际恢复结果自动继续原部署。演示不会要求操作者解释维护状态或选择恢复动作。

需要同步客户端能力说明时，启动命令加 `--client-data /absolute/path/to/client-data`。本次更新只覆盖其中的 `device-capabilities.json`，原文件或原本不存在的事实纳入恢复，客户端数据库和媒体保留。省略时新能力说明只留在更新证据目录，随后可交给客户端。

## 7. C 主程序的最小调用

正常初始化时设置 `camctl_host_config.updater_path`。该路径由部署入口提供，并与普通入口及配置来源对应；初始化在启动原虚拟环境程序前先自动恢复未完成更新。

成功初始化后的普通线程只需调用：

```c
#include "camctl_host.h"
#include <errno.h>
#include <stdio.h>

int update_camctl(const char *update_zip) {
    int result = camctl_host_update(update_zip, NULL);
    if (result != 0) {
        int saved_errno = errno;
        fprintf(stderr, "更新未成功：errno=%d；详情见 processing 诊断。\n",
                saved_errno);
        errno = saved_errno;
    }
    return result;
}
```

返回 `0` 表示新包可靠提交、普通资源重建且首次 `run` 已安排；返回 `-1` 表示本次更新流程失败。停止、安装失败回滚、结果未知时的内部恢复、普通工作重新运行均由组件完成。失败后恢复原版本仍返回失败，主程序不用解析 JSON、结果结构或另行启动 camctl。能力同步时以实际客户端 data 绝对路径替换第二个参数的 `NULL`。

原进程的首次计划和未完成内存提交不会被重送；已保存任务由数据库恢复。主程序继续处理 processing 的传输，更新诊断直接位于该目录，无须先调用 claim。

调用须从普通线程发起，不能在信号处理函数、模块工作线程或回调线程中同步等待更新。并发更新会被拒绝。主程序在更新期间明确要求退出时调用正常停止入口；该停止意图优先于更新的自动运行，新包已提交的事实不被撤销。完整返回和并发规则见[主程序接口](../architecture/software-update-interface.md#主程序接口)。

## 8. 更新失败或主机重启后怎样恢复

安装、迁移、发布或最终核验在可靠提交前失败时，组件自动恢复原环境和相应数据，核实完成后重新建立普通运行周期。本次调用仍返回失败，原失败和恢复结果一起进入 diagnostic。

进程强杀或断电后，按正常方式启动主程序即可。例如：

```sh
"$deploy_root/host/bin/host-demo" \
  --camctl "$deploy_root/venv/bin/camctl" \
  --updater "$deploy_root/maintenance/camctl-updater" \
  --ready "$deploy_root/ready" --processing "$deploy_root/processing" \
  --log "$deploy_root/host.log" --config "$deploy_root/config.toml"
```

主程序调用普通 `camctl_host_init`，组件在任何目标环境命令之前恢复未完成事务。回滚途中再次中断时，下次启动继续使用同一冻结引擎和完整备份恢复。已可靠提交的新部署保持，不因旧调用没有收到结果而回滚。

仍有真实写入者持锁或恢复操作尚未完成时，组件保留恢复责任，禁止执行部分安装环境。应保留 `updates/pending.json`、冻结引擎、备份及已有诊断，处理实际占用、空间或权限问题后再正常启动。不要删除记录来绕过启动核验，也不要同时发起另一份更新。

故障范围覆盖更新与回滚的进程中断及主机断电，不包含介质损坏和备份丢失。原部署本来已经存在的数据或配置错误会被回滚保留；需要丢弃这些状态时使用具有完整新配置的强制包。已经可靠更新并产生新业务后，安装旧 wheel 属于另外一次更新，不等于撤销本次更新。

## 9. 查看诊断、日志和数据库处理结果

更新或恢复产生的独立诊断文件直接出现在实际 processing 根目录，包含原调用、错误阶段、原始输出及截断事实、部署结果、后续恢复及普通重建结果。主程序按普通待传文件传输即可。详情包含实际错误内容，不能把本地证据目录路径当成全部诊断。

部署本地 `<target>/updates/<标识>/` 保留 `receipt.json`、`update.log`、冻结引擎、备份和导出能力说明。调用在准备目录创建之前失败时，仍以 C 模块的 processing 诊断或本地错误为依据。诊断发布遇到错误时，组件保留已完成的准备材料或已发布文件，继续必要恢复及业务安排；传输后的删除由主程序负责。

普通自动迁移以 V9 为起点。当前格式核验，后续格式按登记迁移在副本执行并验证后发布；配置和媒体保留。强制重建使用新空库，无需旧库到新库的迁移路径；失败时恢复原文件，即使旧文件原本无法作为合法库打开，也不能把字节恢复称为业务修复。

可以在维护完成并允许普通运行后查询原路径的 `venv/bin/camctl --version`，与包内 `RELEASE.json.components.camctl` 比较。还须检查自动运行日志和业务恢复；版本字符串本身不能证明完整事务或相机恢复成功。

## 10. 部署绑定与验证证据

维护入口记录的是同一部署的数据库及交接目录。强制包整体替换配置内容，但保留这些路径。已登记维护的实例执行同路径重复初始化正常；改变数据库或交接目录的初始化会在写入前被拒绝。该约束使旧配置或旧库损坏时仍能可靠定位待备份和重建对象。

验证分别保留隔离单元、容器实际更新、目标主机和真实设备证据。容器内通过强制终止进程验证恢复，不等同物理断电。当前实施和真实容器验收状态见[无人值守更新计划](../superpowers/plans/2026-11-02-unattended-updates.md)，实际结果进入组件验证记录。

## 11. 更新 C 模块与客户端

C 模块更新须停止旧主程序，在目标系统或匹配的交叉工具链中构建新源码，按[源码构建说明](../../apps/host-demo/README.md#构建与安装)重新链接主程序。公开头文件、静态库和编译主程序所用接口须来自同一发行物。原配置、数据库及媒体不属于源码替换对象，Python 更新器也不替换静态链接进主程序的代码。

下面从已经解压的 C 源码构建到新的程序安装目录；三个目录都填写绝对路径，构建目录事先创建且为空：

```sh
HOST_SOURCE_DIR=/absolute/path/to/unpacked/host-source
HOST_BUILD_DIR=/absolute/path/to/new-host-build
HOST_INSTALL_DIR=/absolute/path/to/new-host-install
cd "$HOST_BUILD_DIR"
cmake "$HOST_SOURCE_DIR" -DCMAKE_BUILD_TYPE=Release -DHOST_BUILD_TESTS=OFF \
  -DCMAKE_INSTALL_PREFIX="$HOST_INSTALL_DIR" -DCMAKE_INSTALL_LIBDIR=lib
cmake --build . --target install -- -j2
```

随后在第三方主程序工程中使用新目录的头文件及静态库重新链接。例如，主程序入口为 `main.c` 时：

```sh
cc -std=c11 main.c -I"$HOST_INSTALL_DIR/include" \
  "$HOST_INSTALL_DIR/lib/libcamctl_host.a" -pthread -o my-main
```

将新主程序和需要的演示程序部署到现场后，按原配置启动并核对模块启动版本。x86_64 构建的静态库不能交给 ARM64 主程序使用。只复制新头文件而不重新链接主程序，不会更新已经编入的模块。

客户端更新前停止后端服务，按[停机备份](../client/running.md#停机备份)保留整个数据目录。将新程序放到新的程序目录，安装其生产依赖，再用原绝对 `CAMCTL_DATA_DIR` 启动。完整部署包中的 `client/start-client.sh` 或 Windows 的 `start-client.ps1` 支持默认及自定义数据目录，具体命令见包内 README。保持旧程序和备份，启动后核对版本日志、实际数据目录、草稿、原请求、报告和媒体。

例如，在 Linux 上使用完整包中已经构建网页的新 `client/`：

```sh
client_release=/absolute/path/to/new-client
export CAMCTL_DATA_DIR=/absolute/path/to/existing-client-data
cd "$client_release/apps/client"
npm ci --omit=dev --engine-strict --no-audit --no-fund
sh "$client_release/start-client.sh"
```

`client_release` 对应复制后的整个 `client/` 目录；生产依赖安装在新程序目录。更新时不创建新的业务数据目录，也不在网页重新初始化已有数据库。普通启动不自动切换原数据位置，必须核对 `CAMCTL_DATA_DIR`。

客户端没有由 Python 更新器提供的自动迁移或回滚保障，C 主程序二进制也不在该保障范围内；分别按其数据格式、部署及备份契约处理。真实更新和故障恢复的实际验收范围见[无人值守更新计划](../superpowers/plans/2026-11-02-unattended-updates.md)及其引用的验证记录。
