# 完整集成验证资料（2026-10-26）

本目录归档首轮完整集成、补跑、独立审计、逐项诊断、合并证明和第三批窄验证。验证结论由[统一审计](additions/replay-final/supplemental/unified-audit/unified-audit.json)说明；[最终审计索引](audits/final-index.json)登记冻结清单的15项原文及计时和参考脚本。

`main/` 和 `replay/` 分别保留两轮原始 `source.json`、计划、结果摘要、collection 库存、逐任务命令、cases、进度、事件与日志。各自的逐文件源码摘要保持原文。[覆盖审计](additions/replay-final/supplemental/combined-coverage-audit/combined-coverage-audit.json)保留两轮测试身份和实际结果；第三批当前源码的[窄验证审计](additions/replay-final/supplemental/final-narrow-audit/final-narrow-audit.json)及[实际收据](final-combination/receipt.json)单独保存。

[资源总审计](additions/replay-final/supplemental/unified-resource-audit/unified-resource-audit.json)与两轮[首轮原始收据索引](resource-receipts/main/index.json)、[补跑原始收据索引](resource-receipts/replay/index.json)相互对应。357份首轮及846份补跑原始JSON逐份保存，包含别名、副本和pending原文；89组缺少final证据的状态保持审计原状。`budget/` 保留两轮launcher原文，`execution-records/` 保留实际选择、执行命令和session中断，两个driver日志分别位于对应轮次目录。

[最终诊断索引](diagnosis/final-index.json)指向各诊断文件在冻结时对应的归档版本；较早版本同时保留。`merge/` 保存合并证据包与原清单。首批`summary.json`和`additions/replay-final/summary.json`分别记录归档批次，当前整体内容以`manifest.json`及[核验收据](verification-receipt.json)为准。

清单记录每份原文与归档文件的字节数、SHA-256、编码和来源。`encoding=gzip`的文件解压一次即可恢复原文，其余文件原样保存。运行`python -B verify.py`可独立核对整个归档、全部解压原文、1203份预算JSON与审计路径的一致性，以及内层merge包。核验只需要本目录；清单内的原绝对路径用于追溯。manifest自身和核验收据不列入manifest，以免自引用，收据另记录manifest摘要。
