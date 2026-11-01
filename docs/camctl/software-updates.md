# 版本发布与现场更新操作

本页供发布人员、现场操作者和 C 主程序接入人员执行版本更新。版本号及数据库规则以[组件版本与软件更新](../architecture/versioning-and-updates.md)为准，维护接口及失败语义以[更新接入规格](../architecture/software-update-interface.md)为准。

## 1. 先确定要更新的对象

| 目标 | 交付物与更新方式 |
| --- | --- |
| 更新已有主机的 Python camctl 及运行依赖 | 构建 `camctl-update.zip`，通过 `camctl_host_update` 或 host-demo 的维护模式应用。 |
| 首次安装主机及准备客户端 | 构建 `camctl-deploy.zip`，按包内 README 安装到新目录。 |
| 更新编入第三方主程序的 C 接入模块 | 在匹配目标架构的环境编译新模块，重新编译、链接并部署第三方主程序。 |
| 更新个人电脑上的客户端 | 停止客户端服务，保留整个数据目录，部署新客户端程序并重新启动。 |

Python 专用包不替换 C 静态库或客户端。完整包的 `install-host.py` 要求目标目录不存在，用于首次安装；已有实例通过维护入口保留原环境路径、状态库和媒体。软件版本号、数据库格式及协议格式分别管理，不能通过修改数据库格式号来配合软件版本。

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

正常实例可在停机后记录其 `venv/bin/camctl --version` 和当前配置、路径及部署收据。已经存在中断更新或普通入口损坏时，先使用下文的恢复入口，不要求原 CLI 能正常运行。更新不改写配置、不切换交接目录，也不连接相机执行业务。

## 5. 准备稳定维护入口

完整包首次安装成功后，安装器已建立 `<target>/maintenance/camctl-updater`，安装收据记录 `updater_path`，生成的普通启动命令带 `--updater`。已有此入口的实例直接进入应用或恢复流程；存在未完成记录时不要重新发布入口。

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

## 6. 使用 host-demo 应用一次更新

在原主程序已停止的情况下执行：

```sh
"$deploy_root/host/bin/host-demo" \
  --camctl "$deploy_root/venv/bin/camctl" \
  --updater "$deploy_root/maintenance/camctl-updater" \
  --config "$deploy_root/config.toml" \
  --apply-update "$update_zip"
```

host-demo 必须是支持当前接口的版本。参数均使用绝对路径；默认配置来源时省略 `--config`。维护模式只调用更新接口，结束后退出，不启动普通业务。

需要同步客户端能力说明时，在应用命令中附加：

```sh
--client-data /absolute/path/to/client-data
```

该目录须是本次要同步的实际目标。更新只覆盖其中的 `device-capabilities.json`，把原内容或原本不存在的事实纳入恢复；客户端数据库与媒体保留。不传该参数时，新能力说明仅留在更新产物目录，再由部署人员交给客户端。

host-demo 退出 0 表示本次维护请求成功且更新责任允许启动；非零须结合终端结论、原因及诊断位置处理。应用失败但回滚已成功时，演示仍以失败退出，不能把它当成新版本安装成功。

## 7. C 主程序调用与结果处理

正常初始化时设置 `camctl_host_config.updater_path`，让模块在启动原虚拟环境程序前检查维护资格。更新时提供同一实例的 `camctl_path`、`updater_path` 和配置来源；模块不从程序路径猜部署根目录，也不解析 TOML 为主程序补路径。

以下示例只申请更新并确认新版本提交，完成后仍保持停机。实际路径由主程序部署参数提供：

```c
#include "camctl_host.h"
#include <errno.h>
#include <stdio.h>

int update_camctl(const char *camctl_path, const char *updater_path,
                  const char *config_path, const char *update_zip) {
    camctl_host_update_config config = {camctl_path, updater_path, config_path};
    camctl_host_update_request request = {
        .action = CAMCTL_UPDATE_ACTION_APPLY,
        .bundle_path = update_zip
    };
    camctl_host_update_result result;
    int received = camctl_host_update(&config, &request, &result);
    int saved_errno = errno;

    printf("%s；%s\n", camctl_update_effect_description(result.effect),
           camctl_update_eligibility_description(result.eligibility));
    if (result.reason[0])
        fprintf(stderr, "原因：%s\n", result.reason);
    if (result.diagnostic_path[0])
        printf("诊断位置：%s\n", result.diagnostic_path);
    if (received != 0) {
        errno = saved_errno;
        return -1;
    }
    return result.outcome == CAMCTL_UPDATE_OUTCOME_APPLIED &&
           result.effect == CAMCTL_UPDATE_EFFECT_COMMITTED &&
           result.eligibility == CAMCTL_UPDATE_ELIGIBILITY_ALLOWED ? 0 : 1;
}
```

`camctl_host_update` 自身返回 `0` 只表示收到可信结果，本示例再检查三项结论，才把新版本提交认定为成功。公开结果另外提供简短原因和实际诊断位置；不按原因文案进行机器分支。底层原始输出和进程错误由模块统一记录到 stderr，主程序无须重建这些诊断。

| 收到的事实示例 | 主程序下一步 |
| --- | --- |
| `applied`、`committed`、`allowed` | 新版本已可靠提交，主程序可以决定恢复业务。 |
| `failed`、`rolled_back`、`allowed` | 本次更新失败，原部署已核实恢复；按原版本恢复业务的决定由主程序作出。 |
| `failed`、`unchanged`、`allowed` | 本次没有修改部署，检查包或配置错误后决定恢复原业务或另行更新。 |
| 资格为 `blocked` 或 `unknown`，或结果不可信 | 保持停机，依据原因和日志处理占用或恢复责任。 |

完整状态与返回规则见[更新接口规格](../architecture/software-update-interface.md#机器结果与诊断)。所有调用从普通线程发起，不能在信号处理函数或会等待自身结束的模块线程中维护。

## 8. 显式恢复普通业务

主程序核对结果并决定继续业务后，在同一进程显式调用 `camctl_host_restart()`。它复用已经停止实例保存的配置与回调，先检查维护资格，再建立新运行周期；不重送旧首次计划。

只有曾成功初始化并已可靠停止的普通实例才能重启。在从未初始化的独立维护进程中调用会返回 `ENODEV`；这种场景应退出维护程序，再按原启动入口启动主程序。host-demo 维护模式属于后一种场景。

重启返回 0 只表示本地资源和首次 `run` 已安排，仍须观察普通启动日志、设备检查和业务报告。不能用停止后再次调用 `camctl_host_init` 替代显式重启。更新器也不会自动启动主程序。

## 9. 失败、断电和强制终止后的恢复

安装、迁移、发布或最终核验在可靠完成前失败时，组件按正式事务记录自动尝试恢复原环境、数据库及适用能力文件，并核实恢复结果。不能核实时保留责任和停机状态。断电或进程被强制结束后，下一次维护先处理旧事务，使用其冻结引擎；当前新包只有在旧恢复完成后才会处理。

只恢复未完成事务时，不需要提供新更新包：

```sh
"$deploy_root/host/bin/host-demo" \
  --camctl "$deploy_root/venv/bin/camctl" \
  --updater "$deploy_root/maintenance/camctl-updater" \
  --config "$deploy_root/config.toml" \
  --recover
```

C 主程序使用 `CAMCTL_UPDATE_ACTION_RECOVER`，并将 `bundle_path`、`client_data_path` 设为 `NULL`。恢复采用旧事务冻结的配置和能力文件绑定，不能通过本次参数改成另一套恢复目标。只恢复成功后仍保持停机。

资格占用表示仍有维护者或残留写入者，不能直接推断必须回滚。等待原写入者结束并核实原因，再调用维护；不要并发重试整次更新。恢复失败或结果未知时保留目录、存储及权限错误事实，修正原因后再次 `--recover`。不要删除 `updates/pending.json`、备份、冻结引擎或临时恢复材料来绕过资格检查。

自动恢复覆盖本次未完成事务。更新已经可靠提交并产生新业务后，安装旧 wheel 不等于合法撤销；数据库没有登记降级路径时，不能只切回旧软件。

## 10. 查看日志、收据及数据库处理结果

`result.diagnostic_path` 或 host-demo 显示的位置通常位于 `<target>/updates/<标识>/`，其中保留 `receipt.json`、`update.log`、备份和导出的 `device-capabilities.json`。入口在生成该目录前失败时，诊断位置可以为空，应查看主程序 stderr。C 的简短原因可能缩短，完整已捕获事实仍由模块诊断交付。

状态库自动维护以 V9 为起点。当前格式只验证，后续格式按发行物登记的迁移路径在副本上迁移，核验后才发布。原环境、数据库和适用能力文件一起恢复；普通 `run`、`submit` 或重新初始化空库不能替代更新恢复。

成功后，在允许普通启动的前提下查询原路径的 `venv/bin/camctl --version`，与本次 `RELEASE.json.components.camctl` 比较，并检查普通启动及业务恢复。版本字符串单独不能证明迁移、回滚或设备恢复成功。

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

客户端没有由 Python 更新器提供的自动迁移或回滚保障，C 主程序二进制也不在该保障范围内；分别按其数据格式、部署及备份契约处理。真实更新和故障恢复的实际验收范围见[组件更新验证记录](owned-update-verification-2026-11-02.md)及[精简 C 结果验证](../host-demo/verification.md#公开更新结果精简2026-11-02)。
