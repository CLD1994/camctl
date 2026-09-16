# 客户端表单与设备说明实施计划

> 执行方式：按用户要求，由当前 Agent 使用 executing-plans 在当前分支逐项实施，不派遣 subagent。

**目标：** 简化字段显示，支持动作折叠和参数联动，并提供面向普通使用者的设备说明页。

**架构：** 已启用的 JSON Schema 继续作为唯一规则来源；纯函数生成有限合法组合，编辑控件与设备指南共享派生结果。折叠状态仅存在于网页，不进入草稿协议；保存、预设、导出和恢复继续使用现有数据通路。

**技术：** TypeScript、React、Ajv、Vitest、Playwright。

**规格：** [公共编辑规则](../specs/camctl/client-editing.md)、[设备使用说明](../specs/camctl-client/device-guide.md)。

## 已确认约束

- 用户要求先更新规格，再修改代码；不使用 subagent；本地部署，容器由用户验证。
- 缺省、非法、未完成、未知与合法状态分别处理，派生组合不修改任何用户输入。
- 后端导出前完整校验保持权威；JSON 与预设导入的非法值须可修正。
- 测试从状态表独立推导；单元测试不访问文件，真实浏览器与保存属于集成测试。

下列模块与接口是实施建议，可依照实际数据流调整，行为契约不可随意改变。

## 任务一：同源组合推导

建议新增 `src/web/parameter-options.ts`，公开 `parameterOptions(parameter)` 返回可枚举的字段与合法行，或带原因的不可枚举状态；`compatibleValues(catalog, params, field)` 返回与其他已填字段兼容的候选值。

- [x] 写 `tests/unit/parameter-options.test.ts`：空选择、双向限制、非法组合单字段修正、缺省与 null/false/0、引用与条件、不可枚举及上限。
- [x] 运行 `npx vitest run tests/unit/parameter-options.test.ts` 确认新行为缺失。
- [x] 实现完整候选枚举并使用现有 Ajv 配置校验整个对象；按参数对象缓存结果；不以忽略错误关键词代替完整校验。
- [x] 同命令验证通过，审计无输入写入、无相机专属规则、无未知到空集合的折叠。

关键验收示例：

```ts
expect(compatibleValues(catalog, {type: "demo", resolution: "4K"}, "fps")).toEqual([30]);
expect(compatibleValues(catalog, {type: "demo", fps: 60}, "resolution")).toEqual(["1080p"]);
```

## 任务二：表单和折叠

建议修改 `src/web/Editor.tsx` 和 `src/web/style.css`，在字段组件统一处理必填、可选、值标签与非法值保留。动作容器统一处理折叠；新增动作展开，删除同步调整显示状态。

- [x] 更新已有“通过表单选择非法组合”的集成场景为选项联动契约；新增 JSON 非法输入保留与修正场景。
- [x] 新增折叠保留未完成内容、删除后折叠对应、必填无省略和字符串选项显示的浏览器断言；运行定向集成测试观察失败。
- [x] 实现字段显示、同源联动和折叠。无法推导的枚举提供明确 JSON 入口；不自动替换字段。
- [x] 验证表单、JSON、预设、保存刷新及导出错误路径；折叠动作仍显示问题数量。

```ts
await page.getByLabel("分辨率 (resolution)").selectOption("0");
await check(page.getByLabel("帧率 (frame_rate_fps)").locator("option")).toHaveText(["请选择", "30"]);
```

## 任务三：设备指南与整体交付

建议新增 `src/web/DeviceGuide.tsx`，接收当前已启用能力说明；App 保留重新加载和失败处理，展示交给指南组件。复用任务一的目录，不在说明页另写规则。

- [x] 新增浏览器场景验证任务卡片、合法组合表、折叠技术详情与空状态；观察旧页面不满足契约。
- [x] 实现指南布局与响应式样式，使用说明原有名称和描述，不编造真实设备能力。
- [x] 运行 `npm test`（包含构建、单元和集成）；检查完整差异与端到端数据流。
- [x] 本地页面检查桌面和窄屏，刷新运行中的应用，更新验证文档，提交本次修改。

## 横切检查

能力重新加载 → 新规则目录 → 表单选项与指南同时更新；已有参数不写入。JSON／预设 → 原样草稿 → 不合法值提示 → 用户修正 → 自动保存 → 后端导出校验。未完成输入 → 折叠仍可见问题 → 展开原文恢复 → 删除仅影响指定动作。草稿恢复不依赖任何显示状态。
