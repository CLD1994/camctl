# 草稿并发冲突实施计划

> **给执行 Agent：** 使用已选定的 `superpowers:subagent-driven-development` 逐任务实施并独立审查。执行前阅读本计划及规格；步骤使用复选框跟踪。

**目标：** 多个页面编辑同一草稿时，清洁页面自动同步；存在不同修改时，用户比较完整内容并选择另存、采用或明确替换，保存与创建结果未知时能够继续核实。

**架构：** 后端继续使用事务内版本检查保存原草稿，以独立创建事实核实另存。浏览器区分可靠观察、编辑资格、保存资格和各次固定操作；完整版本接纳先准备内容及动作显示映射，再共同提交。差异面板消费这些状态，不自行决定操作成功。

**技术栈：** 现有 TypeScript、React、`@base-ui/react`、Node.js `node:sqlite`、Vitest 及 `@playwright/test`。复用精确 JSON、自动预览和数据库事务，不增加依赖。

**规格：** [设计总览](../specs/2026-10-11-client-draft-conflicts-design.md)、[自动保存与并发冲突](../../client/storage-and-recovery.md#草稿的自动保存与并发冲突)、[冲突交互](../../client/page-interactions.md#草稿并发冲突交互)、[并发验收](../../client/acceptance.md#并发编辑与冲突解决)、[动作显示状态与追加接纳](../../architecture/client-editing.md#动作显示状态与追加接纳)、[自动预览](../../client/automatic-previews.md)。行为规则以这些责任专题为权威，本计划规定实施顺序和证明方式。

**状态：** 任务一至七的实施、逐任务独立审查、整体审查及统一修复后的限定复审均已完成。最终复审没有残留或新增阻断项，完整门禁、证据层次及现场运行范围见客户端验证记录。

## 全局约束

- 自动保存延迟保持最后一次修改后的 `450` 毫秒。普通保存期间允许继续输入，较早回执不得覆盖后续输入。
- 完整版本共同包含 `text`、`pending`、`actionVariants`、`parameterVariants`、`objectVariants` 和 `automaticPreviews`。沿用 `sameContent`、`cloneClientJson`、`stringifyJson` 和 `parseClientJson`；不以 `Number` 投影、正文或动作数量代替完整事实。
- 原草稿写入在同一事务检查存在、尚未导出及版本一致；明确替换也使用这一检查，不增加绕过版本检查的入口。
- 选择期间保留整份版本，不进行字段合并。未完成或业务非法的内容可以保存，编辑资料结构非法则明确拒绝；导出校验继续承担独立责任。
- 请求在途、读取不可用、旧结果不可证明但已不能改变目标，以及可靠的已导出或已删除事实分别建模。导出、删除和追加不借普通冲突选择结束自己的结果核实责任。
- 独立新增动作的身份不可复用；固定追加操作的前后端重算使用同一分配依据。旧草稿没有身份资料时，读取、查看及完整接纳不被动补资料。
- 另存尝试身份、完整原输入及首次创建结果共同保存；同次重试不生成新身份，不复建已删除目标，不回写首次创建快照。
- 并发选择及另存异常按[领域状态表](../../client/storage-and-recovery.md#另存的确认与重试)和[采用与替换异常](../../client/storage-and-recovery.md#采用与替换的异常处理)实施。异常自动显示共用“重试／放弃”弹窗；正常成功直接进入目标。完整差异仍提供三个版本选择，普通保存期间仍允许继续输入。
- 请求、核实及待决定分别建模；另存输入从选择开始保持固定，终态结束创建核实但不解除待决定限制。放弃只停止所选安排或本地处理，完整输入交回原会话，不清除独立责任，不撤销真实写入，不使冲突或终态获得保存资格。
- 每个重试、放弃及共同接纳绑定身份、责任代次、本页版本、完整输入与观察资格；外部 prepare／validate 后再次复核，所有拒绝都发生在首个共同 commit 前。旧 preparedEdit、timer、按钮及导航回调不能绕过领域入口。
- 浏览器未保存资料只在当前会话保留，刷新或关闭使用既有提醒。测试使用隔离临时目录和独立浏览器上下文，不修改用户 `data/` 或依赖 `localhost:4310` 的现场草稿。
- Node.js 使用 `apps/client/package.json` 的 `>=24.16.0 <25`。本次规划环境为 Windows PowerShell、Node.js `v24.16.0`；已存在的锁定依赖可直接运行。单元测试不访问真实文件、网络或 SQLite，这些边界由集成测试验证。
- 文档与说明以中文完整句子表达，保留准确标识符。所有任务保留工作区已有变更；只实施本计划范围，不使用 `tabbit`。

## 审查重点

以下五个接缝是跨任务审查重点，并已经分别安排反例测试。

1. 旧保存的证据被后续写入取代，即使当前内容等于本页内容，也不能认领旧保存；证明旧条件写入不能再改变目标后，用户仍能明确采用。由任务三、四和七验证。
2. 另存创建已成功，但目标在转交前被编辑、导出或删除：保持创建事实及固定输入；目标终态只结束核实，重试按说明发起独立新另存，放弃将输入交回原会话并结束所选责任。由任务二、五、六和七验证。
3. 两页新增身份及完整替换后的身份分配不能碰撞；追加预期、服务端重算和接纳必须使用同一固定身份。由任务一、四和七验证。
4. 正文相同而私有资料不同、数字投影相同而词元不同，都属于完整版本差异；差异展示、资格、另存、采用和替换共同保留这些事实。由任务二至七验证。
5. 读取失效、旧观察、旧按钮回调及显示准备失败不能消费确认或先清除本地资料；异常关闭和导出导航不能解除待决定限制，采用／替换放弃须保留真实写入与完整输入。由任务三至七验证。

## 实施建议与责任划分

以下文件划分、内部名称、签名及任务提交边界是实施建议。执行者可以按真实数据流调整，但须同步所有调用者和测试，并保持权威规格的不变量；需要改变业务语义时停止相关任务并报告。

| 建议落点 | 责任及预计改动 |
| --- | --- |
| `apps/client/src/shared/automatic-previews.ts` | 保持完整比较和独立副本规则；让新增动作的纯计算消费固定分配依据。 |
| `apps/client/src/server/models.ts` | 定义客户端内部创建事实及查询结果，不新增独立 `actionIds` 字段。 |
| `apps/client/src/server/application.ts`、`http.ts` | 同次另存的事务创建、查询及追加身份参数。明确替换复用 `saveDraft`。 |
| `apps/client/src/web/api.ts` | 把完整可用状态转换为可靠目标观察，适配创建及核实接口，保持精确 JSON。 |
| 新建 `apps/client/src/web/draft-conflicts.ts` | 只计算观察分类及完整选择快照资格，不发请求或操作界面。 |
| `apps/client/src/web/session.ts`、`action-ui-identity.ts` | 保存与选择的固定责任、整份接纳及局部显示状态的共同提交。 |
| 新建 `apps/client/src/web/draft-copy.ts` | 固定一次另存的输入、身份、创建核实和转交阶段。 |
| 新建 `apps/client/src/web/DraftConflictDialog.tsx`、`draft-conflict-presentation.ts`，共用异常弹窗模块按现有组件边界安排 | 完整双方摘要、技术详情及确认；共用异常呈现消费领域分类，负责两个按钮、自动打开、非破坏性关闭及键盘焦点。 |
| `apps/client/src/web/App.tsx`、`Editor.tsx`、`editing.ts`、`followup.ts`、`style.css` | 观察适配、按钮资格、导航、追加依据及显示接纳。沿既有模式定向修改。 |
| 新建对应 `.test.ts`，扩展现有会话、身份、追加及 HTTP 测试 | 单元层证明分类与状态；集成层证明真实后端、HTTP、页面和双浏览器协作。 |
| `docs/client/verification.md` 与本计划 | 实施后记录日期、环境、命令、范围及结果；逐项更新进度。 |

无需新数据库表或改变 `schema_version`：建议以现有 `records(namespace,id,value)` 的专用 namespace 保存不可变创建事实。`Store.transaction` 已执行 `BEGIN IMMEDIATE`，事务回调保持同步，不跨越网络或文件副作用。SQLite、Base UI 弹窗、现有观察顺序管理及 UUID 已覆盖所需通用机制；自有代码限于草稿业务分类、固定依据和接纳适配。

## 任务顺序与门禁

任务一和二可以分别审查；任务三消费二的观察类型，任务四依赖一和三，任务五依赖二和四，任务六依赖四和五，任务七验证整个组合。并行实施时分别指定文件写入权，`models.ts`、`application.ts`、`App.tsx` 等共享文件的修改顺序由主 Agent 统一协调。

生产实现及缺陷修复执行“先失败测试、确认失败原因、最小实现、通过针对测试、独立检查契约”的循环。测试失败必须来自待实现行为，不能以语法错误、缺少依赖或夹具损坏作为有效失败证据。任务七对已有实现的组合验收先记录新增测试实际结果；已经满足时不回退代码人为制造 RED，发现真实生产缺陷时才先用合法行为 RED 再作窄修复。提交拆分按任务建议组织；只有取得提交授权时才执行本地提交，不推断远端推送。

所有下列命令都从 `apps/client` 执行，根脚本命令明确标注例外。直接调用已有依赖避免当前 Windows 的包管理器启动入口另行解析或安装依赖；这些调用分别对应 `package.json` 的类型检查、构建及测试职责。集成测试前必须构建当前网页。

## 任务一：固定新增动作身份并闭合追加重算

**预计文件：** 修改 `src/shared/automatic-previews.ts`、`src/web/editing.ts`、`Editor.tsx`、`followup.ts`、`session.ts`、`App.tsx`、`src/server/application.ts`、`http.ts`。扩展 `tests/unit/automatic-previews.test.ts`、`web-followup.test.ts`、`action-ui-acceptance.test.ts` 和 `tests/integration/automatic-previews.test.ts`。

**消费：** 已有 `DraftContent`、`PreviewMetadata`、`AppendExpectation`、`AppendAttempt` 和 `VerifiedAppend`。

**产出建议：** `ActionAllocation` 是一次新增动作的固定依据。UUID 在用户操作准备时分配，纯转换和核实不随机分配。新增参数共同传到前后端。

```ts
export interface ActionAllocation { id: string }
export function createActionAllocation(): ActionAllocation;
export function appendContentAction(
  content: DraftContent, action: Record<string, unknown>,
  uniqueName?: boolean, allocation?: ActionAllocation,
): DraftContent;
export function appendDraftAction(
  content: DraftContent, action: EditObject, allocation?: ActionAllocation,
): DraftContent;
export function copyDraftAction(
  content: DraftContent, index: number, allocation?: ActionAllocation,
): DraftContent;
// AppendExpectation 保留 content、capabilityVersion，增加必填 allocation。
// AppendAttempt 保留既有字段，增加必填 allocation；VerifiedAppend 继承它。
// FollowOperation 即使基线没有元数据，也固定一次分配以供后来准备复用。
export class FollowOperation {
  readonly allocation: ActionAllocation;
  constructor(destination: string, action: Record<string, unknown>,
    transport: FollowTransport, allocation?: ActionAllocation);
}
// Application 的建议签名：
appendAction(id: string, revision: number, action: Record<string, unknown>,
  expectedContent?: DraftContent, capabilityVersion?: string,
  allocation?: ActionAllocation): Draft;
copyAction(id: string, revision: number, index: number): Draft;
```

`ActionAllocation` 在共享自动预览模块定义。`EditObject` 沿用 `editing.ts`，`FollowTransport` 沿用 `followup.ts` 的现有签名：`append(id, revision, action, expected?)`，不再额外增加第五个参数。`FollowOperation` 构造函数分配或克隆一次 `allocation`，所有重试和基线准备复用它。

- [x] **步骤 1：加入独立分配及固定重算反例。** 在现有自动预览单元测试使用已经存在的有效元数据夹具，加入下面的公开函数用例，并分别覆盖动作复制和旧完整内容替换后新增。

```ts
it("两个操作从同一版本新增不同动作，身份独立且重算固定", () => {
  const base = initializePreviewMetadata({ text: '{"name":"计划","actions":[]}' },
    "disabled", "base");
  const a = createActionAllocation(), b = createActionAllocation();
  const action = { name: "报告", type: "report_status", params: { scope: "full" } };
  const first = appendContentAction(base, action, false, a);
  const second = appendContentAction(base, action, false, b);
  expect(first.automaticPreviews!.actions[0].id)
    .not.toBe(second.automaticPreviews!.actions[0].id);
  expect(sameContent(first, appendContentAction(base, action, false, a))).toBe(true);
  const afterReplacement = appendContentAction(base, action, false, createActionAllocation());
  expect(afterReplacement.automaticPreviews!.actions[0].id)
    .not.toBe(first.automaticPreviews!.actions[0].id);
});
```

- [x] **步骤 2：确认失败。** 运行 `node node_modules/vitest/vitest.mjs run tests/unit/automatic-previews.test.ts tests/unit/web-followup.test.ts`。先接通声明使测试可运行，确认旧计数分配不能满足独立身份；保留失败证据。
- [x] **步骤 3：实现固定分配并逐入口传递。** `createActionAllocation` 使用既有 `crypto.randomUUID()`，由 Editor 的新增／复制入口及一次 `FollowOperation` 准备分配。`appendContentAction` 对已有元数据使用传入身份，不从 `namespace` 与 `next` 推测新身份；拒绝缺少分配依据或重复身份，保持输入不变。旧内容没有元数据时继续纯正文追加，不被动建立资料；`next` 保持合法兼容资料，不作为新增 UUID 的分配权威。

```ts
// 有元数据时的分配步骤；其余正文、分支及名称处理沿用既有转换。
if (meta) {
  if (!allocation?.id || meta.actions.some(entry => entry.id === allocation.id))
    throw new Error("新增动作身份缺失或已经使用");
  meta.actions.push({ id: allocation.id });
}
```

- [x] **步骤 4：闭合所有重算入口。** Editor 添加和复制分别分配一次，传给 `appendDraftAction(..., allocation)` 和 `copyDraftAction(..., allocation)`，显示准备不再分配。`FollowOperation.prepareExpectation` 使用 `this.allocation`，`AppendAttempt.allocation` 与 `expected.allocation` 必须相同；重试及新基线准备仍复用。`DraftSession.acceptAppend` 使用 `transition.allocation` 重算并验证与预期一致。`App.tsx` 沿用 `{ revision, action, expected }` 请求，HTTP 按下例读取固定依据。服务器 `copyAction` 及没有前端预期的独立 `appendAction` 在入口分配一次，再调用纯转换；有固定预期却没有分配依据的请求明确拒绝。

```ts
application.appendAction(String(req.params.id), req.body.revision, req.body.action,
  req.body.expected?.content, req.body.expected?.capabilityVersion,
  req.body.expected?.allocation);
```

`classifyAppend` 保持现有参数签名。存在元数据而没有固定 `expected` 及其 `allocation` 时，只能证明原基线或可靠能力派生基线；“已追加”的判定返回 `conflict`，不能随机重算或从 actual 反推身份。完全没有元数据时，仍允许按原纯正文追加规则重算。原先没有 expected 而证明有元数据追加的测试改为显式固定预期，并加入缺少预期的拒绝反例。
- [x] **步骤 5：证明追加与关联没有回归。** 在 `web-followup.test.ts` 给保存响应丢失及能力基线推进用例传固定 `allocation`，断言实际完整结果和转交里的身份相同；在真实 HTTP 集成测试检查请求依据、后端结果及 `acceptAppend` 重算一致。自动预览的 `sourceId`、`rename` 与派生身份继续对应来源；同一来源反复协调不换身份。运行相关单元测试，构建后运行 `tests/integration/automatic-previews.test.ts`，审查通过后进入任务四。

## 任务二：事务保存另存创建事实及核实接口

**预计文件：** 修改 `src/server/models.ts`、`application.ts`、`http.ts`、`src/web/api.ts`。新建 `tests/integration/draft-creations.test.ts`，扩展 `tests/unit/web-api.test.ts`。现有 `application.test.ts`、`http.test.ts` 的 `setup()` 是文件内帮助函数，不能直接导入；在新文件按同一真实组件模式建立隔离环境。

**消费：** `DraftContent`、`Draft`、`Store.get/set/transaction`、`Application.checkContent`、`copyDraftContent`、`coordinatePreviews`。

**产出建议：** 创建事实与当前目标分开。下面的类型定义在 `models.ts`，客户端按同一内部类型消费；不改公共计划协议。

```ts
export interface DraftCreation {
  attemptId: string;
  input: DraftContent;
  created: Draft;
}
export type DraftCreationResult =
  | { kind: "not_created"; attemptId: string }
  | { kind: "created"; creation: DraftCreation;
      target: { kind: "editable" | "exported"; draft: Draft } | { kind: "deleted" } };
// Application 新增：
createDraftCopy(attemptId: string, input: DraftContent): DraftCreationResult;
draftCreation(attemptId: string): DraftCreationResult;
// 内部 HTTP：PUT /api/draft-creations/:attemptId，正文 { content }；
// GET /api/draft-creations/:attemptId；两者返回 DraftCreationResult。
```

- [x] **步骤 1：加入真实事务反例。** 新文件使用 `mkdtempSync`、`Application`、显式 `store.initialize()`；`afterEach` 先关闭连接再删除已验证位于测试临时目录中的目录。以下测试的 `setup()` 在该文件定义，返回已初始化 Application。

```ts
import { afterEach, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, resolve, sep } from "node:path";
import { Application } from "../../src/server/application";
const applications: Application[] = [];
function setup(): Application {
  const directory = mkdtempSync(join(tmpdir(), "camctl-draft-creations-"));
  const app = new Application(directory);
  applications.push(app);
  app.store.initialize();
  return app;
}
afterEach(() => {
  for (const app of applications.splice(0)) {
    const directory = resolve(app.store.directory);
    app.store.close();
    if (!directory.startsWith(resolve(tmpdir()) + sep) ||
        !basename(directory).startsWith("camctl-draft-creations-"))
      throw Error("清理目标不属于本测试临时目录");
    rmSync(directory, { recursive: true, force: true });
  }
  vi.restoreAllMocks();
});
it("同次创建在目标删除后仍返回创建事实，不复建目标", () => {
  const app = setup();
  const input = { text: '{"name":"本页","actions":[]}',
    pending: { "": { kind: "json" as const, text: "{" } } };
  const first = app.createDraftCopy("attempt-1", input);
  expect(first.kind).toBe("created");
  if (first.kind !== "created") throw Error("没有创建事实");
  app.deleteDraft(first.creation.created.id, 1);
  const retried = app.createDraftCopy("attempt-1", input);
  expect(retried).toEqual({ kind: "created", creation: first.creation,
    target: { kind: "deleted" } });
  expect(app.store.all("drafts")).toHaveLength(0);
});
```

- [x] **步骤 2：确认失败。** 接通类型和方法声明后运行 `node node_modules/vitest/vitest.mjs run tests/integration/draft-creations.test.ts`，确认当前单次创建不能复用创建事实，而不是以未初始化数据或夹具结构错误作为失败。
- [x] **步骤 3：实现事务创建与查询。** 使用专用 namespace `draft_creations`。首次尝试在一次事务中校验固定输入、构造独立草稿、保存草稿及不可变事实；重复尝试先检查事实结构、身份和完整输入，结构合法但不同的完整输入返回 `creation_conflict`、HTTP `409`；输入的编辑资料结构非法时，沿用 `invalid_content`、HTTP `400`，不进入完整比较。两类拒绝均保留已有创建事实和目标记录。查询及重复提交共同返回创建事实和同一事务读取的实际目标。没有事实返回明确 `not_created`，数据库不可用或依据损坏则报告相应故障，不返回空结果。

```ts
// 事务中的核心顺序，buildDraft 仅构造内容，不另开事务。
return this.store.transaction(() => {
  const priorValue = this.store.get<unknown>("draft_creations", attemptId);
  if (priorValue !== undefined) {
    const prior = this.requireCreationFact(priorValue, attemptId);
    if (!sameContent(prior.input, input))
      throw new AppError("creation_conflict", "创建身份与固定输入不一致", 409);
    return this.readCreationTarget(prior);
  }
  const created = this.buildDraft(input);
  const creation = { attemptId, input: cloneClientJson(input), created };
  this.store.set("drafts", created.id, created);
  this.store.set("draft_creations", attemptId, creation);
  return { kind: "created", creation, target: { kind: "editable", draft: created } };
});
```

`buildDraft(input: DraftContent): Draft` 是从现有 `createDraft` 抽取的类内无持久化构造步骤；它先 `checkContent`，再 `copyDraftContent(input, randomUUID())`、能力协调及新 ID／时间构造。`requireCreationFact(value: unknown, attemptId: string): DraftCreation` 是类内运行时校验步骤，验证固定身份、完整输入结构及首次创建记录，损坏时报告依据故障。`readCreationTarget(creation: DraftCreation): DraftCreationResult` 是类内读取步骤；它验证实际记录，可靠存在时按 `exportedRequestId` 分类，记录确实不存在时返回 `deleted`。查询只有 `undefined` 才表示没有创建事实；`null`、`false` 或 `0` 等实际记录都是无效依据，不能触发重新创建。这三个辅助步骤只在本任务定义并审查，不成为跨组件通用框架。

- [x] **步骤 4：证明原子性及永久依据。** 分别让草稿写入和事实写入故障，断言事务回滚后两者均不存在；同身份重试只创建一次。用真实 Store 的定向 `spy` 注入一次写入错误，不替换事务实现。分别验证重启、停机完整目录复制后的恢复、两个 Application 连接及同身份并发 HTTP 请求。目标编辑、能力派生、导出和删除后返回原事实及实际分类；原草稿终态不影响核实。`deleteDraft` 仅移除目标草稿，不能删除创建事实。另将一次已创建尝试的依据分别置为 `null`、`false`、`0`，查询和重复创建都报告依据故障，原目标和该记录保持，不生成新草稿或覆盖依据。
- [x] **步骤 5：验证完整输入及 API 解析。** 每类私有资料分别改变而正文相同，同身份请求均拒绝；组合资料和原数字词元也参加比较。合法私有结构的未完成／业务非法内容允许创建，结构非法不留下记录。`web-api.test.ts` 用受接口约束的 fetch 替身验证 `result_unconfirmed`、完整解析及发送词元。真实 HTTP 测试分别验证 PUT 重试、GET 未创建、GET 已创建及故障响应，不能将不完整响应解释为未创建。
- [x] **步骤 6：通过针对门禁并审查。** 运行 `node node_modules/vitest/vitest.mjs run tests/unit/web-api.test.ts`；构建后运行 `tests/integration/draft-creations.test.ts`、`http.test.ts`、`database.test.ts`。不在 `/state` 广播全部永久创建事实，核实按尝试 ID 按需读取。

## 任务三：可靠观察、完整分类与确认资格

**预计文件：** 新建 `src/web/draft-conflicts.ts`、纯内存夹具 `tests/helpers/draft-conflicts.ts` 和 `tests/unit/draft-conflicts.test.ts`，修改 `api.ts`，扩展 `web-api.test.ts`、`state-observations.test.ts`。本任务只计算分类，任务四接入会话的副作用。

**消费：** `Draft`、`DraftContent`、`CapabilityState`、完整比较及同次能力派生。

**产出建议：** 定义以下类型和函数。`available` 之外的观察不能为新的采用或替换提供资格。保存结果证明仍由固定保存核实负责，普通观察分类不推断某次请求成功。

类型、分类及资格函数位于 `draft-conflicts.ts`；`observeDraft` 位于 `api.ts`，消费现有 `ClientState`。同一可靠性判断由该函数提供，其他调用者不复制另一套状态集合检查。

```ts
export type DraftObservation =
  | { kind: "available"; draft: Draft; capabilities?: CapabilityState }
  | { kind: "deleted" }
  | { kind: "unavailable"; message: string };
export type PendingDraftOperation =
  | { kind: "none" }
  | { kind: "save"; revision: number; requestEnded: boolean } // 仅尚未确认的保存。
  | { kind: "other" }; // 导出、删除、追加或冲突选择仍须履行自身责任。
export type DraftDecision = { kind:
  "ignore" | "invalid" | "unavailable" | "blocked" | "exported" | "deleted" |
  "derived" | "saved_content" | "same_baseline" | "remote" | "conflict" |
  "superseded_write" };
export interface ConflictSelectionSnapshot {
  draftId: string;
  localVersion: number;
  local: DraftContent;
  remote: Draft;
  observationEpoch: number;
}
export function observeDraft(id: string, state: ClientState): DraftObservation;
export function classifyDraftObservation(
  baseline: Draft, local: DraftContent, observation: DraftObservation,
  pending: PendingDraftOperation,
): DraftDecision;
export function selectionMatches(
  snapshot: ConflictSelectionSnapshot, draftId: string, localVersion: number,
  local: DraftContent, observation: DraftObservation, observationEpoch: number,
): boolean;
```

- [x] **步骤 1：加入分类反例。** 在 `tests/helpers/draft-conflicts.ts` 定义下面的纯内存 `record`，任务四、五、六的测试共同导入；它只用类型和精确复制，不访问文件系统或服务器。任务三测试导入分类函数，加入其后的独立分区用例。

```ts
import type { Draft, DraftContent, DraftCreation } from "../../src/server/models";
import { cloneClientJson } from "../../src/shared/json";
export function record(revision: number, name: string): Draft {
  return { id: "d1", revision,
    content: { text: JSON.stringify({ name, actions: [] }) },
    createdAt: "2026-10-11 10:00:00", updatedAt: "2026-10-11 10:00:00" };
}
export function creation(input: DraftContent): DraftCreation {
  return { attemptId: "copy-1", input: cloneClientJson(input),
    created: { ...record(1, ""), id: "copy-target", content: cloneClientJson(input) } };
}
```

```ts
it.each([
  ["base", "remote", "remote"],
  ["local", "base", "same_baseline"],
  ["local", "local", "saved_content"],
  ["local", "remote", "conflict"],
] as const)("L=%s、R=%s 的完整分类为 %s", (l, r, expected) => {
  const base = record(1, "base"), actual = record(2, r);
  expect(classifyDraftObservation(base, record(1, l).content,
    { kind: "available", draft: actual }, { kind: "none" }).kind).toBe(expected);
});
it("请求已结束且更高版本等于 L，旧写入仍不得被认领", () => {
  const base = record(1, "base"), actual = record(3, "Y");
  expect(classifyDraftObservation(base, actual.content,
    { kind: "available", draft: actual },
    { kind: "save", revision: 1, requestEnded: true }).kind).toBe("superseded_write");
});
```

- [x] **步骤 2：确认失败。** 运行 `node node_modules/vitest/vitest.mjs run tests/unit/draft-conflicts.test.ts tests/unit/web-api.test.ts`，确认清洁同步、纯版本推进及固定资格的行为缺口。实施记录：可靠读取与顺序边界取得有效行为 RED；新分类模块首次运行因模块缺失而未执行，未取得实现前分类行为 RED。独立审查已核对现有完整分类矩阵，这一过程偏差保留在验证记录中，不补造历史证据。
- [x] **步骤 3：实现可靠读取边界。** `observeDraft` 只有在 `startup.state === "ready"` 且 `drafts` 是完整有效集合时才返回可靠存在或删除。集合缺失、启动异常、重复目标或记录结构错误不能作为可靠删除；fetch／解析失败生成 `unavailable`。保留既有 `readDraft` 给尚未迁移的调用者，用新的观察函数服务冲突流程；两个入口共享可靠性检查。`StateObservations` 的 epoch、请求顺序及回执保护继续生效。
- [x] **步骤 4：按优先顺序实现分类。** 下表是实施与测试的索引，完整条件仍按权威保存专题解释。每个分区独立测试；状态不可解释时不抬高基线。

| 顺序 | 分类条件及结果 |
| --- | --- |
| 1 | 读取不可用返回 `unavailable`；目标 ID 不一致、完整记录异常或同版本不同内容返回 `invalid`；较旧版本返回 `ignore`。 |
| 2 | 可靠导出或删除返回对应终态分类，但调用者仍保留导出／删除／追加自身的待定责任。 |
| 3 | 其他操作待定，或保存请求尚在途，返回 `blocked`；普通观察不能解除这些责任。 |
| 4 | 未确认保存的请求已结束，可靠版本高于旧提交基线，则返回 `superseded_write`，允许新的明确选择，不把旧写入判为成功。版本没有推进时继续核实固定保存。 |
| 5 | 没有待定操作，先证明只是 B 的自动预览能力派生，再返回 `derived`。使用同次能力和记录，检查既有写入依据没有出现无法归于 B 的新用户写入。 |
| 6 | 依次判断 R 的完整内容等于 L、等于 B、L 等于 B，分别返回 `saved_content`、`same_baseline`、`remote`；剩余真正不同内容返回 `conflict`。 |

- [x] **步骤 5：验证快照撤销。** `selectionMatches` 同时检查草稿 ID、本页编辑版本及完整输入、可靠远端版本及完整内容、未导出状态和观察资格代次。会话在读取失效或可靠内容／终态改变时推进 `observationEpoch`；相同完整观察重复到达不推进。失效后再读回相同 R，也不能复活失效前的确认。任一资料变化、读取失效、导出或删除都返回 false；相同正文不豁免私有资料变化。加入 `1e-999` 与 `0`、`1.0000000000000001` 与 `1` 的原文差异。取消只撤销确认，不消费内容。

```ts
it("读取恢复为相同 R 后，旧确认仍已经撤销", () => {
  const remote = record(2, "remote"), local = record(1, "local").content;
  const snapshot = { draftId: "d1", localVersion: 1, local, remote, observationEpoch: 4 };
  expect(selectionMatches(snapshot, "d1", 1, local,
    { kind: "available", draft: remote }, 4)).toBe(true);
  expect(selectionMatches(snapshot, "d1", 1, local,
    { kind: "unavailable", message: "read failed" }, 5)).toBe(false);
  expect(selectionMatches(snapshot, "d1", 1, local,
    { kind: "available", draft: remote }, 5)).toBe(false);
});
```
- [x] **步骤 6：通过针对门禁并审查。** 分类还要覆盖编辑后恢复 B、同版本不同内容恰等于 L、能力派生优先于一般差异、旧读取晚到及不可用集合。运行本任务单元文件和既有 `state-observations.test.ts`、`preview-reload.test.ts`；保证仅计算分类，没有隐式请求或界面写入。

## 任务四：接入保存状态、完整采用与明确替换

**预计文件：** 修改 `src/web/session.ts`、`action-ui-identity.ts`，扩展 `tests/unit/web-session.test.ts`、`web-review.test.ts`、`action-ui-acceptance.test.ts`、`preview-current-state.test.ts`。本任务不先增加按钮，以公开会话行为验证。

**消费：** 任务一的固定身份、任务三的 `DraftObservation`、`DraftDecision`、`ConflictSelectionSnapshot` 和 `selectionMatches`；既有 `DraftTransport.save` 条件写入及固定保存核实。

**产出建议：** 保留兼容的 `editable` 入口，但分别表达编辑和向原草稿普通保存的资格。整份接纳与追加接纳使用不同的显式转换；两者都先准备再提交。

```ts
// DraftTransport 保留 save、read，并增加可靠目标观察适配。
// App 提供此方法；旧单元替身可以由 read 包装 available 分区。
readObservation?(id: string): Promise<DraftObservation>;
export interface WholeDraftTransition {
  draftId: string;
  localVersion: number;
  origin: DraftContent;
  next: DraftContent;
  baseline: Draft;
  actual: Draft;
  reason: "save" | "derived" | "remote" | "adopt" | "replace";
}
// DraftSession 新增或明确化：
get canEdit(): boolean;
get canSave(): boolean;
get confirmedDraft(): Draft; // 当前可靠保存基线，包括 updatedAt。
observeTarget(observation: DraftObservation): void;
prepareConflictSelection(): ConflictSelectionSnapshot;
adoptSavedVersion(snapshot: ConflictSelectionSnapshot): void;
replaceSavedVersion(snapshot: ConflictSelectionSnapshot): Promise<void>;
bindWholeView(prepare: (input: WholeDraftTransition) => () => void): () => void;
retryWholeAcceptance(): void;
// ActionUiIdentity 新增：
prepareWhole(input: WholeDraftTransition): () => void;
```

- [x] **步骤 1：写会话与显示接纳反例。** 使用原 `web-session.test.ts` 的纯内存 Draft 和 `deferred<T>()`。分别加入清洁同步不 PUT、纯版本推进后保存、冲突允许修正但不自动 PUT、旧回执及计时器不能恢复弃置内容的测试。下面的 `record` 与任务三相同，`vi` 使用 Vitest。

```ts
it("采用完整已保存版本后，旧自动保存不能再写回本页输入", async () => {
  vi.useFakeTimers();
  try {
    const save = vi.fn(async (_id: string, revision: number, content: DraftContent) =>
      ({ ...record(revision + 1, ""), content }));
    const session = new DraftSession(record(1, "base"),
      { save, read: async () => record(2, "remote") }, () => {});
    session.edit(record(1, "local").content);
    session.schedule();
    session.observeTarget({ kind: "available", draft: record(2, "remote") });
    expect(session.canEdit).toBe(true);
    expect(session.canSave).toBe(false);
    session.adoptSavedVersion(session.prepareConflictSelection());
    await vi.advanceTimersByTimeAsync(1000);
    expect(session.content).toEqual(record(2, "remote").content);
    expect(session.saved).toBe(true);
    expect(save).not.toHaveBeenCalled();
  } finally { vi.useRealTimers(); }
});
```

- [x] **步骤 2：确认失败。** 运行 `node node_modules/vitest/vitest.mjs run tests/unit/web-session.test.ts tests/unit/action-ui-acceptance.test.ts`，确认清洁接纳、冲突停止写入及整份显示转换的缺口。
- [x] **步骤 3：接入观察及操作资格。** `observeTarget` 消费分类并保持同次能力资料。App 的 transport 用 `readObservation` 在同次可靠状态中取得目标及能力，固定保存核实也使用它；不能继续依赖抛出相同异常的旧 read 区分删除与读取不可用。没有该适配的既有纯内存替身仅能包装可靠存在，删除及不可用用例显式提供新方法。普通轮询同时更新当前观察的可靠性，不清除待定操作。`schedule` 在真正冲突、异常或选择阶段不安排原草稿 PUT；`flush` 优先核实已有固定保存，之后只有满足普通保存资格时才能提交。新保存明确固定请求结束与否，结果与错误文案分开。完整可信的拒绝回执记录为未写入；丢失、不完整或无法对应请求的回执才保留未知责任。已可靠确认的成功或未写入先按具体保存契约处理，不继续以 `PendingDraftOperation.kind="save"` 传给未知分类；拒绝后的读取失败分别显示拒绝事实和读取问题，不能把明确拒绝重新说成未知写入。所有编辑提交、导出、普通复制、删除、追加及能力重载都检查对应会话状态，不能只依靠按钮禁用。

| 会话阶段 | 编辑输入 | 普通原草稿写入及其他业务操作 |
| --- | --- | --- |
| 普通保存正在执行 | 允许；新输入留待后续保存。 | 合并到正在执行的 flush；不能并发发起另一个原草稿写入。 |
| 真正不同版本，尚未提交选择 | 允许修正 L，并撤销旧确认。 | 暂停普通保存；导出、普通复制、删除及追加受保存前提限制。 |
| 保存请求已结束，结果仍未知且旧写入未被可靠隔离 | 保留原输入，沿既有保存契约处理后续输入。 | 先核实；新选择不能绕过。 |
| 旧保存已不能改变当前目标，开放明确选择 | 允许尚未提交的修正；旧结果仍不可确认。 | 不普通重发；选择成功后结束旧调度。 |
| 选择正在提交或核实 | 禁止修改相关会话。 | 禁止再次选择及其他写操作；关闭面板不解除责任。 |
| 后端成功已证明，显示接纳未完成 | 保留固定结果及本页资料，等待本地接纳。 | 只重试接纳，不重复后端写入。 |
| 原目标已导出或删除 | 原目标不可编辑；保留不同的待恢复资料。 | 不写回或重建原目标，适用的独立另存由任务五处理。 |

- [x] **步骤 4：共同接纳完整内容和显示。** 更新 `confirmedDraft` 的实际 revision、内容和 `updatedAt`。每次涉及动作集合的接纳先形成 `WholeDraftTransition`，通过 `bindWholeView` 取得无拒绝步骤的提交回调，随后一次更新内容、基线、操作责任与视图，再发 changed 通知。准备失败时保持原内容及键，保存可靠成功事实并提供 `retryWholeAcceptance`；不得再次 PUT。Editor 未挂载时没有需要保留的局部状态，下一次挂载按实际版本建立。

```ts
// 所有校验、副本计算和显示准备均先完成。
const commitView = wholeView?.(transition);
// 以下步骤不再调用会拒绝的映射检查，旧操作回调受操作代次保护。
commitPreparedContentAndBaseline(transition);
commitView?.();
changed();
```

`commitPreparedContentAndBaseline(transition)` 是 session 内的同步提交步骤：接纳本次已准备的完整内容和可靠基线，按固定操作归属更新责任及确认版本。其分支在本任务实现，不复用没有资格检查的旧 `accept()` 作为按钮入口。

`prepareWhole` 对可靠、可证明存续的身份保留局部键；无身份、资料不完整或来源不能证明时重建该集合的局部键。完整远端接纳即使数组等长也不能沿用位置。身份证明结合确认基线及具体转换；两个页面从 B 分别新增的动作不能仅凭标识恰相同沿用状态。明确替换保留本次所选业务内容和关联，必要内部重建共同映射所有相关身份。追加继续使用 `prepareAppend` 的固定位置及转换链，不套用远端整份重置。

- [x] **步骤 5：实现固定确认、采用及替换。** `prepareConflictSelection` 克隆 L、R、版本及 `observationEpoch`；采用／替换入口在会话层再次检验资格，随后锁定。采用不发 PUT、不触发外部 JSON 导入，不清空可靠分支或预览意图。替换建立以快照 R 为完整基线的新保存尝试，调用现有 `transport.save(id, R.revision, L)`。通过回执或同次核实确认后共同接纳。409 或可靠新版本重新展示差异并撤销确认，不抬高 revision 后自动重发。响应未知保留这次新尝试，旧责任与它分别记录。
- [x] **步骤 6：终止旧调度并防止重复消费。** 请求代次与会话编辑版本绑定。采用、替换或另存转交成功后清理旧 timer、旧 pending 和旧重发安排；迟到回调不能再次接纳。`superseded_write` 只让用户明确选择，包含 R=L 的情况；成功后结束旧保存核实调度，保留旧结果不可确认的诊断事实，不增加成功记录。取消或新结果未知保持各自依据。终态观察不替代导出、删除或追加核实。
- [x] **步骤 7：证明全部状态并审查。** 用延迟 Promise 控制请求顺序，用虚拟计时器检查 `449` 毫秒不保存、`450` 毫秒保存及连续修改重计时。采用／替换分别覆盖取消、本页变更、R 变更、读取失效、再次写入、导出和删除。旧未知保存分别覆盖读取失败、可靠 B、已证明成功、较新不同内容及较新相同 L。显示测试覆盖可靠身份、无身份等长重排及异长、准备拒绝、重复接纳、旧分配冲突和替换回退。运行所有会话、观察、追加及显示单元测试，审查通过后接入另存。

## 任务五：固定另存责任与完整输入转交

**预计文件：** 沿已有 `src/web/draft-copy.ts`、`session.ts` 及 `draft-copy-target.ts` 的责任边界补充；扩展 `tests/unit/draft-copy.test.ts`、`draft-copy-session.test.ts`、`draft-session-conflicts.test.ts` 与 `web-session.test.ts` 的受影响用例。任务六在 App 内接入目标会话及导航。

**消费：** 任务二的 `DraftCreationResult` 及精确 API，任务四的输入版本、锁定及完整接纳。

**产出建议：** `DraftCopyOperation` 持有固定创建依据；创建事实、转交责任和待决定限制分别记录。下列已有建议签名不是业务契约；沿当前实际接口补充“重试所选另存”“放弃所选另存并保留输入”的领域入口与只读资格。任务五完成时报告实际参数、返回结果、代次检查及任务六调用方式，不由 UI 清理责任数组。已有 `pendingCopyRecoveries` 负责未结束责任，历史创建事实不能冒充未处理输入。

```ts
export interface DraftCopyTransport {
  create(attemptId: string, input: DraftContent): Promise<DraftCreationResult>;
  read(attemptId: string): Promise<DraftCreationResult>;
}
export class DraftCopyOperation {
  constructor(readonly sourceId: string, readonly sourceVersion: number,
    input: DraftContent, transport: DraftCopyTransport, attemptId?: string);
  readonly attemptId: string;
  readonly input: DraftContent; // 构造时克隆，不开放外部变更。
  phase: "new" | "submitting" | "unknown" | "not_created" |
    "created_untransferred" | "invalid" | "transferred";
  result?: DraftCreationResult;
  advance(): Promise<void>;
  verify(): Promise<void>;
  completeTransfer(): void;
}
// DraftSession 新增：copyOperation?: DraftCopyOperation。
// beginCopy(transport: DraftCopyTransport): DraftCopyOperation 在会话内检查资格并固定输入。
```

- [x] **步骤 1：加入合法可加载的补充行为反例。** 沿现有 `draft-copy.test.ts`、`web-session.test.ts` 夹具，逐行落实下方任务五反例矩阵。先证明当前可加载实现允许终态待决定期间编辑、或放弃不能正确交回完整输入等实际行为缺口，不重复制造模块缺失。已有响应未知、固定事实与目标验证测试保留；以下 `creation(input)` 使用同次身份和真实接口结构。

```ts
it("目标已删除后同次核实结束，但保留尚未转交输入", async () => {
  const input = { text: "{", pending: { "": { kind: "json" as const, text: "{" } } };
  const fact = creation(input);
  const create = vi.fn(async () => { throw new Error("response lost"); });
  const op = new DraftCopyOperation("source", 4, input,
    { create, read: async () => ({ kind: "created", creation: fact,
        target: { kind: "deleted" } }) }, "copy-1");
  await op.advance();
  await op.verify();
  await op.verify();
  expect(op.phase).toBe("created_untransferred");
  expect(op.input).toEqual(input);
  expect(op.result).toEqual({ kind: "created", creation: fact, target: { kind: "deleted" } });
  expect(create).toHaveBeenCalledTimes(1);
});
```

- [x] **步骤 2：确认行为 RED。** 运行 `node node_modules/vitest/vitest.mjs run tests/unit/draft-copy.test.ts tests/unit/draft-copy-session.test.ts tests/unit/draft-session-conflicts.test.ts tests/unit/web-session.test.ts --maxWorkers=2`，保留新增测试的具体行为失败及其原因；夹具、导入或类型错误必须先修正，不能计为行为证据。
- [x] **步骤 3：实现同次核实、重试及独立待决定限制。** 首次创建固定 `attemptId` 和克隆输入。请求／核实执行中不重复执行也不可放弃；`unknown`、`invalid` 或无可靠资格时先核实同身份同完整输入，不新建身份，放弃暂不可用。可靠 `not_created` 且原请求结束后，重试才按同身份同输入创建。领域限制覆盖 `edit`、已有 `preparedEdit` 提交、`flush`、导出、删除、追加及其他版本选择；普通观察仅更新事实，不能覆盖固定源输入。校验 attempt、完整 input、首次快照及目标实际版本，不凭内容相似认领；来源终态不妨碍独立核实。

```ts
// 核实事实后才更新阶段；此处不能清除源输入。
if (result.kind === "created") {
  requireMatchingCreationResult(result, this.attemptId, this.input);
  this.result = cloneClientJson(result);
  this.phase = "created_untransferred";
}
```

`requireMatchingCreationResult(result: Extract<DraftCreationResult, { kind: "created" }>, attemptId: string, input: DraftContent): void` 在本模块定义，消费整个创建结果并验证固定 ID、完整输入、完整创建记录及其身份。目标存在时检查 `target.draft.id === creation.created.id`、版本不低于首次创建、结构及 `exportedRequestId` 与终态分类一致；`deleted` 依据独立创建查询，不要求原草稿仍存在。错目标 ID、低版本和错误终态分别有单元反例，不能进入 `created_untransferred`。结构不可解释返回诊断并保留固定操作，不能清理为成功或未创建，也不能解除尚未核实的操作锁来开始另一尝试。

- [x] **步骤 4：保留共同转交及复核保证。** 来源在 `beginCopy` 时固定且不先 flush。目标可编辑时准备源 wholeView、目标会话／显示、Map 和导航；来源实际状态来自可靠观察，不能从创建事实推测。外部 prepare 和 validate 后再次复核源身份、operation、责任代次、版本、完整输入、完整创建结果及观察资格，最后取得 `prepareTransferCompletion` 的仅同步应用闭包，全部准备通过后共同 commit。任何拒绝均在首个 commit 前；本地准备失败只重试转交，不再 create；重复结果仅转交一次。保留已验证的责任关联和提交前复核路径，不把仅同步提交改成可拒绝的后置检查。
- [x] **步骤 5：实现终态新决定与保留输入的放弃。** 目标编辑后使用实际版本。目标已导出或删除时结束创建核实，继续保持待决定限制；重试入口按保留完整输入建立独立新身份并关联原转交责任，保留旧事实。可靠未创建、已创建可编辑但转交失败、已创建终态三个分区都可放弃：结束所选安排／转交和待决定限制，输入交回原会话且不再留下同份独立未处理条目；后续编辑属于原会话。源冲突继续暂停自动保存，源终态继续只读。不得调用“丢弃输入”入口代替保留输入放弃，也不得清空其他恢复、未知写入、导出、删除或追加责任。放弃后旧操作及回调不能恢复本次选择。
- [x] **步骤 6：完成矩阵 GREEN、针对门禁及独立复审。** 逐类及组合私有资料使用 `stringifyJson` 验证原数字词元，不使用 `structuredClone` 代替精确复制。运行下列命令，记录实际接口和全部新增反例结果；保留既有 F1／N1 的责任关联与提交前复核回归，独立复审通过后才交任务六。

| 任务五反例分区 | 必须证伪的错误行为与断言 |
| --- | --- |
| 请求执行、unknown、invalid、资格不可用 | 不能放弃、新身份创建或编辑固定输入；重试先读固定事实，创建次数不增加。 |
| 可靠 not_created | 重试身份及完整输入不变；放弃后输入和源冲突保留，对应 pending recovery 为零，其他责任不变。 |
| created editable，prepare 或 validate 失败 | 源／目标／显示提交均未发生；重试不 create，放弃不删除新草稿且输入交回源会话。 |
| created deleted／exported，分别叠加源 editable 冲突／deleted／exported | 创建核实已结束但不能编辑；新决定才创建新身份，旧事实保持；放弃保持输入，源冲突或终态限制不解除。 |
| 其他独立恢复责任与相同正文 | 只完成关联责任；不能按正文清空或合并。新决定失败时输入仍有明确持有者。 |
| 外部 prepare／validate 重入和旧回调 | 分别改变身份、代次、版本、完整内容、观察资格；首个 commit 前拒绝，放弃后的旧编辑／按钮／timer 不复活安排。 |

```powershell
node node_modules/vitest/vitest.mjs run tests/unit/draft-copy.test.ts tests/unit/draft-copy-session.test.ts tests/unit/draft-session-conflicts.test.ts tests/unit/web-session.test.ts --maxWorkers=2
node node_modules/typescript/bin/tsc --noEmit
node node_modules/vite/bin/vite.js build
```

## 任务六：完整差异面板、共用异常弹窗与操作入口

**预计文件：** 新建 `src/web/draft-conflict-presentation.ts`、`DraftConflictDialog.tsx`、`tests/unit/draft-conflict-presentation.test.ts`；共用异常弹窗按现有组件边界组织。修改 `App.tsx`、`Editor.tsx`、`style.css`，在 `session.ts` 补充本任务负责的只核实保存和采用／替换本地处理入口，并扩展 `web-session.test.ts`。新建 `tests/integration/browser-draft-conflict-ui.test.ts`；需要控制未重绘状态时，沿既有 `preview-lock-fixture.tsx` 模式建立专用 `.tsx` fixture，测试入口仍为 `.test.ts`。

**消费：** 任务三的观察及选择快照，任务四的会话操作与显示准备，任务五的固定另存阶段。

**产出建议：** 比较函数不按名称或顺序猜测动作对应；两侧各自解析并按自己的顺序呈现。面板仅调用有资格检查的会话方法。

**领域接缝与实施顺序：** 先消费任务五实际重试／放弃及待决定资格，再为 `session.ts` 补充只核实保存结果的公开入口和采用／替换保留输入的放弃入口，随后接 UI。当前 `flush` 在证明未写入后可继续提交，不能作为核实按钮的实现；当前 `retryWholeAcceptance` 只有重试能力，需要新增停止本地接纳且保留输入的领域操作。入口名和返回类型由实际状态结构决定，以下责任是硬性要求：只核实不 PUT；采用资格变化必须重新确认；替换可靠写入后只处理本地接纳；放弃保留完整输入、可靠写入及其他独立责任，撤销本次代次并恢复原冲突／终态限制。不能修改任务五已经闭合的另存责任规则。

`wholeAcceptance` 也服务普通保存、远端同步、能力派生和另存，不能按 `accepting` 一律开放采用／替换放弃。新增异常事实必须识别固定操作来源及接纳代次。采用重试在任何共同提交前重新检查完整确认资格，资格失效时返回新差异与确认，不能先提交旧采用再消费新观察；替换已可靠写入后则保留该写入事实并仅处理本地接纳。`finishSelection` 是成功收尾，不能直接充当放弃而清除不相关责任。对应反例优先补在实际 `draft-session-conflicts.test.ts`，必要时扩展 `web-session.test.ts`。

```ts
export interface DraftSideSummary {
  title?: string;
  actions?: Array<{ index: number; name?: string; type?: string }>;
  raw: string;
  parseError?: string;
}
export interface DraftConflictPresentation {
  local: DraftSideSummary;
  remote: DraftSideSummary;
  savedAt: string;
  differences: Array<{ section:
    "text" | "pending" | "actionVariants" | "parameterVariants" |
    "objectVariants" | "automaticPreviews"; different: boolean }>;
  localDetails: string;
  remoteDetails: string;
}
export function presentDraftConflict(snapshot: ConflictSelectionSnapshot): DraftConflictPresentation;
// DraftConflictDialog 的输入：当前快照／可靠性、操作阶段、呈现模型；
// 回调：采用、替换、另存、重新核实、关闭；业务方法仍在 session 与 copy operation。
```

- [x] **步骤 1：先测试完整摘要与领域接缝。** 用相同正文分别改变六个资料部分及其组合；断言差异类别、顺序、原事实，不逐字锁定中文文案。两侧同名、改名、重排、动作非对象、不可解析 JSON 和未完成原文各自显示。数字词元、特殊字符及私有原文在技术详情完整保留；解析失败不生成空动作列表。会话测试分别覆盖核实读到未写入仍零 PUT、采用接纳失败后重试／放弃、替换已写入但接纳失败后重试／放弃；每例断言完整输入、后端事实、本地接纳状态与独立责任，不能只断言错误字符串。

```ts
it("正文相同而未完成输入不同，完整差异仍可见", () => {
  const remote = record(2, "计划"), local = cloneClientJson(remote.content);
  local.pending = { "/actions": { kind: "json", text: "[" } };
  const shown = presentDraftConflict({ draftId: remote.id, localVersion: 3,
    local, remote, observationEpoch: 1 });
  expect(shown.differences.find(item => item.section === "pending")?.different).toBe(true);
  expect(shown.differences.find(item => item.section === "text")?.different).toBe(false);
  expect(shown.localDetails).toContain('"["');
  expect(shown.savedAt).toBe(remote.updatedAt);
});
```

- [x] **步骤 2：取得可加载的行为 RED。** 运行 `node node_modules/vitest/vitest.mjs run tests/unit/draft-conflict-presentation.test.ts tests/unit/draft-session-conflicts.test.ts tests/unit/web-session.test.ts --maxWorkers=2`。组件集成反例分别覆盖异常自动打开、两个按钮与禁用原因、源导出后 Editor 卸载、隐藏再打开和旧回调；使用构建后的页面及受真实接口约束的故障注入，不能以模块缺失或损坏夹具当作行为失败。
- [x] **步骤 3：完成领域入口、纯呈现及共用异常弹窗。** 先让步骤 1 的会话接缝测试通过。复用 `@base-ui/react/dialog`，参考实际位于 `App.tsx` 的 `FollowupDialog` 焦点实现。差异面板保持完整双方摘要、详情及三个选择。共用异常弹窗按权威状态表自动打开，只提供重试和放弃；请求／核实执行中不可重复操作且不能关闭，创建 unknown／invalid／无资格时放弃暂不可用。另存已经可靠创建且新草稿终态时，正文在重试前说明独立新另存；采用／替换因原草稿终态失去确认资格时，按相应领域表更新可靠事实并提供适用选择，不能由共用组件自动发起创建。正常成功直接导航。初始焦点不触发操作，背景不关闭，其余非执行状态 Escape 仅隐藏并保留待决定入口，不能解除领域限制。
- [x] **步骤 4：接入观察、导航限制与完整显示。** App 在 UI 过滤 deleted 之前将原始 `/state` 草稿集合交给完整 sessions Map 的 `observeDraft`，分别处理存在、可靠缺失、集合无效和 fetch 失败，并携带同次能力。保留 StateObservations 顺序保护，被忽略读回不能授权确认／核实。Editor 绑定 `bindWholeView`。审计按 `exportedRequestId` 自动导航的 effect：导出观察不能卸载或隐藏唯一的待决定提示；无论保留当前入口还是导航后呈现责任，都必须持续可访问，不能覆盖固定输入。保存时间只来自 `confirmedDraft.updatedAt`。
- [x] **步骤 5：接入所有选择及共同转交。** 冲突另存和终态额外输入另存调用任务五领域入口，不先 flush。新目标会话／显示／Map／导航必须返回带 validate 的准备结果；先准备 source wholeView、目标及导航，外部 validate 最后调用 preparedTarget.validate，随后由 session 再复核全部固定依据并取得同步完成闭包，统一 commit。App 不单独持有或执行操作完成闭包，不在 prepare 中改变 selected、tab 或 Map，不覆盖目标已有脏或待定输入。重试和放弃消费所选责任代次；普通复制、导出、删除、追加和能力重载仍经领域资格检查。核实保存按钮只调用只核实入口，不调用 flush。
- [x] **步骤 6：分开呈现保存事实和未结束责任。** 保存区持续显示未保存、保存中、失败、待核实、冲突、已保存及终态；另行显示所选异常的待决定状态、独立恢复输入和重新打开入口。不能用待处理责任数量伪造本页输入未保存或已保存，不用 `useFeedback(page, 3000)` 清除责任。业务校验独立呈现。`beforeunload` 同时消费本页未保存、未知结果、待决定、`pendingCopyRecoveries` 及其他独立未结束责任，不能只判断 `!saved`，也不能以旧 created 事实永久制造未处理提示。
- [x] **步骤 7：验证所有组件分区并独立复审。** 按 M1–M15 记录纯函数、会话和组件各自证据。UI 未重绘时调用旧编辑／按钮／导航回调，覆盖 prepare／validate 重入，证明首个 commit 前拒绝。键盘测试证明两个按钮、自动打开、Tab／Shift+Tab、Escape、背景点击及重新打开入口；超过 3 秒和导出导航后责任仍可见。放弃后源完整输入保持，冲突继续禁自动保存、终态只读；可靠替换写入不再 PUT。运行呈现和会话单元、类型检查及构建，再运行 UI 集成、`browser-navigation.test.ts`、`browser-preview-locks.test.ts`。这些结果只证明组件范围，真实双浏览器与全套门禁由任务七完成。

## 任务七：真实双浏览器组合、完整门禁与交付

**预计文件：** 新建 `tests/integration/browser-draft-conflicts.test.ts`；按需要扩展 `draft-creations.test.ts`、`parameter-variants.test.ts`、`browser-editor-identity.test.ts`；更新 `docs/client/verification.md` 与本计划进度。

**消费：** 前六项的真实实现。双浏览器测试使用同一个隔离 Application、真实 Files、RequestLifecycle、HTTP 服务、当前网页构建，以及两个独立 `browser.newContext()`。

**产出：** 权威验收矩阵的覆盖映射、可重放测试及独立最终审查记录。

- [x] **步骤 1：建立双浏览器夹具。** 在新测试文件沿 `browser-navigation.test.ts` 的真实 setup 模式初始化临时目录与 Application，监听 `127.0.0.1` 随机端口；A、B 分别有独立上下文，共用该后端。关闭上下文、服务和 Store 后移除已验证的测试目录。复制能力文件或构造目录资料均在集成层完成；不要把文件访问放进单元帮助函数。

```ts
import { beforeAll, afterAll, afterEach, expect, it } from "vitest";
import { chromium, expect as check, type Browser } from "@playwright/test";
import { copyFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, resolve, sep } from "node:path";
import type { Draft } from "../../src/server/models";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";
let browser: Browser;
const clean: Array<() => Promise<void>> = [];
beforeAll(async () => { browser = await chromium.launch({ headless: true }); });
afterEach(async () => { for (const close of clean.splice(0)) await close(); });
afterAll(async () => { await browser.close(); });
async function setupPair() {
  const directory = mkdtempSync(join(tmpdir(), "camctl-draft-conflicts-"));
  copyFileSync("../../protocol/examples/capabilities/demo-device.json",
    join(directory, "device-capabilities.json"));
  const app = new Application(directory);
  app.store.initialize();
  const draft = app.createDraft({ text: '{"name":"起点","actions":[]}' });
  const files = new Files(app), requests = new RequestLifecycle();
  const server = createHttpApp(app, files, requests).listen(0, "127.0.0.1");
  await new Promise<void>(done => server.once("listening", done));
  const contexts = await Promise.all([browser.newContext(), browser.newContext()]);
  const [a, b] = await Promise.all(contexts.map(context => context.newPage()));
  const releases: Array<() => void> = [];
  const stop = createStop(() => new Promise<void>((done, reject) =>
    server.close(error => error ? reject(error) : done())), requests,
    () => files.idle(), () => app.store.close());
  clean.push(async () => {
    releases.forEach(release => release());
    await Promise.all(contexts.map(context => context.close()));
    await stop();
    const target = resolve(directory);
    if (!target.startsWith(resolve(tmpdir()) + sep) ||
        !basename(target).startsWith("camctl-draft-conflicts-"))
      throw Error("清理目标不属于本测试临时目录");
    rmSync(target, { recursive: true, force: true });
  });
  const url = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  for (const page of [a, b]) {
    page.setDefaultTimeout(5000);
    await page.goto(url);
    await page.getByTestId("draft-open-button").click();
    await check(page.getByLabel("计划名称", { exact: true })).toHaveValue("起点");
  }
  return { app, draft, a, b, onCleanupRelease: (release: () => void) => releases.push(release) };
}
```
- [x] **步骤 2：编写真实并发验收并记录实际结果。** 下例的 `setupPair()` 在新文件定义并返回 `{ app, a, b, draft }`，A、B 已打开同一草稿；`check` 为 `@playwright/test` 的 expect。在 B 上截住本次 PUT，使 A 先实际保存，再释放 B 请求，避免依赖两次键盘输入的速度制造冲突。前六项已满足的行为可以直接通过，不回退实现制造失败；发现生产缺陷时先定位、保留合法行为 RED，再按责任边界修复。

```ts
it("冲突另存保留双方完整版本，原草稿不被本页输入覆盖", async () => {
  const { app, a, b, draft, onCleanupRelease } = await setupPair();
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  onCleanupRelease(release);
  await b.route(`**/api/drafts/${draft.id}`, async route => {
    if (route.request().method() !== "PUT") return route.continue();
    await gate; await route.continue();
  });
  const blocked = b.waitForRequest(request => request.method() === "PUT" &&
    new URL(request.url()).pathname === `/api/drafts/${draft.id}`);
  await b.getByLabel("计划名称", { exact: true }).fill("本页版本");
  await blocked;
  await a.getByLabel("计划名称", { exact: true }).fill("已保存版本");
  await check.poll(() => app.draft(draft.id).content.text).toContain("已保存版本");
  release();
  await b.getByRole("button", { name: "查看差异", exact: true }).click();
  await b.getByRole("button", { name: "将本页另存为新草稿", exact: true }).click();
  await check.poll(() => app.store.all<Draft>("drafts").length).toBe(2);
  expect(app.draft(draft.id).content.text).toContain("已保存版本");
  await check(b.getByLabel("计划名称", { exact: true })).toHaveValue("本页版本");
});
```

- [x] **步骤 3：覆盖响应丢失及交错。** 请求故障使用 `route.fetch()` 先完成真实后端写入，再中断响应；不要只用未到达后端的 abort 冒充提交结果未知。固定闸门控制第二次写入、导出或删除，按实际事实核对双方完整内容和 Store。创建核实响应同样可丢失，最终只保留一份创建事实和一份目标；目标后来编辑／终态不改变首次事实。请求等待有界，夹具清理总是释放闸门，禁止用任意睡眠决定先后。
- [x] **步骤 4：逐项闭合权威验收。** 下表的编号按 [并发验收](../../client/acceptance.md#并发编辑与冲突解决) 28 行的当前顺序索引，仅用于任务追踪。条件与预期以责任专题为准；执行者在记录中给出具体测试名称和断言，不能只打勾而没有证据。

| 验收行 | 实施与证明责任 |
| --- | --- |
| 1–3：450 ms、编辑恢复 B、纯版本推进 | 任务三、四单元分类和计时；双浏览器验证完整同步且接纳不多一次 PUT。 |
| 4–6：不同修改、未完成输入、完整内容一致 | 任务三、四、六；双浏览器检查完整数据及保存／业务反馈。 |
| 7–10：在途、核实读取故障、旧证据取代、较新 R=L | 任务三、四；真实 HTTP 交错至少覆盖不同当前版本和相同 L，两者都保留旧结果未知事实。 |
| 11–14：另存、响应丢失、未创建重试、源及新目标后续变化 | 任务二、五、六；真实创建查询、终态待决定、同次重试与独立新决定、保留输入放弃及完整转交。 |
| 15–18：采用／替换取消成功、再次并发、本页及远端变化 | 任务四、六；双浏览器分别验证三个选择、旧资格撤销与再次 CAS。 |
| 19–21：读取不可用、原目标终态、其他业务操作待定 | 任务三、四、五；保留实际责任，不把缺失集合当删除；恢复与继续选择覆盖真实界面。 |
| 22–23：各私有资料单独差异及完整原文往返 | 任务二至六；分别运行三个选择后重开，切换外层类型、参数类型及对象模式检查所属原文和关联。 |
| 24–26：无身份完整接纳、两页独立新增、替换后再新增 | 任务一、四；真实页面核对数量、顺序、折叠／局部 JSON 模式及稳定身份。 |
| 27–28：独立两侧内容、键盘、锁定及焦点 | 任务六纯呈现及组件测试；双浏览器真实冲突面板验证操作结果和焦点。 |
| M1–M7：另存正常、未知、未创建、准备失败和源／目标终态 | 任务五领域矩阵与任务六组件；任务七真实页面验证自动弹窗、同次创建不重复、另存已创建目标终态后重试使用新身份及放弃后的完整输入归属。准备故障注入证据单列。 |
| M8–M10：采用／替换确认失效与本地接纳失败 | 任务六会话／组件；双浏览器验证可靠变化后重新确认，实际替换写入后异常的重试／放弃均不再次 PUT，完整输入及写入事实保持。 |
| M11–M12：提交前复核、旧回调及独立责任隔离 | 任务五、六定向会话与组件重入证明；任务七审计实际调用路径并在真实页面验证提示和完整责任归属，不能以历史 created 条目数当作 pending 数。 |
| M13–M15：焦点关闭、只核实保存及导航／离页提醒 | 任务六组件；任务七真实页面验证关闭不能编辑、重开入口、源导出导航不丢提示、核实未写入不 PUT、普通保存继续输入。 |

另行验证刷新／关闭提醒、后端保存时间、已保存但业务非法以及持续责任反馈。M4–M10 使用完整组合输入，实际点击重试／放弃后核对页面、后端和关联责任；三种版本选择后重开并往返所属类型／模式仍须真实页面证据。验证记录对每个分区列出测试名、断言、故障注入位置和证据层次；组件夹具或本地单元不能标为真实双浏览器覆盖。测试断言机器字段、版本、完整事实和资格；中文说明按结构及事实验证，不在多个层逐字锁定。

- [x] **步骤 5：运行完整门禁。** 从 `apps/client` 顺序执行以下命令，先类型检查与当前网页构建，再单元和集成。失败时定位具体根因，不删除测试、放宽配置或跳过验证。

```powershell
node node_modules/typescript/bin/tsc --noEmit
node node_modules/vite/bin/vite.js build
node node_modules/vitest/vitest.mjs run tests/unit --maxWorkers=2
node node_modules/vitest/vitest.mjs run tests/integration --maxWorkers=2
```

从仓库根目录执行以下检查。链接检查不验证标题锚点，新增引用的标题需另外实际核对。

```powershell
node scripts/check-protocol.mjs
node scripts/check-doc-links.mjs
git diff --check
```

- [x] **步骤 6：独立最终审查并修复。** 审查实际完整改动及验证输出，沿输入、身份、观察、能力派生、事务、副作用、响应、未知、重试、转交和可观察界面逐段追踪。分别核对普通保存、替换、复制、导出恢复、删除、追加和能力重载入口。对每个发现给出自包含修复指令；修复后重跑受影响门禁及最终组合，不能以执行者的完成声明代替审查。
- [x] **步骤 7：记录与交付。** 在 `docs/client/verification.md` 写实际日期、Node／浏览器版本、隔离环境、命令和结果，链接对应测试。更新本计划复选框，只记录已经取得证据的阶段。软件组件及浏览器测试不代表相机联调、主机性能或物理断电验证。交付时链接有效规格及验证记录，说明当前运行服务是否已加载实现；未部署到用户服务时不能声称现场问题已经消失。

## 端到端契约闭合检查

| 权威输入与过程 | 副作用、失败及恢复必须共同满足的结果 |
| --- | --- |
| 完整 L、B、R 及同次能力进入观察分类 | 读取资格和单调观察共同决定结果；能力派生有证明，其他修改不伪装派生。普通接纳与保存核实分别负责当前状态和具体尝试。 |
| 450 ms 定时器、flush 及后续本页修改 | 同次固定保存、串行写入及回执归属闭合；冲突暂停写入，解决后旧 timer／回调不能恢复弃置版本。 |
| 两侧完整快照进入采用／替换确认 | 本页版本、远端版本、完整资料及可靠观察资格共同绑定；任何改变撤销旧确认；替换事务再检查版本和终态。 |
| 固定原输入及 attempt 进入另存创建 | 原输入、独立复制结果和目标关联一起提交；响应未知只核实同次事实，重启和备份仍有依据，目标后续状态不能改变创建事实。 |
| 新增分配依据进入正文、预览关联、追加与显示 | 新操作分配独立，重算固定；前后端和接纳使用同一依据，完整替换不能使将来的动作复用另一身份。 |
| 已证明结果进入内容及视图转交 | 显示准备和 Map／导航准备先完成，提交共同发生；接纳失败保留事实和输入，仅重试本地接纳；重复结果只消费一次。 |
| 固定另存输入进入异常重试／放弃 | 在途不重复，unknown／invalid／无资格先核实，可靠未创建同身份重试，可编辑目标仅转交；另存已创建目标终态后的重试构成新身份决定。放弃输入交回源会话、仅结束关联责任，其他责任和后端事实保持。 |
| 采用／替换确认进入异常处理 | 可靠变化或条件拒绝后重试须重新确认；采用本地失败检查资格，替换可靠写入后只处理本地接纳。放弃保留完整输入与写入事实，不撤销后端写入或放行未解决冲突。 |
| 外部 prepare／validate 与迟到回调 | 身份、责任代次、本页版本、完整输入及观察资格在外部返回后复核，拒绝先于首个共同 commit；旧 preparedEdit、timer、按钮和导航不能重启已放弃选择。 |
| 读取失效、目标导出／删除及页面离开 | 真实未知、终态和待决定分别展示；终态结束核实仍保持待决定限制。App 导出导航、隐藏弹窗、切换和三秒计时不丢提示；完整独立责任进入离页提醒，不承诺跨刷新恢复。 |
| 保存结果的主动核实与持续反馈 | 只核实入口不触发 PUT；本页是否已保存依据可靠完整内容，各项未结束责任另行呈现，不能互相替代或仅用 !saved 判断离页提醒。 |

## 执行前核对及交接

业务行为没有待定选择。接口名称、模块划分和 namespace 是本计划的建议，执行者在任务开始时核对当前调用者，并将必要调整共同记录。实施前检查 Node 版本、锁定依赖和工作区状态；规划已经确认本地存在 TypeScript、Vitest 入口，后续不得把运行依赖重新安装作为测试前提。

本计划使用已选定的逐任务 subagent 实施与独立审查方式。任务五补充复审通过后，任务六消费其实际接口；任务六的局部门禁通过后，任务七完成真实组合、全套门禁和独立最终审查。文档补充本身不代表新增行为已经实施或验证。
