# 报告导入夹具来源

`confirmed-short-report.json` 由 camctl 报告生成器从真实的覆盖检查、来源发布、取回及删除处理结果生成；`1.bin` 是同一测试中实际发布到 delivery 的文件。两者未经改写业务字段或内容。

生成入口是 `apps/camctl/tests/integration/history/test_action6_video_result_history.py::test_action6_fixed_h_survives_obtain_delete_and_inspection_cleanup`。测试在完成真实来源处理与 `generate_report_file` 后生成 `staging/after-obtain-delete-cleanup.json`，并从实际 `ready/1.bin` 取回文件。可用以下定向命令重新生成：

```sh
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest --basetemp=.local/verification/action6-client-fixtures apps/camctl/tests/integration/history/test_action6_video_result_history.py -q
```

本报告中的 `camera_record` 来源动作失败，`result.coverage.status` 为 `confirmed_short`；同一报告包含一个已发布并可核验的 delivery。文件内容用于客户端 SHA-256 关联测试，不要求媒体解码。
