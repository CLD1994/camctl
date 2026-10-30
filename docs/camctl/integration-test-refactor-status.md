# camctl 集成测试重构状态总览

本文件是[integration-test-refactor-status.json](integration-test-refactor-status.json)的摘要；JSON保存冻结提交中全部逐文件状态、SHA-256、静态定义数、候选路由和证据依据。维护文件级状态时只更新JSON，再据它更新本摘要。

## 快照与范围

- 冻结Git提交：`519d1e8e4339e53d34059eb41c0250a2f65d9405`（tree `deb924844b9f1ece45a27ccae6c09493f9968edc`）。
- 范围：这是`519d`冻结树的文件索引，不代表此刻工作树的全库剩余文件或用例数量。快照包含`658`个受跟踪源码文件，651个Python、2个TS、4个MJS和1个C；Python语法树（AST）在全部651个Python源码中识别出`3866`个静态测试函数定义，涉及`546`个文件；MJS逐文件核实的`node:test`注册调用点共`55`个。参数展开不计入这些静态定义数，`skipif`不从定义数中扣除。
- 完整路径、当前blob SHA、源类型、函数数、路由与证据只在JSON的`files`数组中维护。清单生成前核对了冻结树的实际`git ls-files`，未运行pytest或构建。

## aa2e 定向证据交叉引用

本次仅核对`acceptance`、`contracts`和`host_files`三个目录。JSON中的`current_crosswalk_snapshot`固定为`aa2e5f268bd652c3f9c3dc34e27405f7ca4cb6a9`，逐文件`current_crosswalk`保留当前blob SHA、运行范围和局部评审链接。

阶段5的三个目录任务（[归档清单](verification-data/2026-10-26-integration-refactor/stage-5/manifest.json)）分别收集并通过337、457和128个pytest节点，覆盖14、8和13个测试源文件，共35个文件、922个节点。阶段5跟踪清单将这些任务对应到源码提交`290f4ad39db1bffdf6b764e2ef77c97ce12a82fb`；我逐文件比较了290f、519冻结提交与aa2e当前提交的Git blob SHA，35个测试源文件全部相同。该证据只确认这些节点曾在相应目录运行并通过，不能证明每个节点都覆盖公开接口或整文件边界均已审查。阶段5整个14任务组另有失败；这里的通过数只指表内三个独立目录任务。

| 目录 | 519索引源文件 | 含测试定义的文件 | 阶段5运行节点 | 已关联的具体审查范围 |
| --- | ---: | ---: | ---: | --- |
| `acceptance` | 15 | 14 | 337 | `test_validation.py`的七个精确校验节点已移到单元层，且当前单元函数AST与cd228基线集成源一致；`test_fields.py`具名登记见证仍存在，但当前函数AST与基线不同。另有一个事务调用候选仍待判断。 |
| `contracts` | 10 | 8 | 457 | `test_schemas.py`和`test_motor_schema.py`的具名真实见证源码与cd228基线相同；`conftest.py`及`test_external_boundaries.py`匹配既有语法树复用优化记录的实现SHA。`test_dependencies.py`的旧实现SHA与当前源码不符，未沿用旧身份。 |
| `host_files` | 14 | 13 | 128 | `test_capture_sources.py`当前完整文件SHA与cd228基线相同；旧记录说明三个值、路径和JSON检查继续留在集成层，但没有记录三个具体节点标识。`test_tasks.py`的两个`pending`名称命中已被静态审计判为测试局部等待列表，不是运行时待处理集合候选。 |

`acceptance/test_source_member_proof.py`在冻结的AST审计中仍有两处`commit_operation`调用候选，当前源码SHA吻合，仍需按其受理边界目标与事务专项边界判断。`host_files/test_tasks.py`的筛除结论只覆盖所列两个局部列表位置，不代表整文件边界已完成审查。所有原`current_status`保持519冻结值，没有新增整文件完成声明；运行证据之外未找到对应文件的进一步公开边界判定时，JSON保留未决标记。

逐文件候选和归档位置见[阶段7公开边界审计](verification-data/2026-10-26-integration-refactor/stage-7/boundary-inventory/public-boundary-inventory.json.gz)及[阶段12剩余边界审计](verification-data/2026-10-26-integration-refactor/stage-12/remaining-boundary-survey.json.gz)。局部映射见[单元归属记录](verification-data/2026-10-25-integration-optimization/unit-bootstrap.json)、[contracts优化记录](verification-data/2026-10-25-integration-optimization/contracts.json)和[优化总结](integration-test-optimization-2026-10-25.md)。阶段5、阶段7和阶段12归档使用仓库内`.gz`文件，并按解压内容SHA-256核对；关联的运行提交由阶段5跟踪manifest确认。`unit-bootstrap.json`、`contracts.json`和优化总结是仓库跟踪的未压缩源记录，没有gzip副本，因此JSON记录其原字节SHA，不伪装成压缩归档。具体文件、原始SHA、压缩SHA和逐文件映射以JSON为准。

## 已登记的未迁移范围

| 状态 | 登记数 | 含义 |
| --- | ---: | --- |
| 已知待迁 | 32 | 现有明确待迁分组，包括bootstrap两组、outputs清理四文件组及两项单例。outputs四文件的13项没有逐文件分配，因此JSON逐文件只指向共享组总数。 |
| 用户暂缓 | 16 | `capture/test_segment_inspections.py`登记的16项，保持暂缓。 |
| 部分路由待审 | 18个文件 | capture十六个文件与history两个文件有待审路由；不据此断定每个文件仍有有效未迁移测试。 |

媒体夹具源码同时提供夹具准备与一个受`skipif`控制的实际测试定义，因此按测试源计数；其当前环境是否收集以及是否需迁移仍未确认。逐文件状态数量如下，表内计数是文件数，不是测试实例数：

| 519冻结索引中的文件状态 | 文件数 |
| --- | ---: |
| 519冻结状态：尚未关联既有边界评审证据的测试源 | 523 |
| 已登记待迁 | 8 |
| 用户暂缓 | 1 |
| 部分路由待审 | 18 |
| 支持或辅助源 | 108 |
| 合计 | 658 |

523是519冻结索引中`current_status`标签的历史计数，并非aa2e以后未关联证据的当前总数。本次为三个目录增加的逐文件交叉引用范围见上文；其余已有迁移或审查进展仍按各自记录维护。状态清单不因尚未关联的文件而要求全库重跑或从零审查。

已登记48项（32待迁、16暂缓）是明确范围下限，不是全库剩余总数。文件路由、测试函数定义数或历史AST候选都不能补成有效参数实例总数；因此本表不编造剩余总数或完成百分比。

## 状态解释

Python静态计数覆盖全部651个源码：统计模块级`test_*`函数、`Test*`类方法及直接继承`unittest.TestCase`的类方法；不按文件角色过滤，且不展开参数化实例。历史候选按旧审计记录的SHA与519d冻结blob逐文件比较。SHA一致只保留为复核路线，SHA不一致就不沿用旧行号；没有命中旧候选也不代表符合公开接口。事务最小命令边界、固定样本及85/126/23批次只记为特定边界证据，不标记整文件完成。

JSON把`current_status`与`confirmed_unmigrated_case`分开：部分路由的有效活动用例数为`null`，表示尚未确认；支持文件不会计为独立业务测试完成。最新工作树提交之后的变更不回写到本冻结快照，后续由root按受影响文件SHA更新。

JSON中的来源指南、历史审计和P3事务范围证据均保留仓库内文件路径与SHA；压缩归档项的SHA按解压内容核对。519之后的clock配置和仓储替身提交在JSON中单列为快照外消息，不计入文件现状。静态计数方法和边界限制见`method_and_limits`。
