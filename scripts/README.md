# 仓库开发与检查脚本

本目录保存跨组件使用的脚本。组件自己的构建、生成和测试工具留在组件目录。

## 隔离并行集成测试

在仓库根使用已准备测试依赖的 Python 3.11 运行下列入口。入口默认运行 camctl 的全部组件集成目录、根目录全部 pytest 模块和全部 Node 集成文件；容量测试与完整 Hasselblad 正常链矩阵都保留。

```bash
/absolute/venv/bin/python scripts/run-integration.py \
  --python /absolute/venv/bin/python \
  --node-modules /absolute/client/node_modules \
  --jobs 4 --output /tmp/camctl-integration-run
```

`--output` 必须是尚不存在的目录；不指定时在 `/tmp` 创建新目录。`--python` 默认采用启动脚本的解释器；入口核对 Python 3.11 和 pytest，不安装或同步该环境。客户端依赖默认取源工作区的 `apps/client/node_modules`，需按[客户端说明](../apps/client/README.md)的 `npm ci` 和 `package-lock.json` 预先安装，并与已准备的 Playwright Chromium 版本一致；共享依赖目录发生版本漂移时，先在独立目录按锁文件准备，再用 `--node-modules` 指向该目录。浏览器和媒体工具仍遵守[跨组件测试前提](../tests/integration/README.md)。uv 离线缓存默认取 `uv cache dir`，也可用 `--uv-cache` 指定已准备的缓存目录。

`--camctl-memory-limit` 和 `--camctl-memory-cgroup-root` 分别配置被测 camctl 实例的内存与交换空间合计预算及资源组委派目录，直接 pytest 使用相同参数。预算由实例主进程与其全部后代共享，测试准备、客户端与运行器在组外；默认值由测试支持模块维护，不按 `--jobs` 平分。需要 sudo 委派时，在原命令外使用 `python scripts/run-memory-budget.py -- 原命令`；启动器仅为本次运行创建独立目录，原命令和业务保持调用用户身份。具体合计峰值验收、权限要求及独立无交换验收见[内存预算测试](../docs/camctl/memory-budget-testing.md)。列表和收集模式只解析配置，不启动受限业务；未实际执行的容量检查不计作通过。

先用 `--list` 查看从实际源码发现的任务；该选项不执行测试。`--scope camctl`、`--scope root`、`--scope node` 分别选择组件目录、根 pytest 和根 Node，默认 `all`。`--target` 可以重复指定所选范围内的仓库相对目录、文件或 pytest nodeid；同一组件目录仍只启动一个 pytest，不同目录保持独立进程。文件、目录或函数已包含的参数节点不会再次分配；文件部分先规范化相对路径，拒绝逃出源码根的目标。例如：

```bash
/absolute/venv/bin/python scripts/run-integration.py --list
/absolute/venv/bin/python scripts/run-integration.py --scope camctl \
  --target apps/camctl/tests/integration/motor \
  --target apps/camctl/tests/integration/logging_runtime --jobs 2
/absolute/venv/bin/python scripts/run-integration.py \
  --target 'tests/integration/test_hasselblad_photo_roundtrip.py::test_installed_hasselblad_report_rebuild_keeps_frozen_bytes' \
  --node-modules /absolute/client/node_modules --jobs 1
```

入口从 Git 已跟踪文件与未忽略的新文件冻结源码，包括当前未提交的修改；删除的文件保持删除。`--source` 可指定另一 Git 工作区。快照保留正式源码、协议、构建文件、测试素材和文档，但不复制 `docs/camctl/verification-data/` 中的历史验证输出；该目录中用于调度的 `camctl-files.csv`、`cross-component-files.csv` 和 `cross-component-cases.csv` 仍按当前工作区内容纳入快照。本轮输出目录即使位于源工作区内也不会纳入。生成目录、依赖和缓存不作为源码快照输入。每个任务取得独立源码副本；Git 索引基于原 HEAD，因此排除的已跟踪文件在副本的 Git 状态中显示为删除，不能将副本视为完整或干净的仓库。`source.json` 记录来源 HEAD、原工作区状态、快照范围、实际纳入文件的摘要和整体摘要；它不把 Git 索引描述为原工作区的暂存状态。快照只接受普通源码文件；发现符号链接时明确报错，不跟随链接访问外部对象。已有逐文件测量仅用于优先调度较长任务，未测目标照常执行。

根 pytest 中，历史所选节点估算至少 600 秒，或逐实例历史 CSV 的 `status` 曾为 `failed` 的模块，先在独立源码副本执行 `--collect-only`；本次收集多于一个节点时按当前清单分片。没有历史失败的短模块默认保持单进程与一次准备。显式指定 `--force-root-shards` 时，本次所选根 pytest 模块不受历史时长或失败记录的候选条件限制；该选项不改变组件目录或 Node 任务。`--root-shards` 默认取 `--jobs`，限制每个模块的片数；设置为 1 可保持模块单进程，即使指定强制选项也不分片。实际只收集到一个节点时也保持单任务。历史失败只计到实际失败步骤，其耗时是完整成本的下界，不能与已完成用例的耗时视为可比的完整成本；`historical_failure_observed` 在计划中标识这类模块。历史逐实例耗时仍作为均衡权重，不是精确负载预测，历史未登记的新节点仍会分配；各片的并集必须与本次收集清单一致且互不重复。每片独立构建／安装和执行，容量规模及完整 Hasselblad 十二组正常链保持。窄目标只估算并收集所选节点，不会因同模块的其他慢例而扩大覆盖。强制分片会增加收集与各片准备成本，是否缩短墙钟取决于实际工作量和并发。

`--list` 只展示发现与选择的模块任务；需要审查实际分片时，用 `--plan-only` 冻结源码并生成 `plan.json`，分片候选的真实收集清单与命令保存在 `collection/`。此模式不执行测试，也不代表测试通过：

```bash
/absolute/venv/bin/python scripts/run-integration.py --plan-only \
  --jobs 4 --output /tmp/camctl-integration-plan

/absolute/venv/bin/python scripts/run-integration.py --plan-only \
  --scope root --target 'tests/integration/test_hasselblad_photo_roundtrip.py::test_installed_hasselblad_photo_roundtrip' \
  --force-root-shards --root-shards 4 --jobs 4 --output /tmp/camctl-forced-plan
```

每个执行槽按任务需要准备独立的 uv 缓存、uv 项目环境和客户端 `node_modules` 副本；同一槽依次复用，槽间不共享写入。Node 任务只准备源码、临时目录和客户端依赖，不复制 uv 缓存或创建 Python 环境，也不同步 Python 包资源。同一槽随后执行 Python 任务时再准备这些资源。复制使用 `cp -a --reflink=auto`，支持时由文件系统写时复制，否则复制普通文件，不建立硬链接。每个任务另有自己的源码、`_resources`、客户端 `dist`、pytest 临时根、SQLite、交接文件与安装环境。任务的 `.venv311` 使用标准库创建，不安装包，以 `.pth` 只读引用外部测试环境的依赖；源码通过该任务的 `PYTHONPATH` 导入。每槽的 `PYTHONPYCACHEPREFIX` 位于源码之外，创建虚拟环境、同步资源、pytest 与安装版 CLI 共同使用该私有字节码缓存；collection 使用自己源码副本外的私有前缀。入口清除继承的两个字节码缓存环境变量，版本核验另外使用 `-B`，不向共享依赖写入字节码。缓存改变不影响安装模块的来源和 wheel 的自包含边界。跨组件辅助中的 C 构建仍使用其随机唯一目录或 pytest 临时目录；现有 `/tmp/camctl-host-build-*` 不受 pytest 临时根管理。Hasselblad 联合链的每个任务还在源码副本之外，从同一快照创建独立客户端候选，保留客户端、协议与测试驱动的相对布局，使用该槽私有依赖执行真实 `npm run build`，并设置 `CAMCTL_HB_TEST_CLIENT_ROOT`。候选的源码与 `dist` 都不引用用户共享工作区。系统工具、外部测试依赖和已安装 Chromium 只读复用。入口面向 Linux 开发容器，要求 Git、GNU cp、uv 与 Node 已可用；不会安装系统工具，也不代替目标设备验收。

仅选择已核对不执行 Python 包构建或安装的组件测试时，可以将 `--uv-cache` 指向本次创建的空目录，避免为每个执行槽复制已有下载缓存；测试依赖仍由指定 Python 环境提供。使用完毕后由调用方删除这个空目录。需要离线构建、安装或由 uv 准备环境的测试继续使用完整缓存；不能仅按目录名判断，也不把选定文件的通过结果推广到全量。

`plan.json` 保存实际分配，`summary.json` 每半秒更新任务状态、PID、当前节点与阶段、退出码及墙钟时间；每 15 秒也向终端打印活动目标。pytest 插件在节点进入阶段时写入 `events.jsonl` 和 `progress.json`，因此即使用例没有输出，也可定位正在执行的节点。观测器配置在 pytest 配置阶段移除，不进入安装版运行环境。插件导入时固定自身文件操作和计时函数，避免产品测试替换共享模块函数时打断观察记录；真实磁盘错误仍显式报告。每个任务的 `artifacts/` 另存命令、工作目录、源码身份、工具版本、完整输出、真实收集的 `inventory.json`、主实例阶段结果 `cases.json` 和最终结果。主实例按 nodeid 聚合，嵌套子检查另计，父 call 的时长不再次加上子检查时长。Node 文件保留 TAP 日志与结果计数。入口等待全部分配完成后汇总：全部任务退出 0 才返回 0，有任务失败时返回 1；输入或准备失败保留非零状态，中断返回 130。准备与执行子进程都保留具体 PID、命令和退出结果；中断时仅向本入口持有的进程组发送信号并等待收场。早期准备中断只报告实际已经生成的证据。

任务的准备和执行命令实际退出、结果写入后，入口立即删除成功任务的源码副本、客户端候选和临时环境，保留其结果与进度文件；收集成功后同样删除收集用源码和临时环境。清理本次运行拥有的目录时，遇到只读目录会恢复该目录的所有者权限再重试；不修改符号链接指向的对象，其他删除错误仍报告。失败或中断任务的源码副本和临时现场保留在原位置。同一执行槽的任务依次复用该槽缓存；全部直接受管进程和缓存复制工作结束后，入口删除执行槽缓存，失败现场中的缓存链接可能因此失效；完整运行或只生成计划成功时还删除冻结源码副本。各测试及其启动的程序负责等待各自创建的进程，运行器不会额外跟踪已脱离受管进程的后代。输出目录保留计划、源码身份、日志、结果和失败现场；临时材料在问题解决后删除。各任务墙钟包含自身准备，整轮墙钟包含入口准备与全部调度；历史估计值不是本轮测量。只选择窄范围的结果不能证明全量验收。入口本身的真实进程契约测试为：

```bash
/absolute/venv/bin/python -m unittest discover -s scripts/tests -v
```

## 文档与公共协议

在仓库根运行 `node scripts/check-doc-links.mjs` 检查文档的本地文件链接。

运行 `node scripts/check-protocol.mjs` 检查公共协议 Schema、共享计划与能力样例、报告原始字节摘要、已登记错误详情及结构正反例。脚本复用 `apps/client` 已锁定的 Ajv 依赖，需要先准备客户端依赖；不依赖网络下载 Schema。这是访问真实规格文件的检查，不属于单元测试，也不代替生产组件的语义与集成验收。

## 报告字段依赖

运行 `node --disable-warning=ExperimentalWarning scripts/check-report-dependencies.mjs` 检查[报告字段依赖](../docs/camctl/database/report-dependencies.md)。使用 Node.js 24 的 SQLite 接口和上述 Ajv，核对字段覆盖、条件来源、嵌套结构、枚举、真实关联及报告父对象；来源列与实际 SQL 一致。该检查不实现生产报告生成。

运行 `node --disable-warning=ExperimentalWarning --test tests/integration/report-dependenc*.test.mjs` 验证登记的正反例与条件场景；这些测试读取真实 Schema、登记和规格 SQL，属于集成测试。

## 事件转换规则

运行 `node --disable-warning=ExperimentalWarning scripts/check-event-transitions.mjs` 核对[事件转换规则](../docs/camctl/database/event-transitions.md)与实际投影列、状态模型、历史对象及报告依赖的覆盖关系。修改规则后使用 `--write` 更新事件索引与 SQL 类型范围；默认检查拒绝生成内容漂移。运行 `node --disable-warning=ExperimentalWarning --test tests/integration/event-transitions.test.mjs` 验证规则反例和行权限。具名业务校验、生产事务和完整历史恢复不由这些脚本执行。

## 数据库结构与运行库

运行 `~/.venv/bin/python scripts/sync-history-object-sql.py` 核对历史对象登记生成的 SQL；仅在修改登记后使用 `--write` 更新生成区段。运行 `~/.venv/bin/python scripts/check-database-spec.py` 检查真实 SQLite 结构，运行 `~/.venv/bin/python -m unittest discover -s tests/integration -p 'test_database_history_sql.py' -v` 验证历史与报告所需的状态组合、目录范围、外键及分页查询。上述检查不创建实际状态库，也不执行设备操作。

结构检查在内存 SQLite 中加载全部规格 SQL，核对表目录、STRICT 类型、主键、外键及索引，并用合法与非法样本检查状态组合、完整事务边界和精确整数。样本只验证结构和查询，不作为可回放的业务历史；查询计划通过也不代表目标硬件的性能达标。完整生产验收按[事务验证门槛](../docs/camctl/database/transactions.md#实现与验证门槛)及[数据库一致性验收](../docs/camctl/database/consistency-verification.md)执行。

结构检查先读取[统一运行库版本条件](../docs/camctl/sqlite-runtime.json)，核对文档版本表、Python 实际链接的 SQLite 及必要能力。`--runtime-only` 只执行这部分检查，使用隔离的临时文件库验证 WAL/FULL、JSON、STRICT 及分段 BLOB 读写；`--write-runtime-doc` 从同一条件生成[版本表](../docs/camctl/sqlite-runtime.md#允许的版本)。脚本尚未接入生产入口，也不证明目标存储的并发或断电持久性。

结构检查同时读取[全库整数编号定义](../docs/camctl/database/enum-registry.json)，核对有限值列的完整分类、实际 SQL 列值约束、公共错误映射及事件状态条件。`~/.venv/bin/python scripts/check-database-spec.py --write-enum-doc` 从同一来源生成[编号一览](../docs/camctl/database/enum-values.md)及分段公共错误 Schema 的枚举字面量；默认模式检查生成值是否一致。SQL 列值检查与完整表的状态组合样本分别执行，JSON 内部分类只检查定义及归属；生产类型适配、JSON 业务校验和历史往返仍待实现。

## 执行入口配置消费

运行 `python3 scripts/check-config-consumption.py` 核对生产源码中拍摄执行入口对配置的实际消费路径：`CaptureRuntime` 的尝试配置（check、stop、start、query、delete）必须经统一的按设备读取接口（`_device_attempts`／`_device_seconds`）构造或由装配参数转发，设备调用意图的 `config` 必须引用运行时配置字段或转发装配输入，延时额外等待必须经统一读取或注入转发。检查基于语法树与真实调用点：`AttemptConfig` 预算关键字出现数字字面量即拒绝；唯一例外是主机源交付拷贝的本地单次预算协议（一次、无时限、无定时重试），其值由授予拷贝时保存的配置约束。该检查证明入口消费统一配置接口，不验证各接口的实现正确性（由组件集成测试覆盖）。
