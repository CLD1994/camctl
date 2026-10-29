# 异步接口与正式运行装配的三个接缝

本记录针对合并 `61c67571` 后的 OSMO normal 失败，只读取源码、提交差异、既有日志和已收场数据库；没有修改生产文件，没有运行新世界。范围限于本轮已改变的三个直接相邻端口，不扩展为全库迁移审计。

## 现场与版本核对

- 仓库：`/opt/devcontainer/.codex/worktrees/de38/camctl`，核对时 HEAD 为 `61c67571`。工作区文档改动由主代理维护，本复核没有修改仓库文件。
- 现场：`/tmp/camctl-quality-osmo-runtime/fixed-after-normal-pytest/test_installed_segment_source_0/world`。
- 主日志：上述世界的 `module.log`、`deployment/camctl.log`，以及 `deployment/processing/camctl-host-diagnostic-1553761-1-miqRia.log`。
- 安装包：`/tmp/camctl-quality-osmo-runtime/fixed-after-normal-pytest/osmo-builtin-package0/venv/lib/python3.11/site-packages/camctl`。
- 数据库：上述世界的 `deployment/state.db`；诊断通过 `sqlite3.connect('file:'+path+'?mode=ro', uri=True)` 查询，没有提交写入。

OSMO 安装夹具从当前项目构建并安装 wheel；私有时钟入口只替换时钟并调用正式 CLI，观察器只包装仓储提交。根集成测试没有构造 `SessionContext`。现场导入来源均为该 venv，复制的入口与测试入口相同。

以下九个文件逐字节比较均相同。摘要为仓库源码与现场安装文件共同的 SHA-256：

| camctl 包内相对路径 | SHA-256 |
| --- | --- |
| `session/service.py` | `a504eff3bb09c33911b35cde6b0f2fce4dddce387f06e1572e0c284312afb326` |
| `bootstrap/lifecycle.py` | `61d9fe7c56629299288027ab190ab62c9c4f5755a40fb2d99561698749a6ba18` |
| `bootstrap/cleanup_assembly.py` | `c4a357f56e40602ff86f5d3e21ff019b4040a8fb13442d0998abb92a97dbf986` |
| `capture/preparation.py` | `1c85ee22db3d23607618f1849dc11d09bcf2bc203a4068eef9a42a35202f5855` |
| `capture/handlers.py` | `85dc2681641be442c0b9a4a67d2e8a19ec0e02d9c1e15ff793cadf5e35b0b6bd` |
| `devices/drivers/adb_cameras/driver.py` | `6dd8a4fef80f2664d4c055473ceee03e9001593ec348d0fa25b2078543690885` |
| `bootstrap/capture_assembly.py` | `b4fa9df964b96237953ade1d1a79f645ba2a4cff76a3ce6d9467b69451993640` |
| `devices/ports.py` | `a536e9bf688dafcc032b64cf45afd824c575b32a55cdcca8c2d693ba6ed2f27e` |
| `devices/drivers/adb_cameras/bounded_result_reading.py` | `07eb55371cfd1270737b49a884d377210a6521bdd9edd96033f6e93c7a640599` |

## 已确认的异步契约

`docs/superpowers/plans/2026-10-26-read-sharing-snapshot-runtime.md` 的步骤 3 要求正常 run 的业务与快照共用数据库派发；协程继续拥有设备、文件和运行时状态，数据库线程执行有限查询和完整仓储操作。该步骤目前尚未勾选完成。

`BusinessDatabase.read()` 接受同步的有限数据库回调，由数据库线程执行，再异步返回有限值；回调不得交付连接、游标或延迟执行对象。关闭也必须等待真实数据库责任交付。`devices/ports.py` 已将 `dispatch_check` 改为 `Callable[[], Awaitable[str | None]]`，将 `prior_result_entries` 改为 `Callable[[], AsyncIterator[Mapping[str, Any]]]`。`docs/architecture/camera-capabilities.md` 第 179 行明确要求在每次新派发前等待真实资格查询，查询期间发生的取消仍阻止下一次派发。

## 一、现场主错误：清理流程读取不存在的会话端口

`dc1c8d9b` 将 `bootstrap/cleanup_assembly.py::_CleanupRound.__call__()` 第 633 行改为 `runtime = await self._factory(context.database)`；`session_cleanup_assembly()` 返回的工厂也改为异步并要求 `BusinessDatabase`。

正式生产者 `session/service.py::SessionContext` 第 227—285 行没有 `database` 字段；唯一正式构造位于 `bootstrap/lifecycle.py` 第 1118 行，也没有提供该端口。正常流程注册包含这个新清理流程，因此真实 `SessionContext` 一进入该轮就会触发现场原文：`流程 cleanup 异常: 'SessionContext' object has no attribute 'database'`。

这是生产者与消费者组合不闭合，现场安装内容与源码相同，不能归因于 OSMO 测试保留旧上下文。新增清理单元使用 `SimpleNamespace(database=autospec)`，证明清理消费者自身的异步协作，但没有经过真实会话装配。

最小责任边界在正式会话数据库拥有者、`SessionContext` 和清理工厂之间。选定的修复应让一次 run 实际拥有并交付 `BusinessDatabase`，使用真实数据库配置并在原本地责任结束后等待关闭；仅给测试上下文补字段不能修复生产。是否同时完成其他流程的迁移，需由实施方按当前步骤的有效范围确定，不能凭新增一个字段宣称全 run 已共用派发。

验证应覆盖正式 `SessionContext` 经真实 bootstrap 注册后驱动一次清理、空清理和有原保存责任的收场，再运行既有受影响清理及会话装配测试。当前只读证据足以定位本错误，不证明新数据库拥有者的生命周期已经实现。

## 二、现场次错误：拍摄资格回调仍同步，具体堆栈尚未保留

`27d8ccc3` 将 ADB 驱动第 219、282 行及 Hasselblad 驱动第 438 行改为 `await request.dispatch_check()`。生产请求构造仍有三个同步来源：

- `capture/preparation.py` 第 178 行的 lambda 调用同步 `preparation_dispatch_reason()`，资格成立时返回 `None`。
- `capture/handlers.py` 第 1672、1964 行的 lambda 调用同步 `_capture_dispatch_check()`，返回 `str | None`。

因此这些真实生产请求不满足端口当前的 Awaitable 契约。资格成立时，驱动会尝试等待 `None`；资格被拒时也会尝试等待字符串。这一接口不兼容可由源码直接确认，并非测试 stop 替身引入。

现场 `deployment/camctl.log` 在会话失败前记录了 `ReservePreparationCommand` 和 `BeginAttemptCommand`。只读数据库中两条责任键为 `prepare/1/...`，第二条尝试仍处于运行状态。`advance_preparation()` 创建 `_call_prepare()`，后者进入真实 `prepare_capture()`；`_wait_preparation_task()` 保存并交付原任务错误，后台任务的错误可在停止及等待原责任时作为 shutdown 次错误交付。这与现场 `TypeError: object NoneType can't be used in 'await' expression` 精确吻合。

现场日志没有 Python 堆栈，所以“次错误具体来自第 219 行”仍是依据生产调用链的归因，不能声称已有堆栈直接证明。可以确认的是同步生产者与异步消费者不兼容，而且现场确已进入该准备责任。OSMO 私有入口没有构造同步 None stop，不能以此解释本次错误。

最小责任边界在生产资格回调构造处：准备及实际 START 都须交付可等待的真实资格查询，保留原取消、占用和窗口判断；查询恢复后仍执行驱动已有的调用取消检查。撤销驱动的等待或只把测试替身换成 `AsyncMock`，不能兑现新契约。若以 `BusinessDatabase.read()` 承接，应把完整的有限查询放进所属数据库线程，不能把同步 SQLite 读取仅包成一个没有实际等待的协程后宣称完成线程迁移。

验证应从生产请求构造进入真实驱动，覆盖资格成立、取消、窗口失效，以及查询等待期间发生调用取消；原准备与 START 的尝试身份和收场边界保持。当前没有重跑这些测试，也没有通过注入或绕过收场取得成功结论。

## 三、静态未触发：前页历史消费者改为异步迭代，装配仍返回同步生成器

`e5aea1d5` 改变了 `prior_result_entries` 端口；`devices/drivers/adb_cameras/bounded_result_reading.py` 第 206 行现在使用 `async for entry in history()`。正式生产者 `bootstrap/capture_assembly.py::_prior_result_entries()` 第 307 行仍注解为 `Callable[[], Iterator[Mapping[str, Any]]]`：首页返回 `lambda: iter(())`，后页第 373 行定义同步 `entries()` 并以普通 `yield` 生成前页条目。

同步生成器没有异步迭代协议，不能满足新消费者。这是另一处可确认的正式装配接缝；本次世界在准备阶段已失败，尚无证据表明它触发了结果历史读取，不应把它列为当前日志的直接原因。

最小责任边界在真实前页历史生产者与端口之间：按新端口交付异步迭代，保持同一原 RESULTS 尝试、可靠末引用、连续游标和有限批次；读取实际数据库页时使用选定的异步数据库端口。不能通过一次物化全部历史或替换为测试自造条目来避开契约。

验证应覆盖真实装配的首页及后续页，尤其需要检查前页原片来解释预览缺失的分区；保留跨页连续性和范围错误反例。当前只做静态核对，未执行新结果链或容量测试。

## 本次复核边界

三处都属于本轮端口改变后正式生产者与消费者未同时接通。根集成测试运行真实安装产物，应继续暴露这些错误，当前没有证据支持为新设计放宽业务断言或在测试入口补生产字段。修复方案、授权和实施验证由主代理与用户另行确定。
