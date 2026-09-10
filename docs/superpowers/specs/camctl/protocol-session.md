# 请求受理与会话

[返回设计总览](../2026-09-08-camctl-cli-design.md)

本专题定义调用方、主程序与 camctl 的请求和进程契约。动作特有参数与执行结果由各动作专题定义；`last_report_id` 的有效性及覆盖含义由[状态报告与累计确认](status-reports.md)定义。

## CLI 命令与进程协议

### 命令面

```text
camctl run [<plan-path>] [--config <path>]
camctl submit <plan-path> [--config <path>]
camctl --version
```

#### `run`

完整执行会话。

不带 `plan-path` 时为裸 `run`：

```text
camctl run
```

用于驱动状态库中已经受理的任务、恢复遗留状态和生成必要报告。

#### `submit`

快速受理：

1. 打开并验证状态库；
2. 静态校验计划；
3. 执行 `request_id` 幂等判断；
4. 注册新请求，或关联已有请求；
5. 吸收有效的 `last_report_id`；
6. 判断是否需要后续 `run`；
7. 秒级退出。

`submit`：

- 不执行设备操作；
- 不生成数据产物；
- 不要求系统墙钟当前可信。

### stdout

stdout 只负责：

```text
进程控制 / 会话控制
```

不承担文件交付通知。

合法消息：

```json
{ "kind": "succeeded" }
```

```json
{ "kind": "succeeded", "body": { "needs_run": true } }
```

```json
{
  "kind": "error",
  "body": {
    "code": 13,
    "reason": "clock_invalid",
    "details": {}
  }
}
```

### `needs_run`

`needs_run: true` 表示：

> 本次提交需要一个后续 `run` 驱动。

它不要求发送此消息时旧 `run` 已经退出。

主程序应维护一个“后续需要启动 run”的本地事实：

```text
收到 needs_run
    │
    ├── 当前无 run → 启动裸 run
    │
    └── 当前 run 尚未退出
            ↓
         记录 pending-start
            ↓
         当前 run 退出后启动一次裸 run
```

多个 `needs_run` 可以合并为一次后续启动。

`run` 正常退出且没有 pending-start 时，主程序不应无条件再次启动 `run`。

### run / submit 接管不变量

会话锁只保证：

> 同一时刻最多一个 `run`。

它不等价于：

> 持锁的 `run` 一定还愿意接收新任务。

因此，`submit` 入库和当前 `run` 进入收尾之间必须有明确交接点：

```text
新计划
    ├── 当前 run 明确接管
    └── 或 submit 返回 needs_run
```

不得出现计划已经受理，却没有任何后续会话负责驱动的情况。

### `run` 生命周期

`run` 是当前主机供电窗口内的持续调度执行器。

它允许两种结束方式：

```text
主机突然断电
    → 进程在任意状态下被直接终止

当前已经没有需要继续驱动的计划工作
    → run 可以主动进入收尾并正常退出
```

只要仍然存在尚未完成、未来仍可能需要当前 `run` 驱动的计划工作，`run` 可以持续存活并等待。例如，未来 `scheduled_at` 的 `pending` action 本身就是未完成计划工作，不要求为了缩短进程生命周期而提前退出并另建定时唤醒机制。

运行中的 `run` 不是启动时计划集合的静态快照。只要尚未进入最终收尾阶段，它必须持续接纳后来通过 `submit` 成功受理的新计划，并将这些计划纳入同一进程的调度集合。

`possibly_recording` 等残留 `device_fact` 是持久化设备事实，不等同于待执行工作。如果所有计划工作已经结束，且当前没有安全且必要的主动收场责任，仅存在这类未解决残留事实时，不阻止 `run` 正常退出。

当 `run` 判断已经没有需要继续驱动的计划工作并准备退出时，仍必须满足本专题的“run / submit 接管不变量”：收尾过程与并发 `submit` 之间不得出现“新计划已经受理，但既未被旧 `run` 接管、也没有触发后续 `run`”的灰区。

## 执行计划

### 基本结构

示例：

```json
{
  "request_id": "req-20260910-001",
  "created_at": "2026-09-10 15:00:00",
  "name": "夜间采集-01",
  "last_report_id": 42,
  "actions": [
    {
      "name": "主录像",
      "type": "camera_record",
      "device_id": "cam0",
      "group": "采样-1",
      "scheduled_at": "2026-09-10 16:00:00",
      "params": {
        "duration_s": 60,
        "record": {}
      },
      "policy": {
        "max_delay_ms": 5000
      }
    },
    {
      "name": "读取传感器",
      "type": "mcu_read_data",
      "device_id": "mcu0",
      "group": "采样-1",
      "scheduled_at": "2026-09-10 16:00:30",
      "params": {
        "command": {}
      }
    }
  ]
}
```

### `request_id`

`request_id` 由调用方生成。

首次成功受理：

```text
request_id
    ↓
plan_instance_id
    ↓
action_instance_ids
```

之后永久保留该关联。

重复提交：

#### 同 `request_id` + 正文相同

复用原计划。

不创建：

- 新 plan；
- 新 action；
- 新 delivery；
- 新设备操作。

#### 同 `request_id` + 正文不同

拒绝。

不得修改已经受理的执行意图。

#### 新 `request_id`

即使正文与历史计划完全相同，也视为新的执行意图。

### `last_report_id` 是幂等比较的例外

同一 `request_id` 重试时：

```text
last_report_id
```

允许变化。

比较“请求正文是否相同”时排除该字段。

有效的新 ACK 可以单调推进累计确认位置。

较旧 ACK 或字段缺省不得使确认位置回退。

### 名称和实例 ID

调用方提供：

- `request_id`
- plan `name`
- action `name`
- `group`

CLI 分配：

- `plan_instance_id`
- `action_instance_id`
- `output_id`
- `delivery_id`
- `report_id`

plan/action `name` 是人类可读标签。

CLI 分配的实例 ID 是机器身份，不要求调用方解析格式。

### group

`group` 是轻量集合标签。

同一计划允许：

```text
action A → group = 采样-1
action B → group = 采样-1
```

每个 action 第一版最多属于一个 group。

group：

- 不参与调度；
- 没有独立状态机；
- 没有成功/失败语义；
- 不创建动作依赖；
- 可以作为取回范围；
- 可以作为取消范围；
- 用于报告组织。

`action name` 在同一计划内保持唯一。

`group` 不要求唯一。

## 取消目标寻址

`cancel_task` 可以按：

- `request_id`（计划级）；
- `plan_instance_id`；
- `action_instance_id`；
- `(plan_instance_id, group)`

寻址。

### request_id 取消

允许客户端在尚未收到状态报告、还不知道 `plan_instance_id` 时：

> 使用原计划的 `request_id` 取消整个计划。

承载该 `cancel_task` 的计划自身拥有新的 `request_id`。

不能修改原计划正文来表达取消。

### 只取消已知计划

取消动作实际执行时查询目标。

目标不存在：

```text
target_not_found
```

本次取消结束。

本次结果只针对执行时已知的目标。目标以后首次提交仍可正常受理。

同一个取消请求 ID 的重试复用原取消结果。

如果目标后来出现并且客户端仍希望取消：

> 使用新的取消请求 ID 再次提交取消。

## 新计划受理序列

首次成功受理新计划时分配单调递增的 `plan_seq`。同一 `request_id` 的幂等重试不推进该序列。状态库提供轻量读取的 `latest_plan_seq`。这两个名称为候选，见 U8。序列推进须与新计划成功受理保持一致。

该序列定义受理顺序；[调度与设备执行](scheduling-execution.md)据此发现新计划。业务变化报告使用的 `change_seq` 由[状态报告与累计确认](status-reports.md)定义。

## 持久化记录

以下是支撑本专题行为的逻辑记录建议；表划分和内部接口不构成已确认的实现约束。

请求/计划记录：`request_id`、`plan_instance_id`、`plan_seq`、原始计划正文、用于幂等比较的规范化正文、`created_at`、`name`、计划状态。请求与实例的历史关联永久保留。

受理序列计数器提供 `latest_plan_seq`。会话接纳与收尾所需的持久化记录依 U9 的交接协议确定。

## 验收要求

### 单元测试

隔离状态存储和会话协作者，验证公共输入、请求幂等判断、group 约束、取消寻址与进程响应的行为分支。

### 集成测试

组合真实 SQLite、独立 `run/submit` 进程与主程序启动逻辑，验证并发受理、收尾交接、重复提交和 pending-start（待启动标记）。涉及进程生命周期的验证归集成测试。

### 行为覆盖清单

以下按行为组织，执行时按上述单元测试与集成测试边界归类。

#### 请求幂等

- 同 `request_id` 重复提交不重新执行；
- 同 ID 正文变化拒绝；
- 同 ID 允许更新 ACK；
- 新 ID + 相同动作仍执行。

#### run / submit

- submit 不要求可信墙钟；
- `needs_run` 与旧 run 收尾竞态；
- 主程序 pending-start 模拟；
- `run` 可以跨整个主机供电窗口持续运行；
- 所有需要驱动的计划工作完成后 `run` 可以主动正常退出；
- 未来 `pending` action 阻止 `run` 因“当前无立即工作”而提前退出；
- 运行中的 `run` 持续吸收后续 `submit` 成功受理的新计划；
- 只剩 `possibly_recording` 等残留 `device_fact` 时允许正常退出。

#### group

- 多动作共享 group；
- group 取回；
- group 取消；
- group 不影响调度。

## 待确认与待细化

本节不构成已确认的行为选择。涉及其结果的实现须先补齐契约。

### U8. 新计划受理序列的最终命名

当前调度设计需要一个与 `change_seq` 独立的单调序列，只在首次成功受理新 plan 时推进，用于轻量判断是否存在尚未被当前 `run` 吸收的新计划。

当前草案暂用：

```text
plan_seq
latest_plan_seq
last_seen_plan_seq
```

该命名是当前优先候选，但尚未单独完成最终命名确认。

### U9. `run` 收尾与并发 `submit` 的原子交接机制

已经确认的不变量：

```text
新计划成功受理后
    ├── 必须由当前仍在接纳工作的 run 接管
    └── 或 submit 必须返回 needs_run = true
```

并且运行中的 `run` 会持续吸收后续通过 `submit` 受理的新计划。

但 `run` 从“仍接纳新工作”进入最终收尾时，如何与并发 `submit` 做到可原子判定的具体数据库协议尚未确定。后续设计必须保证不存在：

```text
submit 认为旧 run 会接管
        +
旧 run 已经决定退出且没有观察到该计划
```

这一竞态灰区。

### 后续契约完善

补齐计划公共字段的必填性、合法范围、时间字面量、正文规范化比较规则，以及动作类型入口与各专题的引用。明确无效 ACK 与请求受理的组合结果、`run <plan-path>` 的受理/锁/时钟检查顺序、stdout 消息及退出码的完整契约。计划状态与退役条件尚需定义。
