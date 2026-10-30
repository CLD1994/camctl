# 报告选择的固定历史样本

`report-selection-4200.sqlite.gz` 是一份完整状态库的压缩副本。对应清单保存该副本的摘要、格式、实例身份、目录绑定、日志模式、完整历史边界和实际行数。样本包含 3 个计划、4206 个动作（3／4200／3）和 4 个产物；读取测试每次解压独立副本并以只读连接核验。清单里的事务和事件数量描述这份固定文件，不限制今后合法的生产事务编排。

此样本只供不访问实际媒体文件的历史读取测试使用。元数据中的 `/srv/camctl` 路径不表示这些文件存在或可用于拍摄、取回和主机集成。生成方式的证据来自生成入口及运行记录，不能仅从数据库内容推断。真实受理、调度和拍摄来源仍由同目录测试的 `test_real_source_preparation_preserves_large_report_selection` 验证；该见证保留原有规模和输入，不以样本读取替代生产者覆盖。

使用 Python 3.11，在仓库根目录同步资源后生成一个**尚不存在**的新输出目录：

```bash
/workspaces/camctl/apps/camctl/.venv311/bin/python apps/camctl/scripts/sync_resources.py
PYTHONPATH="$PWD/apps/camctl/src:$PWD/apps/camctl/tests" /workspaces/camctl/apps/camctl/.venv311/bin/python -m integration.support.report_selection_corpus --output-dir .local/new-report-selection-corpus
```

生成器使用测试侧真实业务入口，关闭实际工作线程、检查点和连接，再核对完整库、生成 gzip 与清单；它不会覆盖已有输出目录。生成结果包含新的数据库身份和操作键，因此每次字节摘要可以不同；清单必须与生成的 gzip 一起使用。更新受版本控制的样本前，将新目录两文件作为一个整体审查和替换，运行真实来源见证与完整报告选择文件，并保存命令、日志及摘要。

当正式状态库存储格式、事件／枚举登记或报告历史读取契约变化时，先核对既有样本能否被当前正式读取器完整验证、结果及覆盖是否仍符合契约；不兼容或生成输入、规模及目标契约发生变化时重新生成。无关的私有实现调整不自动使样本失效；也不能因为旧样本仍可解压就跳过兼容性和业务行为核对。
