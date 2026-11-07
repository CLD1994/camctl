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

## 分段历史固定样本

`segment-history-256.sqlite.gz` 和 `segment-history-2048.sqlite.gz` 是两个独立完整状态库，分别包含 256 和 2048 个分段成员，以及 254 和 2046 个缺口。它们由正式拍摄装配、结果分页、文件事实和分段仓储生成；对应 JSON 清单记录库和压缩文件的摘要、分段引用及完整历史边界。生成器在退出拍摄资源后检查点、核对并关闭数据库连接，再生成压缩副本。样本中的目录绑定只用于记录生成时的状态，不代表媒体文件可用。

读取测试将样本解压为独立副本，以只读连接核对格式、完整性及数量，关闭该连接后启动独立受限业务进程。业务进程按每页最多 7 项读取全部成员和缺口，并逐块写出快照；组外核对全部分页与快照行、数据库身份集合和成员顺序。预算测试与开启 `tracemalloc` 的增长诊断分别启动业务进程；诊断数据用于分析，不作为整组预算峰值或增长验收结论。

使用 Python 3.11 并同步资源后，可分别生成一个尚不存在的输出目录：

```bash
PYTHONPATH="$PWD/apps/camctl/src:$PWD/apps/camctl/tests" /workspaces/camctl/apps/camctl/.venv311/bin/python -m integration.support.segment_history_corpus --output-dir .local/new-segment-256 --count 256
PYTHONPATH="$PWD/apps/camctl/src:$PWD/apps/camctl/tests" /workspaces/camctl/apps/camctl/.venv311/bin/python -m integration.support.segment_history_corpus --output-dir .local/new-segment-2048 --count 2048
```

每次生成的数据库身份和操作键均不同。更新固定样本时，将同次生成的压缩文件与 JSON 清单一并替换，运行真实来源见证和完整分段历史测试。存储格式、事件登记或历史读取契约变化时，核对既有样本与正式读取器的兼容性，并按需要重新生成。
