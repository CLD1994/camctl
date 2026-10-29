追加程序由 `append_evidence.py` 提供。收到本轮结束通知后，使用外部 Python 3.11 执行；`--help` 只显示参数，不读取或复制 replay 内容。

```bash
/workspaces/camctl/apps/camctl/.venv311/bin/python -B append_evidence.py \
  --replay /tmp/camctl-integration-replay-20261026-u19lkvoe \
  --label replay-final \
  --resource-audit main=/tmp/camctl-full-272f-audit/resource-audit.json \
  --resource-audit replay=/tmp/camctl-full-272f-audit/replay/resource-audit.json \
  --audit coverage=/已完成的coverage审计路径 \
  --audit resource=/已完成的resource审计路径 \
  --audit production=/已完成的production审计路径 \
  --evidence selection=/实际选择记录路径 \
  --evidence interruption=/实际中断记录路径
```

审计和补充证据可以传入文件或窄目录，并可重复使用参数。后续 merge 修复窄证据也通过 `--evidence name=/绝对路径` 加入。replay 根目录中的原始元数据、collection/tasks 的直接 artifacts、预算目录原文，以及两轮同名 sibling `-driver.log` 自动收录。诊断目录重新遍历，新文件加入原相对位置，变化文件另存到当前批次版本目录。

两份最终资源审计必须明确传入。入口逐份保留 `receipts[*].paths` 指定的原始 JSON，包括 pytest 别名、副本和 pending 收据；只选择审计引用的 JSON，不复制其所在测试世界。每轮另有路径索引和审计原文，缺失 final 的进程组保持审计原状。实际选择、命令与 session 中断记录自动收录。第三窄验只取其 `receipt.json`、`results.xml`、`output.log` 及独立执行脚本。

入口要求 replay 摘要包含终态和退出码，预算 launcher 已结束且收据没有剩余进程。原 main 文件和既有诊断原文保留；追加前的整体 manifest 与收据也保存在本批次中。冻结 source、tmp 世界、数据库、依赖、cache、dist 和 wheel 目录不复制。结束后整体 manifest 重新登记，并验证全部归档文件和解压原文 SHA-256、内层 merge 包及无未列文件。

追加命令成功后，再运行 `python -B verify.py` 可独立核验；核验只需要当前归档目录。首批 summary.json 保留最初统计，当前完整内容以整体 manifest.json、verification-receipt.json 和各批次 summary 为准。
