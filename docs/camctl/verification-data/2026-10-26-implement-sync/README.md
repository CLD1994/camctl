# implement_camctl 后续同步验证（2026-10-26）

本目录保存 `c55f94d4` 合入 `27713c14` 后的实际检查。结果与限制见[回归报告](../../integration-regression-2026-10-26.md#后续分支同步与定向验证2026-10-26)，汇总见[执行回执](receipt.json)。本次定向集成未通过，没有重跑完整集成套件。

| 证据 | 内容 |
| --- | --- |
| [单元回执](unit/receipt.json)、[原日志](unit/pytest.log.gz)、[JUnit 结果](unit/results.xml.gz) | Python 版本、命令、合并父提交、受测源码逐文件摘要及完整结果。 |
| [集成汇总](integration/summary.json) | 10 个目标文件、3 个隔离任务、冻结源码摘要、命令、进程和退出结果。 |
| [装配任务](integration/bootstrap/result.json)、[拍摄任务](integration/capture/result.json)、[会话任务](integration/session/result.json) | 各任务同目录另存命令、节点清单、逐节点结果和压缩原日志。 |
| [失败对应关系](failure-comparison.json) | 本次 11 项失败与前次归档节点及首因分类的逐项对应。 |
| [默认删除日志](integration/bootstrap/default-delete-camctl.log.gz)、[默认查询日志](integration/bootstrap/default-query-camctl.log.gz) | 两条真实默认清理路径的 `closing` 名称错误。 |
| [主程序回执](host/receipt.json) | 构建与全部单元入口结果、环境及源码前后摘要；原日志在同目录 `logs/`。 |
| [文件清单](manifest.json) | 归档文件长度与 SHA-256，不包含清单自身。 |

`.gz` 文件采用 gzip 压缩。原回执中的绝对路径保留执行时的位置，读取归档结果无需原临时目录。Python 单元回执记录受测生产代码、测试、C 组件和协议摘要；验证结束后逐项核对未发生变化。未执行的业务与资源验收不计作通过。
