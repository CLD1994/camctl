# 分段成员保存的标量适配回归复核

本记录只复核既有失败证据，不修改生产文件、数据库或测试目标，也不重新启动拍摄链。

## 结论

生产读取 `read_result_file_input()` 把 `ObjectId` 类型的完成页编号放入 `SegmentMemberInput.page_event_id`，而 `segment_storage.document()` 只允许普通整数。正常完成的原片和预览因此在 `ResultMembersCommand.plan()` 编码申请时被拒绝。具体变化在 `0522159e58943dba0f028d2db5c8aa6d993e0e24`：`_completion_page()` 原有的 `return int(page_id)` 改成 `return page_id`，未配套改变真实保存消费者。这证明确有生产接口组合不闭合；不能仅凭编码器当前拒绝子类就断定修复只能落在返回端。

已有 Action6 native 和 OSMO normal 原测试均在未改测试基线 `509002c25308eda98e0096068d3510485938027e` 上失败。`ResultMembersCommand` 回滚及 CLI 的 `state_db_error` 与本次只读重现一致，不属于测试客户端会话装配错误。

## 核对对象与来源

- 仓库：`/opt/devcontainer/.codex/worktrees/de38/camctl`。本次文件摘要核对时 HEAD 为 `0ff20735ec02849cf6a3dfa3436621d1f15026bd`，工作区干净；下列两个相关生产文件与失败时安装包相同。
- 失败时已安装包：`/tmp/camctl-quality-evidence/action6/baseline/data/action6-builtin-package0/venv/lib/python3.11/site-packages`。
- 已失败、已收场的 Action6 世界数据库：`/tmp/camctl-quality-evidence/action6/baseline/data/test_installed_action6_native_0/action6-native/deployment/state.db`。
- 解释器：`/workspaces/camctl/apps/camctl/.venv311/bin/python`；失败基线收据记录 Python 3.11.17。
- Action6 原日志：上述世界的 `deployment/camctl.log`、`module.log`。OSMO 原失败：`/tmp/camctl-quality-evidence/osmo/before.log`。

两个生产文件的仓库版本和上述安装版本逐文件 SHA-256 相同：

| 相对 camctl 包路径 | SHA-256 |
| --- | --- |
| `persistence/repositories/result_file_proofs.py` | `049cae6f0ebfb6bc0c46f9e1773ac41d9ad56f14b7e69183efec57b959c7daf5` |
| `capture/segment_storage.py` | `d6693d14668258ca381a0d9cede6e835e9e22f6fe5aa021e3862981e0b9443e2` |

## 具体调用与失败位置

1. `capture.segment_completion._prepare_members()` 读取原集合的有限成员输入，并调用 `SegmentRepository.append_result_members()`。
2. `segment_result_sets.read_result_member_inputs()` 经 `_retained_input()` 调用 `result_file_proofs.read_result_file_input()`。
3. `result_file_proofs._completion_page()` 第 128 行执行 `ObjectId(evidence['result_page_event_id'])`，第 130 行直接返回这个子类对象。
4. `_read_result_file_input()` 第 174 行把该值装入 `SegmentMemberInput.page_event_id`。
5. `segment_result_members.ResultMembersCommand.plan()` 第 144 行调用 `[document(item) for item in self.payload]`。
6. `segment_storage.document()` 对 dataclass 字段递归。第 171 行的普通标量分支使用 `type(value) in (str, int, bool)`；`ObjectId` 是 `int` 的子类，因此不匹配，最后抛出 `ValueError('分段保存只接受固定大小模型')`。

编码发生在读取集合行及分配新事件之前。日志中该事务 `rows_written=0`、`outcome=rolled_back`，与失败位置吻合。

父版本对照命令如下，父版本函数末尾明确为 `return int(page_id)`：

```bash
git show 0522159e^:apps/camctl/src/camctl/persistence/repositories/result_file_proofs.py
git blame -L 121,131 apps/camctl/src/camctl/persistence/repositories/result_file_proofs.py
```

## 只读数据库核实

执行时通过 `sqlite3.connect('file:'+path+'?mode=ro', uri=True, isolation_level=None)` 打开既有数据库。只调用生产读取入口；它使用的 `BEGIN` 最终 `ROLLBACK`，没有保存事务。诊断环境为：

```bash
PYTHONPATH=/tmp/camctl-quality-evidence/action6/baseline/data/action6-builtin-package0/venv/lib/python3.11/site-packages \
/workspaces/camctl/apps/camctl/.venv311/bin/python - <<'PY'
import sqlite3
from dataclasses import fields, replace
from types import SimpleNamespace
from camctl.capture.segment_storage import SegmentResultRef, document
from camctl.persistence.repositories.segment_result_sets import read_result_member_inputs
path='/tmp/camctl-quality-evidence/action6/baseline/data/test_installed_action6_native_0/action6-native/deployment/state.db'
connection=sqlite3.connect('file:'+path+'?mode=ro',uri=True,isolation_level=None)
try:
    aid,sid,version=connection.execute('SELECT action_id,id,version FROM segment_sets ORDER BY id LIMIT 1').fetchone()
    page=read_result_member_inputs(SegmentResultRef(aid,sid,version),None,128,SimpleNamespace(connection=connection))
    for item in page.items[:2]:
        print([(field.name,type(getattr(item,field.name)).__name__,repr(getattr(item,field.name))) for field in fields(item)])
        try: print(document(item))
        except Exception as error: print(type(error).__name__,str(error))
    encoded=[document(replace(item,page_event_id=int(item.page_event_id))) for item in page.items]
    print({'items':len(page.items),'normalized_encoded':len(encoded),'page_ids':sorted(set(item['page_event_id'] for item in encoded)),'in_transaction_after':connection.in_transaction})
finally:
    connection.close()
PY
```

上述两项观察实际分成两个只读命令完成：第一个打印前两项真实字段类型及原编码错误，第二个仅对全部八项的内存副本转换该标量并编码；这里合并列出等价复现命令。观察结果为：

- 数据库中原结果集合为 `(action_id=1, id=1, version=1, state=1, member_count=NULL)`。
- 第一项是 `PREVIEW`，第二项是 `ORIGINAL`；二者 `page_event_id` 都是 `ObjectId(65)`，`file_id`、`file_event_id` 等其余编号是普通 `int`。
- 对两项原输入调用 `document()` 都得到相同 `ValueError`。
- 第一页共八项，仅把内存副本的 `page_event_id` 转为 `int` 后，八项全部编码成功，页编号仍为 `65`，读取完成后 `connection.in_transaction` 为 `False`。命令退出码为 `0`。

## 设计意图、同类边界与判断

可核验的设计要求来自 `docs/superpowers/plans/2026-10-26-transaction-facts-refactor.md` 第 9—14、65—70 行：普通消费使用已保存的固定文件、成员及 assessment／conclusion，不重跑 START／WAIT、基准或原保存来源算法；schema、业务身份、事件表示及操作键语义保持。责任规格 `docs/camctl/database/segments.md` 的“固定事实与普通发布的责任”和 `docs/camctl/database/file-fields.md` 的“文件事实的形成与消费”要求直接引用、固定历史位置和关系合法，没有要求把全部编号改成 `ObjectId` 对象。

提交说明只有“固定成员与配对消费已保存文件事实”。差异显示该提交重写完成页核验路径并改用已保存事实读取，同时删除末尾整数适配；它没有修改 `SegmentMemberInput`、`document()` 或申请编码消费端。没有找到将此返回类型变化声明为有意 API 迁移的设计记录；“重写时遗漏旧适配”是根据这些差异作出的推断，不能据此声称知道作者动机。

`SegmentMemberInput.page_event_id` 仍注解为 `int`，构造器仅调用 `ObjectId(value)` 校验，不把字段替换为包装对象。这一模型本身允许合法整数子类，不能用注解证明普通 `int` 是唯一合法实现。同类 `HistoryBoundary`、`TransactionRange` 也接受非布尔的整数子类；`fact_reads.read_event()`、`read_transaction()` 只调用 `ObjectId(value)` 作输入校验，返回的数据继续来自真实数据库，没有全链包装要求。缓存的严格类型键区分实际输入，不赋予包装对象额外的来源资格。

当前单元 `test_saved_file_inputs.py::test_saved_completion_does_not_reexecute_source_algorithm` 比较 `(file_id, file_event_id, page_event_id) == (30, 883, 872)`；包装对象与整数值相等，因此不能检查这次输出与编码端是否兼容。固定成员消费单元从已保存成员行开始，也没有跨过新输入到申请编码的接缝。这说明验证缺口，不支持把真实联合链改为忽略或替换生产返回值。

## 处理选择与验证边界

当前证据支持生产适配接缝有缺陷，尚不能证明唯一修复位置。可以比较恢复 `_completion_page()` 的原整数返回适配，与让有限模型编码器明确支持模型原本允许的已验证整数值对象；前者变更范围最小，后者需要审计所有适用值对象和编码规则。两者都应保持固定引用的范围校验、原申请和历史表示；没有证据要求接收增长容器或新增持久化结构。当前用户要求继续调查，生产保持未改。

应先用最窄回归证明完成页读取返回普通整数且可被现有 `document()` 编码，覆盖原片、预览及不具备完成页的分区，再重跑真实成员保存与同键核实的相关测试，以及受阻的 Action6 native 和 OSMO normal 链。只读 8/8 编码观察只证明提案能排除此处类型不匹配，不表示守卫、完整提交或后续联合链已经通过。

本记录完成时生产文件没有修改；生产修复的授权与实际验证另行处理。
