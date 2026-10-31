# 组件版本与离线更新验证

## 实现与版本来源

基线为 `13b81d85`。用户选择三个组件独立编号，部署包提供离线更新入口，并在停机后更新原 Python 环境。[组件版本与软件更新](../architecture/versioning-and-updates.md)定义当前行为，[实施计划](../superpowers/plans/2026-10-31-component-versioning-and-offline-update.md)跟踪步骤。

Python camctl 版本为 `0.2.0`，以 `pyproject.toml` 为发行权威，安装后的版本选项和启动记录查询发行物元数据。host 为 `1.1.0`，版本头文件同时供 C 源码和 CMake 使用；客户端为 `0.2.0`，启动记录读取 `package.json`。这些数字是本次验证快照，不作为另一份版本登记。

Python 四条业务命令在日志上下文建立后记录一次版本 INFO，绕过普通过滤和采样；队列满时走 stderr，保持机器 stdout 契约及 INFO 的原持久化要求。元数据读取失败保留原错误并记录版本不可用，不阻挡业务收场。

更新入口使用新解压的完整交付目录，按原配置和状态库取得稳定锁，离线安装后核对实际版本、原库身份及目录绑定。构建脚本准备 Linux ARM64、x86_64 的配套依赖 wheel，记录目标文件清单和下载工具版本。安装器与更新器在导入前禁用字节码写入，避免自身缓存破坏完整摘要集合。已有运行目录、配置和媒体保持各自所有权；更新器不迁移数据库或自动回滚 Python 环境。

## 隔离单元验证

工作目录 `/workspaces/camctl`，Python 3.11.17。命令为：

```bash
env PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  apps/camctl/.venv311/bin/python -m pytest -q -p no:cacheprovider \
  apps/camctl/tests/unit/logging_runtime/test_admission.py \
  apps/camctl/tests/unit/logging_runtime/test_startup_admission.py \
  apps/camctl/tests/unit/logging_runtime/test_startup_version.py \
  .agents/skills/camctl-real-device-deploy/tests/unit/test_update.py \
  .agents/skills/camctl-real-device-deploy/tests/unit/test_bundle_entrypoints.py \
  .agents/skills/camctl-real-device-deploy/tests/unit/test_deployment_inspect.py
```

57 项及 5 个子测试通过，0.22 秒，退出 0，输出保存在 `tmp/component-version-unit-final.log`。日志接纳用例构造真实内存通道但不启动监听线程，版本及更新用例隔离元数据、Schema、文件、进程、配置及数据库协作者。覆盖启动过滤例外、拥塞、故障后备输出、普通 INFO 规则、版本缺失、原配置选择、锁范围、失败收据、离线安装参数及只读检查责任。错误用例还验证异常链和附加事实、释放锁后才交付诊断，以及日志与 stderr 写入或刷新同时失败时仍保留原更新结果并尝试最终收据保存。没有将替身结果作为真实 wheel、SQL 或安装过程通过的证据。

开发期间保留两项范围越界：一次 CLI 测试导入使第三方库读取自身元 Schema 包资源；一次导入缓存反例在独立工作树生成两个 `.pyc` 文件。均已向用户说明，前者通过 Schema 接口替身与资源访问禁止修正，后者删除本次缓存并在切换标志前取得源码代码对象，标志关闭期间不执行普通模块导入。越界执行不计入本次符合约束的验证，随后以修正后的范围取得上述通过结果。未操作业务文件、状态库或相机。

## 静态检查与独立复核

host 与客户端在独立工作树完成 CMake 配置、严格 C 语法、TypeScript 类型及版本元数据一致性检查；未构建静态库或启动服务。集成阶段完成 17 个 Python 文件的 AST、Python 与客户端锁文件版本一致性及差异空白检查。变更文档的本地链接存在；部署包 README 模板中的三项链接由构建脚本导出到包内 `docs/`，源模板目录没有这些生成文件，实际发行物链接核验仍未运行。独立只读评审确认并修正了导入缓存导致完整清单校验拒绝、外置配置接线及错误诊断截断问题。更新器和启动记录均保留实际交付异常，host 在其他工作线程启动前记录版本；最终源码未发现剩余确定阻断。

执行代理在 `/tmp/camctl-version-host-client-20261031` 使用以下命令，均退出 0，输出保存在对应工具会话中：

```bash
cmake -S apps/host-demo -B /tmp/camctl-v3-config-20261031 -DHOST_BUILD_TESTS=OFF
cc -std=c11 -D_GNU_SOURCE -Wall -Wextra -Wpedantic -Werror \
  -Iapps/host-demo/include -Iapps/host-demo/src -Iapps/host-demo/vendor/yyjson \
  -fsyntax-only apps/host-demo/src/host.c
npm run typecheck --prefix apps/client
```

CMake 3.28.3 的生成缓存及 CPack 配置均记录 host 版本 `1.1.0`。类型检查使用 Node 24.22.1、npm 11.21.1，并临时连接已有开发依赖；检查完成后移除工作树中的该链接。根代理未重复这些检查。

## 未运行范围

未新增或运行集成测试套件，未实际下载目标 wheel、构建部署包、安装或更新虚拟环境，也未验证真实数据库组合、最低 CMake、目标 ARM64 或设备。新包中的完整清单、跨架构 wheel 选择及旧发行物 API 组合仍须发行物验证；本次尚未生成可交付的新部署包，先前部署包的版本和内容保持原样。

部署 skill 的完整包验证门槛继续有效，后续生成部署包时不得跳过。Python 环境更新失败后可能已部分改变，旧软件重新安装不撤销数据库迁移；不能由本次单元通过推断自动回滚或现场更新成功。
