# 阶段 7：2026-10-30 窄范围组合证据

本目录为持续问题记录保存已经结束的窄范围组合结果和一份交接映射。它只固定本次明确列出的六组运行，不表示完整集成回归，也不表示 S13 或总体目标完成。

## 独立运行组

| 运行组 | 收据中的 40 位 HEAD | JUnit 结果 | `suite_time_seconds` | `testcase_time_seconds_sum` | pytest.log 时间 | receipt 墙钟 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `merged-boundary-diagnostics54（引用 stage-6）` | `76e07abd3f58c7538b52d1b199c63fa2f5a3bef1` | 54 通过，0 失败，0 跳过 | 22.459 秒 | 20.814 秒 | 22.47 秒 | 24.610 秒 |
| `merged-boundary-photo/capture` | `47cb7c6b302def953bab5c5d4709c8b6311823fe` | 79 通过，0 失败，0 跳过 | 32.461 秒 | 29.458 秒 | 32.47 秒 | 33.535 秒 |
| `merged-boundary-photo/bootstrap` | `47cb7c6b302def953bab5c5d4709c8b6311823fe` | 77 通过，0 失败，0 跳过 | 17.873 秒 | 16.659 秒 | 17.88 秒 | 19.765 秒 |
| `merged-boundary-photo/history` | `47cb7c6b302def953bab5c5d4709c8b6311823fe` | 43 通过，0 失败，0 跳过 | 54.417 秒 | 53.457 秒 | 54.43 秒 | 56.282 秒 |
| `merged-boundary-work-files/bootstrap` | `32819074ddbbc2bd48bd71bb1bd994e5f1a5e46b` | 32 通过，0 失败，0 跳过 | 22.732 秒 | 21.540 秒 | 22.75 秒 | 24.696 秒 |
| `merged-boundary-work-files/outputs` | `32819074ddbbc2bd48bd71bb1bd994e5f1a5e46b` | 56 通过，0 失败，0 跳过 | 20.385 秒 | 18.461 秒 | 20.40 秒 | 22.419 秒 |

照片相关的三组运行都来自同一 HEAD，但各自有单独的测试文件、收据和节点范围；work-files 的两组也分别保留自己的收据。表格只报告各自结果，不合并或相加为全量通过。六份测试收据的 `source_manifest_changes` 均为空；每组原始 `sources_sha256` 字典保留在 manifest。

JUnit 套件时间、单个 testcase 时间总和、pytest 日志摘要和收据墙钟分别来自不同字段。manifest 使用独立字段记录，不以一个 `time_seconds` 代表多种计时口径。

## 五文件交接映射

`boundary-output-handoff/handoff.json.gz` 保存五个文件的旧目标与新公开边界映射。交接文件还保留一个尚未迁移的节点：`test_cleanup_terminal_absence.py::test_pending_companion_precondition_is_checked_before_terminal_file_save`，它仍直接构造 `PendingCleanupCompanion` 并写入 `runtime.pending_companions`。交接明确说明总体目标未完成；因此五文件映射和这些窄范围通过结果都不能作为完成结论。

## 归档与校验

清单有 19 条证据记录：18 条运行文件记录和 1 份 handoff；其中 3 个 diagnostics54 文件引用 stage-6 已有 payload，未重复压缩。本目录新增 16 个 gzip payload，压缩前 66,775 字节、压缩后 13,546 字节。所有新 payload 均以 mtime=0 gzip 保存；原始和压缩 SHA-256、字节数及运行来源见 [manifest.json](manifest.json)。JSON 与 XML 解压后解析通过。归档过程未运行测试，也未读取运行中的 work-files 分支目录、临时目录或数据库。
