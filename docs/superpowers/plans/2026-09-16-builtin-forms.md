# 内置动作协议表单实施计划

执行方式：依照 executing-plans 在当前分支直接实施，不派遣 subagent。

目标：全部内置动作通过表单表达协议，同时保留非法及未完成输入的诊断与恢复。

规格：[客户端计划编辑](../specs/camctl/client-editing.md#内置动作与公共字段)。协议来源为 plan-input、obtaining-outputs、task-cancellation、output-cleanup 和 status-sync 各专题。

技术：React、TypeScript、现有草稿编辑函数、Vitest 与 Playwright。下述文件划分为实现建议，不限制基于实际依赖调整。

## 约束与端到端边界

草稿是唯一编辑权威；表单、JSON 和历史追加共用它。打开或切换视图无写入；显式改选模式才替换指定对象。未完成输入阻止覆盖及导出；额外字段和非法值保留诊断。校验和导出继续经过现有共享校验器与后端，不增加宽松通道。测试使用临时数据，不能修改用户日常草稿。

## 任务

- [x] 添加浏览器失败测试：取回隐藏所属组但能移除非法旧值；四种来源生成准确字段，组模式不保留筛选；动作筛选数组及删除数组保留空值/重复值错误；取消四种目标；报告三种范围及非法起点；JSON、刷新、类型切换和未完成输入保留。
- [x] 运行 `npm run test:integration -- --maxWorkers=1 tests/integration/browser-editing.test.ts`，确认新测试因未实现表单失败。
- [x] 提取现有 Field/JsonField 为共享控件；实现 BuiltinFields。引用模式使用精确键集合识别；用户改选用 `setValue(content, referencePath, reference)` 替换引用，对组来源明确移除筛选。未知结构显示 JSON，不隐式转换。ID 列表逐项编辑，保持 JSON 类型及原始输入。
- [x] ActionEditor 接入内置表单、动作组禁止分支、公共时间必填提示及旧值修正。报告起点从传入的 reports/coverage 派生，调用共享校验器保持选项与导出一致。
- [x] 运行针对性测试，定位并修复失败；检查历史追加入口与既有测试，不修改已导出请求。
- [x] 运行 `npm run test:unit`、`npm run test:integration -- --maxWorkers=1`、构建及 `git diff --check`。审查数据从表单到保存、刷新、校验、导出的闭合路径，更新验证记录并提交当前分支。

