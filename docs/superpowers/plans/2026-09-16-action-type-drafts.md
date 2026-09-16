# 动作类型独立草稿与字段展示实施计划

> 执行方式：当前 Agent 在现有 `codex/client-mvp` 分支内使用 executing-plans 逐项完成，不派遣 subagent。

**目标：** 动作类型可无损来回切换，取回筛选按来源显示，普通选填字段直接展示且必填标签统一。

**规格：** [客户端计划编辑](../../architecture/client-editing.md)、[数据保存与恢复](../../client/storage-and-recovery.md)。

## 已确认契约和项目规则

- 名称、执行时间共用；其他动作字段与其未完成输入按类型保存。未选择和未知类型也不得丢失数据。
- 当前协议文本是唯一执行意图；其他类型资料仅属于草稿。所有保存、比较、恢复沿用同一版本事务。
- 普通选填字段保持缺省与空字符串、0、false、null 的区别；禁止渲染时写默认值。
- 本计划来源不提供产物筛选，只有指定动作实例展示附加开关；不改变协议合法性。
- 先失败测试再实现；单元测试隔离真实 IO，浏览器与数据库属于集成测试。
- 整份 JSON 结构替换须明确确认清除其他类型内容，参数 JSON 不清除；查看不写入。
- 本地部署由 Agent 验证，容器部署由用户执行。

## 建议实现与执行顺序

文件和内部接口是预估，可按实际数据流调整；以上契约为硬性要求。

### 1. 草稿数据闭合

建议 `DraftContent` 增加按动作下标保存的非当前类型列表，每项包含原始 type（可缺省）、独立字段对象、相对动作路径的 pending。类型匹配用 JSON 值语义，不依赖可读名称或目录顺序。活动类型从列表移出，防止两份权威内容。

- [x] 在 `apps/client/tests/unit/web-editing.test.ts` 添加失败用例：首次切换只剩公共字段、往返恢复对象与未完成输入、未知和缺省类型、删除前项后内容不串位、普通字段编辑和追加保留资料、整体 JSON 替换需要显式标志。
- [x] 运行 `npm run test:unit -- apps/client/tests/unit/web-editing.test.ts`，确认失败来自缺少切换行为。
- [x] 在 `apps/client/src/server/models.ts`、`apps/client/src/web/editing.ts` 实现模型和纯转换；`setValue` 保留编辑资料，`removeAction` 同步移动对应资料，整份 JSON 替换显式清除。
- [x] `apps/client/src/web/session.ts` 的 `sameContent` 比较全部编辑资料；`apps/client/src/server/application.ts` 检查资料结构，复用现有原子保存。
- [x] 单元验证保存比较包含资料；集成验证重开数据库能恢复、结构非法拒绝、未选类型 pending 不阻止当前报告导出且导出无元数据。

验收示例：录像中输入 `max_delay_ms` 为 `1e`，切换报告后正文为 `{name,type:"report_status",scheduled_at?}`，可合法导出；切回录像恢复 `1e` 且禁止录像导出。

### 2. 表单接入

- [x] 浏览器失败用例覆盖取回四种来源：只有 action_instance_id 显示筛选；切回本计划来源不残留 output_ids。
- [x] 浏览器失败用例覆盖类型往返、刷新后恢复、没有不适用字段提醒、动作组/数值/布尔选填直接显示、清空与明确值分离，以及每类必填标签。
- [x] `apps/client/src/web/Editor.tsx` 的类型选择器调用转换；整个动作有 pending 时禁用切换；整份 JSON 实际编辑前确认资料替换。
- [x] `apps/client/src/web/BuiltinFields.tsx` 按识别的精确来源模式显示附加筛选；JSON 原值继续提供诊断与显式移除。
- [x] `apps/client/src/web/Fields.tsx` 普通字段一直显示，选填已有值/pending 提供清空；保留全部 JSON 值类型。
- [x] 更新既有验收中被新规格替代的交互步骤，保留非法 JSON 修正路径测试。

### 3. 复核与交付

- [x] 审计草稿生产者与消费者：新建、复制请求、历史追加、表单、参数 JSON、整份 JSON、删除动作、自动保存、失败核实、导出。
- [x] 执行 `npm run test:unit`、`npm run test:integration -- --maxWorkers=1`（包含类型检查和构建），失败先定位。
- [x] 使用隔离数据检查桌面和手机布局；更新验证文档。
- [x] 提交当前分支并重启本地服务，确认新资源可访问；不推送、不合并。
