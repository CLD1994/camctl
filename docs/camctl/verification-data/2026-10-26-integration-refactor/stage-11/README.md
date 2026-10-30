# 阶段 11：分段历史与 SQLite 报告边界证据

本目录保存五个彼此独立的运行组和两份提交中的精确测试源码/差异。不同运行的目标源码 SHA、基线和范围不同；不能把它们合成一次运行，也不能据此宣称整个 history 目录或 S13 全量验收完成。

## 运行结果

| 运行组 | 运行快照与目标 | JUnit 结果 | pytest 日志 | 外层墙钟 |
| --- | --- | --- | --- | --- |
| segment-history-full-35 | 基线 caaba927facbf47a3c0596486d5fde71081c7724；目标源为提交 81b2c507ce9a0a515a15526661c9d321a5cb9822 中的 test_record_segment_history.py | 35 通过；套件 128.139 秒；用例时间合计 126.546 秒 | 35 passed in 128.14s | 129.865 秒 |
| report-sqlite-boundary-report-full-1 | 基线 caaba927facbf47a3c0596486d5fde71081c7724；目标文件 SHA 53943afc2b89ccab08eb6326b6ba3ce29842ab710dbc5079a29e7a320affed3b | 43 通过；套件 60.211 秒；用例时间合计 59.187 秒 | 43 passed in 60.21s | 61.635 秒 |
| report-sqlite-boundary-replacement-1 | 基线 caaba927facbf47a3c0596486d5fde71081c7724；目标文件 SHA e931aafa152d300b8b1009b9743bc4ae72c393509530c1fa3b6faab4510f4e23 | 1 通过；套件 0.595 秒；用例时间合计 0.389 秒 | 1 passed in 0.59s | 1.335 秒 |
| report-sqlite-boundary-replacement-2 | 基线 4ee7b1487bc7b60be3e95776ca61677c14100f44；目标源为提交 8ce2768f309364832a9c4f19ee6056e54011fef8 中的 test_report_selection.py | 1 通过；套件 0.549 秒；用例时间合计 0.357 秒 | 1 passed in 0.55s | 1.196 秒 |
| merged-history-callers-report-fcfbfb0d | 主树提交 fcfbfb0d3d1588174cb38b2cca0ad04130b91305；仅 test_snapshot_fix_round1.py 与 test_report_selection.py | 88 通过：前者 45 项、后者 43 项；套件 84.530 秒；用例时间合计 82.883 秒 | 88 passed in 84.55s | 85.344 秒 |

JUnit 套件时间、所有 testcase 时间之和、pytest 日志所报时间和外层墙钟采用不同测量口径，清单将四者分开保存。报告三组的源码 SHA 各不相同：report-full-1 与 replacement-1 的精确源码/补丁在现有证据目录中缺失，本归档只保存各自的过程收据、日志和 JUnit XML；没有用后来版本替代。replacement-2 的精确源和相对 caaba 基线的单文件差异来自 8ce2768f Git 对象。分段历史测试源及其差异来自 81b2c507 Git 对象，差异与原始 change.diff 字节一致。

主树 88 项来自 fcfbfb0d 记录的两份测试文件，运行收据注明源码清单前后没有变化。该组独立记录，不与作者树中的 43/1/1 报告边界结果合并。

## 导入与范围边界

分段历史 full.json 中的 import_path 是启动器拼接出的字段，并非 pytest 运行期间的实际导入观测。import-supplement.json 记录的是 pytest 后在独立解释器中的检查。主树 preflight-import.json 同样是在 pytest 前由独立解释器检查导入路径，并不是 pytest 内的模块来源或资源访问跟踪。

分段历史交接只覆盖记录的 test_record_segment_history.py 迁移范围；其中结构摘要只核对迁移前后的测试函数名、断言和 pytest.raises 数量，不替代行为覆盖审查。主树 88 项也只运行两份点名的测试文件，不能代表整个 history 目录、S13 全量、真实设备或未执行的故障分区。

本归档没有复制通过用例数据库、临时目录或源码树。归档器只压缩已终止的证据文件，没有运行测试。manifest.json 逐项记录来源路径、原始和压缩字节数、SHA-256、gzip mtime 与 JSON/XML 解析核验结果。
