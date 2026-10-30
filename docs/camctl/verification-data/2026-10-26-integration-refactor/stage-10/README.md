# 阶段 10：公开 SQLite 报告分页单次结果

本目录只保存 `test_report_selection.py` 的一份 **43 项通过**运行、该运行的精确测试源和从 `c5a257769449bd0d109b8d709f66f3abe6879325` 到测试提交的单文件差异。这个运行来自作者工作树在 c5a 基线上仅增加一个有未提交改动的测试文件；没有在 root 主树另跑一遍。

## 运行身份和结果

| 项目 | 记录 |
| --- | --- |
| pytest 目标 | `apps/camctl/tests/integration/history/test_report_selection.py` |
| 作者工作树基线 | `c5a257769449bd0d109b8d709f66f3abe6879325`，该目标文件当时有未提交改动 |
| 进程 | PID `73504`，退出码 `0` |
| JUnit | 43 项通过，0 失败，0 错误，0 跳过；JUnit 套件 54.723 秒，testcase 用例时间合计 54.509 秒 |
| pytest 日志 | `43 passed in 54.72s` |
| 外层墙钟 | 55.403s |
| 工作树目标文件 SHA-256 | `8248ed499e195b2671abb3726e09dd63c22317b2ceb282ea05f7d80ebb873f0b` |

`process.json` 保存命令和 cwd。运行收据中的目标文件 SHA 与从提交 `5a4c6061470756fdafbd6550dc1337bda5d4b8c8` 用 `git show` 得到的测试文件 SHA 一致。归档保存的是该提交对象中的准确文件字节，没有从当前含未提交改动的工作树拼接重建。补丁由 `git diff c5a257769449bd0d109b8d709f66f3abe6879325..5a4c6061470756fdafbd6550dc1337bda5d4b8c8 -- apps/camctl/tests/integration/history/test_report_selection.py` 生成；完整范围的 `--name-only` 结果只有该文件，差异为 47 行新增、26 行删除。

root 后续集成提交为 `4b607a8b79e732562d3cf6bdc9374762aeafab3e`。该提交与作者提交 `5a4c6061470756fdafbd6550dc1337bda5d4b8c8` 的 `apps/camctl/src` Git 树对象均为 `9c2b982aa4050c25646e40b6500ebad3a84a8661`，`apps/camctl/tests` Git 树对象均为 `8aa2d1537d87f2efbfefb508a9aacf816b769fb8`。这证明两棵树的内容身份相同；本目录仍只有 c5a 基线加 dirty 测试文件的那次运行，不声称主树另有一次 43 项结果。

## 观测范围与尚未迁移的边界

新增的 `_QueryProbe` 通过对 `sqlite3.connect` 做猴子补丁（monkeypatch，运行时替换）观察只读状态库连接。它只在连接参数等于 `repository.path.resolve().as_uri() + "?mode=ro"` 且 `uri=True` 时注入观测连接，并等到该连接执行精确的 `BEGIN` 才挂接进度回调（progress handler）、SQL 跟踪（SQL trace）和行工厂（row factory）。它给出这次匹配路径上的分页查询与连接收场证据；它不是 `HistoryRepository` 的公开观测端口，也不能覆盖等价但写法不同的 URI、连接工厂或事务启动形式。

本文件的其他用例仍直接检查 `selection._cache`，并 monkeypatch 私有 `_CACHE_LIMIT`；读取失败用例仍替换私有 `repository._connect`。这批缓存上限、缓存内部状态和连接故障注入没有因为本次文件通过而迁移完成。该分页节点本身也仍观察 `report_entity_changes`、索引名称、SQL 文本、查询计划和 SQLite VM 步数。因此，43 项通过只说明记录中的这个文件快照通过，不表示全部目标均已转为公开契约测试，也不表示未覆盖故障分区已完成。

## 文件完整性

`manifest.json` 记录五个 gzip 载荷各自的来源、原始/压缩 SHA-256 与字节数。gzip mtime 固定为 0；归档时逐件核对解压后字节，并解析了 JSON 与 XML。没有复制数据库或源码树，归档过程没有运行测试。
