# 阶段 6：2026-10-30 单测、仓储端口与收场证据

本目录保存从已结束运行中选出的收据、完整测试报告、必要日志、逐项映射及少量现成 SQLite 文件字节。`manifest.json` 是来源路径、源码身份、运行身份、范围、原始文件摘要、gzip 大小和压缩摘要的权威清单。所有 `.gz` 的 gzip 时间戳为 0；压缩后已完整解压核对字节、SHA-256 和长度，并解析 JSON/XML。归档过程没有运行测试，也没有使用 SQLite 打开或查询数据库。

## 运行结果与源码身份

| 证据组 | 源码身份与运行范围 | 记录结果 |
| --- | --- | --- |
| `unit-all` | HEAD `d56be550c597ed96c8aedd6b986eeb1ad97c05e5`；PID `67982`；`2026-10-30T09:32:56.603572+00:00` 至 `2026-10-30T09:35:30.676084+00:00`；exit `0` | 组件单元测试全目录：9,354 通过、1 跳过，JUnit 共 9,355 节点；此结果只归属于 d56 源码快照。 |
| `unit-boundary-audit` | 原审计固定在 HEAD `d56be550c597ed96c8aedd6b986eeb1ad97c05e`；只读定义在后续规范提交 `7f0aa164` 获批准并记录 | 保留原 JSON/Markdown 字节，不重跑、不改写。原审计只覆盖首次导入观察，不代表所有单测函数体均无资源访问。 |
| 仓储端口初轮 | HEAD `ed29963d5aa4348778afa78515a3917cde285e2b`；初轮 before/after 收据记录同一对未提交源哈希 | before 为 8 失败、9 通过；after 为 1 失败、16 通过。 |
| 仓储端口最终复核 | HEAD 仍记录为 `ed29963d5aa4348778afa78515a3917cde285e2b`，但源文件哈希已变化 | before-final 为 8 失败、9 通过；after-final 为 17 通过。最终测试和 helper 源码快照与该轮哈希一致。初轮源码字节已缺失，只保留初轮收据中的 SHA，没有用最终版本替代。 |
| main 仓储端口单测 | dbea 主工作区早期 helper-9 快照；PID `76553` | 9 项通过；与 review worktree 的 17 项结果分开记录。 |
| 来源容量 unit | HEAD `1cc0f887654db9c3cb7f37a948892ab6a5061236`；PID `50552` | 8 项通过；保存原单测 XML、日志及首次导入记录。 |
| 来源容量首次调用 | 同上；PID `50599` | 测试准备因 basetemp `/tmp/camctl-resume-20261030/source-capacity-evidence/capacity-final` 不存在而报 `FileNotFoundError`；测试主体没有执行，目录下没有叶组预算结果或数据库。收据、日志和 XML 保留了这个准备失败。 |
| 来源容量 2,400 项 | 同一 HEAD；PID `50645`；独立于首次调用 | 一个固定批次测试通过；write、reuse、repeat 三条业务消息均记录 2,400 个成员。叶组记录 memory-and-swap 合计限额 2 GiB、合计峰值 43,040,768 字节，OOM 与限额事件为 0，退出码 0，组已回收。单独交换峰值没有测量；launcher 收据的 delegation 父组近乎无限额，不用于替代叶组预算。 |
| capture 分段生产 gap | facts 文件没有记录源码提交或完整运行收据；保存事实文件指向的精确 DB | 原始 facts、`state.db` 及当时存在的 WAL/SHM 作为字节快照保存。归档过程未查询该数据库。 |
| bootstrap `public-boundary-final48-real` | 当前完成工作区 HEAD `88bfa20efb5670f80374973ed4c0975722b70d05`；PID `60309`；exit `1`；source SHA `cffb918058953a018fbe6d08528574b83c24a0c094ec25c8ca1b4b59a2d05998` | 48 节点：44 通过、4 失败。节点映射记录 3 个失败发生于 `closing=true`；另一个 `default-delete` 失败为 TimeoutError。四个失败对应的 JUnit tmp_path、日志和现成 DB（含现存 WAL/SHM）被单独保存。 |
| bootstrap default-delete 单项复跑 | 同一完成工作区；PID `63186`；exit `0`；source SHA `2a2534d061e534c01eb77cdf256f97f889091698f5fab09dc23e479949c89ae0` | 单个用例通过。它是后续单项观察，不确诊也不覆盖上面的 48 节点结果。对应收据、日志、XML 和现存 DB 分开归档。 |

这些运行来自不同 HEAD、工作区和时间点，不能合并成一次全量通过。`unit-all` 的 9,354 项通过、仓储端口各轮结果、2,400 项容量结果与 bootstrap 48 节点结果分别保留各自来源。

## 后续补充的稳定证据

- `merged-boundary-diagnostics54/` 保存 HEAD `76e07abd3f58c7538b52d1b199c63fa2f5a3bef1` 的单独诊断范围：54 项通过，pytest 阶段 22.459 秒，进程墙钟 24.610 秒；收据记录 source manifest 未变化。该结果单独归档，不并入 unit-all 或 bootstrap 的运行。
- `diagnostic-other14/cost-summary.json.gz` 是 290f4ad3 诊断轮 14 个串行任务的节点、文件、任务计时摘要，引用 14 份原 cases 文件 SHA。其范围字段明确声明不是最终回归或提速比较，摘要中的 call 耗时也不是单个目标操作耗时。没有复制完整 events、临时目录或数据库。

## 缺失与排除

- 仓储端口初轮的源哈希在收据中，但其原始源码字节已不存在。保留在 review 目录中的源文件匹配最终复核哈希，不能代替初轮代码；清单明确列出两个初轮哈希缺口。
- 来源容量首次调用的 `capacity-final` basetemp 目录缺失；收据、pytest 日志和 XML 保留了准备失败。没有对应业务 DB 或受限叶组收据可归档。
- capture facts 没有记录源码提交。清单将 `source_head` 留空，未按相邻仓库推断。
- 未归档源码工作树、依赖、pycache、pytest 临时目录、普通用例 DB、通过用例 DB、容量输入/expected fixture 或全量 capture 证据。bootstrap 只保存四个实际失败节点及 default-delete 单项复跑对应的 DB 字节和日志。

## 文件和校验统计

本阶段包含 73 个 gzip 证据文件，原始字节合计 15,332,270，压缩字节合计 1,272,228。逐文件来源、运行身份、解压长度和双重 SHA-256 见 [manifest.json](manifest.json)。该归档只固化已存在证据，不代表阶段验收。
