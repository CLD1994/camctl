# 拍摄能力与媒体结果客户端执行计划

> 执行方式：使用 superpowers:executing-plans，由当前 Agent 在 `codex/client-mvp` 分支逐项执行，不派遣 subagent。

**目标：** 客户端支持设备声明的三种拍摄动作，可靠接收图片与视频，以摘要、分页产物和图片预览呈现结果。

**架构：** 公共 Schema 是动作与报告结构的权威来源，生成类型并由共享分类函数支持各入口。文件继续共用已有保存、核验及恢复事务；展示层单独派生产物及交付视图，不改变报告历史。媒体响应从有效报告确定内容类型。

**技术：** TypeScript、React、Node.js、SQLite、Ajv、Vitest、Playwright。

**规格：** [全局拍摄](../../architecture/camera-capture.md)、[报告](../../architecture/report-format.md)、[客户端浏览](../../client/result-browsing.md)。

## 已确认约束

- 本计划仅实现客户端所需功能，不实现嵌入式驱动；真实设备命令与执行仍需设备联调。
- 设备能力精确匹配；三种拍摄的参数由设备规则决定，不推测统一拍摄参数。
- 取消的单张和延时摄影可保留正式产物，录像取消仍遵守录像协议。
- 上传、关联、核验、媒体解码分别表达；已有演示数据保留。
- 未知、缺失、失败与未完成不得互相替代。跨页选择按 ID 保留，取回动作不借用其他交付的成功。
- 文件划分、内部函数名和提交划分为实现建议；行为与验收条件是硬性要求。

## 任务一：公共协议与编辑能力

建议修改 `apps/client/src/shared/plan.ts`、`action-params.ts`、生成报告类型、`apps/client/src/domain/reports.ts`、`apps/client/src/web/Editor.tsx`、`BuiltinFields.tsx`；审计所有 `camera_record` 分支，区分录像专属规则与通用拍摄规则。

输入：全局 Schema 和启用能力目录。输出建议接口 `isCameraAction(value: unknown): value is CameraActionType`，由 Schema 的拍摄枚举派生。`ACTION_TYPES` 同样从 Schema 派生；不能复制完整清单。

- [x] 写 `apps/client/tests/unit/capture.test.ts`：三种拍摄的设备/策略/时间规则、照片和延时来源取回；取消保留的报告通过，未开始携带产物和非法成功组合拒绝。

```ts
expect(validatePlan(planWithPhotoAndObtain, photoCapabilities)).toEqual([]);
expect(() => validateReport(canceledCaptureWithOutputs)).not.toThrow();
```

- [x] 执行 `npx vitest run apps/client/tests/unit/capture.test.ts`，确认当前硬编码仅录像导致失败。
- [x] 运行 `node --import tsx apps/client/src/domain/generate-report-types.ts`，更新分类、计划校验、报告语义、设备选择、参数表单、时间/策略必填及取回来源选择。新动作的有限尝试使用已有预算校验，历史只接受合法状态转换。
- [x] 重跑本文件及 `npm run test:unit`；对旧测试中“本版不支持照片”的前提按新契约更新，保留未知动作拒绝测试。
- [x] 检查后提交此任务。

## 任务二：媒体文件接收与读取

建议在 `apps/client/src/shared/media.ts` 集中定义可内联的图片/视频 MIME 类型；`apps/client/src/server/files.ts`、`http.ts`、`models.ts` 和前端导入入口使用已有文件流程。内部已有视频存储命名可以保留以保证现存数据可读，不复制数据库和核验状态机。

输入：完整交付文件名、字节、有效报告。输出：核验状态、读取接口的正确内容类型与原始字节；增加媒体路由时保留原视频路由。未知或不支持内联的类型只能下载，图片也不得跳过 `openVideo` 当前的保存、映射及可读性检查。

- [x] 增加 `apps/client/tests/integration/media.test.ts`，用 PNG 字节和合法图片报告验证先文件后报告、先报告后文件、错误副本补发、重启读取与响应类型。

```ts
expect(response.headers.get('content-type')).toContain('image/png');
expect(Buffer.from(await response.arrayBuffer())).toEqual(pngBytes);
```

- [x] 运行该测试看到旧接口错误的 `video/mp4` 或不接受图片分类。
- [x] 统一图片与视频上传、状态恢复和内容响应；只有明确允许的 MIME 类型可内联，设置 `nosniff`。未知类型保留为附件。
- [x] 运行媒体、文件、HTTP 集成测试，检查重复输入、失败恢复和 Range 原行为。
- [x] 检查后提交此任务。

## 任务三：动作摘要、分页与图片查看

建议拆出 `apps/client/src/web/result-model.ts` 派生业务视图和 `MediaResults.tsx` 管理分页与预览；`Records.tsx` 保留动作与计划入口。

建议纯函数输入为动作、全部计划、本地文件，输出按 `output_id` 聚合的文件与每次交付；取回动作仅聚合自身 deliveries，拍摄动作关联全部有效交付。类型由报告决定，未知归其他文件。摘要计数按产物去重，异常交付不因已有好副本而消失。

- [x] 单元测试覆盖同产物多交付、取回范围隔离、未知类型、取消有产物、无快照不能断言无文件、聚合数量；浏览器测试覆盖默认收起及 100 张图片的分页、过滤、选择与大图。

```ts
expect(model.products).toHaveLength(1); // 同产物两次交付
await expect(page.getByRole('button', {name: '展开动作 延时摄影'})).toBeVisible();
await expect(page.locator('img[data-thumbnail]')).toHaveCount(12);
```

- [x] 先运行新增测试确认失败，再实现摘要、技术详情折叠、每组 12 项分页、过滤和按 ID 选择。
- [x] 实现受核验约束的图片与视频展示，弹窗只加载当前图片，焦点恢复、Escape/方向键、跨页导航及文件失效提示。
- [x] 全选当前页和准备取回所选明确显示范围；所有操作继续进入既有草稿流程。
- [x] 运行新增测试及全部浏览器测试；既有展开定位根据默认摘要入口调整，不能删除原业务断言。

## 端到端检查与交付

- [x] 沿“能力目录→编辑→保存/恢复→导出→报告→文件→核验→摘要/预览→后续取回草稿”逐层检查数据和错误传播。
- [x] 运行 `npm run test:unit`、`npm run test:integration`、`git diff --check`，构建已包含类型检查。
- [x] 使用隔离数据浏览桌面与 390px 窄屏，确认没有横向溢出、收起时不加载图片、分页保留、图片放大可操作。
- [x] 更新验证文档，提交实现，验证本地启动与最新构建。原日常客户端数据不清空；容器与真实设备验收由用户执行。
## 执行与验收结果

2026-09-16：任务一已提交；任务二、三共同交付。文件错误副本补发通过共用文件流程的既有集成测试验证，新增图片测试补充两种到达顺序、恢复、内容类型与原始字节。实现时按实际组件数据流拆分，没有改变已确认的行为契约。

最终 `npm test`：14 个单元测试文件共 515 项通过，14 个集成测试文件共 217 项通过；类型检查与生产构建通过。桌面与 390px 窄屏的隔离数据验收完成。日常服务已重启并验证数据保留，完整范围见[客户端验证](../../client/verification.md)。
