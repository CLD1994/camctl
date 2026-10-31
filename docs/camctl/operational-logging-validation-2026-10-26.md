# 关键阶段与命令日志验证（2026-10-26）

行为由[日志规格](../superpowers/specs/2026-10-26-operational-logging.md)定义，进度见[实施计划](../superpowers/plans/2026-10-26-operational-logging.md)。关键业务阶段及所有实际外部命令使用 INFO，纯耗时明细使用 DEBUG，记录采用统一 JSON 元数据及所属业务身份。`init`、`describe` 只沿原 stderr 诊断通道输出，不装配运行会话。

## 软件验证

最终组合来源为 `685c5280f21eda64ebbd39473d2e300ea1e4097d`。环境为 Linux 开发容器、Python 3.11.17、SQLite 3.53.1，工作目录为 `/workspaces/camctl`，解释器为 `apps/camctl/.venv311/bin/python`。各命令显式设置本工作区的源码和测试搜索路径，并移除安装子进程不应继承的计时及 Python 环境变量。集成目录分别启动 pytest，在同一工作区顺序运行。

全量单元使用 `python -m pytest apps/camctl/tests/unit -q --tb=short`，9565 项通过、1 项跳过，退出 0。两条既有警告来自非异步用例的 asyncio 标记。

以下定向集成均以 `python -m pytest <范围> -q --tb=short` 执行，共 195 项通过，所有进程退出 0。

| 范围，均位于 `apps/camctl/tests/integration/` | 实际结果 |
| --- | --- |
| `logging_runtime` | 147 项通过；一条既有 asyncio 标记警告。 |
| `operations/test_process.py`、`operations/test_process_events.py` | 9 项通过。 |
| `devices/test_command_events.py` | 3 项通过。 |
| `host_files/test_command_events.py`、`host_files/test_tasks.py`、`host_files/test_task_events.py` | 14 项通过。 |
| `bootstrap/test_logging_composition.py`、`bootstrap/test_one_shot_logging.py`、`bootstrap/test_cli_error_diagnostics.py` | 18 项通过。 |
| `capture/test_global_error_diagnostics.py`、`capture/test_recording_start_runtime.py::test_actual_start_info_preserves_anchor_and_wait_does_not_repeat` | 4 项通过。 |

这些组合验证真实队列、日志线程、文件或 stderr、受管子进程、文件工作线程、SQLite 和 CLI 的协作。观测覆盖 INFO/DEBUG 过滤、命令实际起止、启动失败及取消、身份隔离、原 ERROR 异常图、必要停止与关闭、拍摄时钟锚点、空轮询及保存重送去重。扫描缓存关闭失败保留实际分类，命令业务结果不预报日志关闭是否成功。独立审查已核对核心及跨组件接缝，发现的问题均有修复和反例验证。

原命令、UTC、源码身份、执行结果及日志保存在 `.local/verification/operational-logging/final-complete/`，各域定向检查及原始失败归档于同级 `agents/`。最终组合没有覆盖全部组件集成目录，也没有执行新的相机拍摄。

## 扩大检查中的既有失配

扩大检查保留以下失败，不计入通过结果：

| 实际失败 | 归因依据与验证边界 |
| --- | --- |
| 一项 ADB 元数据测试期待旧帧版本 `/1`，实际脚本为 `/2`。 | 在实现前基线 `0a28fb49` 独立复现；本次没有修改元数据协议。 |
| 旧拍摄集成文件中的 83 项用例向 `CaptureRuntime` 传入 `owned`，在日志路径执行前拒绝装配。 | 本次没有改变该运行时构造契约；另用当前 `BusinessDatabase` 装配验证实际拍摄入口。旧夹具未迁移，不宣称这些用例通过。 |
| 两个初始化夹具向 `accept_input` 交付旧连接类型，另一个报告配置故障用例的设备流程预期不符。 | 在不含本次 CLI 修改的独立基线 `0f037676` 复现相同三项失败；实际 CLI 日志及诊断组合另行通过。 |

## 验证安装

在 `/home/cld/.camctl/manual-validation-20261026` 从上述固定提交归档并离线构建、重装 camctl。wheel 的 SHA-256 为 `51abb56a9edafdeeccb2a6df8229aebb8245581c920121d417e92bf239a70e7c`。335 份包内 Python 文件、27 项权威资源在归档、wheel 和安装中逐字节一致；依赖检查通过，依赖版本前后一致。

5912 个受保护文件的内容、大小和模式，以及受保护目录和链接，安装前后核对一致；范围包含配置、业务数据、已有日志、host 和客户端。原配置已为 INFO，未修改。旧 wheel、配置及状态库备份保存在本次交付目录的 `backup`。

安装版隔离检查验证事件元数据、INFO/DEBUG 级别、原异常传播及纯文本 JSON 外层。真实安装版 `describe --config <原配置>` 退出 0，stderr 输出 `describe` 的 INFO 开始和返回 JSON，stdout 仍为一份能力文档；没有执行业务数据库操作或设备命令。安装结束时 host、camctl、客户端均未运行，没有沿用历史 PID。

完整收据、安装字节清单、保护文件清单和命令日志位于 `.local/deployments/20261026-operational-logging/`。运行目录的 `delivery-manifest.json` 已指向该次索引，原启动入口仍为 `/home/cld/.camctl/manual-validation-20261026/start-host-demo.sh`。以上证明软件验证及安装一致性，真实拍摄结果仍由相机验证取得。
