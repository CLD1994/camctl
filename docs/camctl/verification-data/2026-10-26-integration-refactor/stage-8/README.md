# 阶段 8：2026-10-30 结果页与根公开边界证据

本阶段归档五组彼此独立、已经终止的测试结果，两个对应运行的完整源码清单，bootstrap 失败场景的有限临时事实，以及静态边界审查和既有结果页参数比较。f44 与 c597 两个快照分开保留；这些结果不合并为一次全量回归，也不表示总体重构目标完成。归档本身未运行测试。

## 独立测试运行

| 运行组 | 收据 HEAD | JUnit 结果 | `suite_time_seconds` | `testcase_time_seconds_sum` | pytest.log 时间 | 收据墙钟 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `merged-result-page193/capture` | `f44e37ab293a62512bc0bfad431cc89fcec1c19e` | 47 通过，0 失败，0 错误，0 跳过（47 个节点） | 17.264 秒 | 15.388 秒 | 17.28 秒 | 18.336 秒 |
| `merged-result-page193/persistence` | `f44e37ab293a62512bc0bfad431cc89fcec1c19e` | 146 通过，0 失败，0 错误，0 跳过（146 个节点） | 19.583 秒 | 18.393 秒 | 19.60 秒 | 21.360 秒 |
| `merged-public-boundaries/outputs` | `c5977f4bf7cfbbfc326ee0ecf55888d0c0357c14` | 253 通过，0 失败，0 错误，0 跳过（253 个节点） | 86.865 秒 | 84.829 秒 | 86.91 秒 | 88.634 秒 |
| `merged-public-boundaries/bootstrap` | `c5977f4bf7cfbbfc326ee0ecf55888d0c0357c14` | 27 通过，1 失败，0 错误，0 跳过（28 个节点） | 17.080 秒 | 16.753 秒 | 17.09 秒 | 18.677 秒 |
| `merged-public-boundaries/unit` | `c5977f4bf7cfbbfc326ee0ecf55888d0c0357c14` | 9371 通过，0 失败，0 错误，1 跳过；日志含 2 个 warning（9372 个节点） | 133.906 秒 | 119.881 秒 | 133.98 秒 | 136.314 秒 |

result-page 两组来自 f44 快照：`capture` 47 项通过，`persistence` 146 项通过。public-boundaries 三组来自 c597 快照：`outputs` 253 项通过；`bootstrap` 27 项通过、1 项失败；`unit` 共 9,372 项，其中 9,371 项通过、1 项跳过，pytest 日志记录 2 个 warning。各组单独报告；bootstrap 失败保留在记录中，不由 outputs 或 unit 的通过抵消。JUnit 套件时间、case 时间总和、pytest.log 摘要和收据墙钟分别记录，不能混为一个计时字段。

## Bootstrap 失败的适用范围

`merged-public-boundaries/bootstrap` 的失败节点是 `test_cancel_during_local_drain_withdraws_queued_startup_sweep`。日志显示断言在测试的 controlled blocker 已完成后失败，Future 带有 `TimeoutError('受控线程门闩未获释放')`；测试日志还记录扫描任务状态、`old_copy_exists=False` 和 `settlement_error=None`。同一运行源码清单确认该用例源码与 c597 对应；精确 c597 源码设置 `ThreadPoolExecutor(max_workers=1)`，并使用 `_GATE_TIMEOUT = 10`。因此这里记录的是本次单线程受控池和 10 秒测试门闩的失败证据，不外推到其他线程池、设备或持续时间。

失败输出还包含取消与数据库打开异常的诊断链，但该断言失败没有验证预期的取消/撤回流程最终是否交付原打开错误。本归档不把这一结果定性为生产缺陷，也不声称完整的错误交付路径通过。失败快照保存 `state.db`、`state.db-wal`、`state.db-shm`、`camctl.log`、`staging/logs/failure-marker.json` 和 `staging/logs/notes.txt`。复制前后 `fuser` 均未发现这些数据库文件的活动使用者；归档按原始字节读写，没有打开 SQLite；WAL 和 SHM 与数据库一起保留。`failure-marker.json` 的现有 38 字节内容是文本而非 JSON，manifest 将它按文本保存，不把扩展名当作解析结果。

## 根非 Python 边界静态审查

`root-nonpython-boundary/` 保存 audit JSON、对应 Markdown 报告和 resolution JSON。审计基于 `f44e37ab293a62512bc0bfad431cc89fcec1c19e`，记录开始时观察到的 HEAD `f5e962b1ca2ede79fdca2bd477ca1e751653d860`；后续 root 判定的 source HEAD 仍为 f44，判定时观察到 c597。审计 JSON 的 SHA 与 resolution 内的 `survey_sha256` 相同。审计有 7 个目标源文件和 23 个不重叠的正式上下文文件，共 30 个文件；7 个目标的 SHA 与 f44 提交中的文件对象一致。`apps/client` 在 f44 与归档时当前 HEAD `6ba76b54132b4f3ae1178598e817efeb11ac967c` 之间没有 Git 路径差异。判定材料标记 `tests_run_by_this_resolution=false`，因此它们是边界静态审查，不是 Node、pytest、客户端构建或设备验证结果。

## 结果页参数比较

`result-pages/baseline-attempt-comparison.json.gz` 保存已存在的两组对照 JSON（`public-full-1` 与 `baseline-persistence-1`），保留共同的 clock anchor `1768467600000000` 与 3/10/3 参数记录。它是既有比较材料，不是本阶段新运行；该 JSON 引用的数据库不在本阶段读取或复制。

## 归档范围与校验

manifest 包含 27 条文件记录：15 条测试运行文件、2 份源码清单、6 个 bootstrap 失败快照文件、3 个根静态审查文件和 1 份结果页参数比较。所有 payload 均为 mtime=0 gzip，manifest 保存原始和压缩 SHA-256、字节数、来源路径及适用的 40 位 source HEAD。JSON/XML 解压后可解析；名为 `.json` 但内容非 JSON 的 marker 按文本保存。数据库只做原始字节摘要与一致性检查。没有复制 bytecode cache、整个临时目录或通过用例数据库，也没有把五组结果合并成一次通过。
