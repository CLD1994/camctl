# 有界报告字段决策

基线：implement_camctl 2329dffd。

字段出现条件继续由 report-dependencies.json 唯一定义。新增纯投影入口返回有限字段与按公开字段路径索引的延迟数组决定：省略、保留空数组、空时省略。条件依赖的事实缺失仍报错。投影结果不持有读取资源；生成器按决定逐项读取，保持 H、排序和编码不变。完整投影及 public_changed 接口不变。

范围包括现有 deferred 登记，错误详情扩展保持其已有独立契约。取消项不再在生成器比较目标动作类型。修正取回目标撤回数组会改变旧错误报告的 JSON 字节；不变的合法字段保持原顺序。

## 实施

- [x] 纯投影 API 与语义单元测试：条件、缺事实、嵌套路径、空策略、不展开集合。
- [x] 生成器消费决定并保持逐项读取，不新增集成测试。
- [x] 局部单元回归、独立代码评审与验证记录；全量限制见下文。

## 接口与验证边界

`project_bounded(ProjectionInput)` 返回 `BoundedProjection`（对象整体不适用时返回 `OMIT`）。`finite` 是有限公开字段；`deferred` 用相对公开路径定位 `ArrayEmission.OMIT / KEEP_EMPTY / OMIT_EMPTY`。延迟成员必须逐对象投影，不允许在已物化数组内部残留延迟标记。

`NodeGate.omit(..., projection_decision=ArrayEmission.OMIT)` 表示条件省略，不宣称读取耗尽。空集合省略仍提供实际 `ReadEnd`；保留空集合仍打开并耗尽数组。生成器保留读取资源所有权。

全部 9 个既有延迟字段已审计：plan.actions、action.outputs/deliveries 空时省略；obtain_result.failures、delete_result.items、cancel_result.items、cancel_item.withdrawals、video_coverage.checked_output_ids、video_default_scope.output_ids 保留空数组。父级 result/coverage 合法省略时不产生后代决定。错误详情增长字段仍使用原独立展开契约。

2026-11-07，Linux / Python 3.11.17：

- 新 API 首轮 5 failed / 1 passed（缺入口），实现后 6 passed；评审反例首轮 4 failed，修复后通过。
- 最终 contracts + reporting：1191 passed / 1 skipped。命令：`PYTHONPATH=apps/camctl/src python -m pytest apps/camctl/tests/unit/contracts apps/camctl/tests/unit/reporting -q`。
- `capture/test_unknown_read_exhaustion.py` 独立运行 5 passed；全 unit 同进程收集时其隔离守卫拒绝已加载 camctl 模块。
- 排除此隔离文件后的全 unit 运行仍有多项失败，在约 64% 处中止，未完成全量验收。改为 `-x` 取得首个精确失败：`bootstrap/test_capture_preparation_handoff_flow.py::test_flow_waits_for_original_preparation_result_save_then_dispatches_start`，CaptureRuntime 测试替身缺 `pending_segment_inspection_results`。其他中止前失败尚未逐项归因。
- 既有 unit 目录中部分模块导入真实包资源；为运行它们先调用 `apps/camctl/scripts/sync_resources.py`。目录名不表示所有既有用例均满足纯隔离定义；本次新增 API/门禁单测资源来源在内存隔离。
- 不新增、不运行集成测试，未执行数据库到最终文件的真实组合验证；不以单元通过代替该证据。
- gpt-6.1-sol 独立评审与修复复核通过；`git diff --check` 通过。

- 干净基线 2329dffd 使用同一 `-x` 命令复现相同首个失败（1 failed / 127 passed），确认该 CaptureRuntime 替身问题并非本次改动引入。
