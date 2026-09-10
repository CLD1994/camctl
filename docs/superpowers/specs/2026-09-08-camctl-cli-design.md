# camctl 设计规格

# 1. 背景与角色

`camctl` 是运行在 ARM 嵌入式 Linux 板上的 Python CLI，负责控制相机和附属 MCU：

- 接收调用方生成的执行计划；
- 按计划调度设备操作；
- 管理设备通信；
- 在主机异常断电后恢复和对账遗留设备状态；
- 管理设备产生的数据产物；
- 将状态报告与调用方明确请求的产物发布到待传目录。

系统角色：

```text
调用方客户端
    │
    │ 生成执行计划 JSON
    ▼
主程序
    │
    │ Linux 主机上随开机自动运行的 C 程序
    │ 1. 接受外界输入并持久化计划文件
    │ 2. 调用并管理 camctl run / submit 生命周期
    │ 3. 在现实条件允许的输出窗口处理 ready 目录中的文件并对外传输
    ▼
camctl CLI
    │
    │ 调度、设备控制、状态持久化、产物管理、报告生成
    ▼
受管设备
    ├── Camera
    └── MCU
```

## 1.1 现实运行模型

主机运行于特殊环境中，供电和开机时间完全由现实条件临时决定，没有固定规律，也没有 camctl 或主程序能够预先知道的稳定运行时段。

因此，每次主机上电只意味着主程序和 camctl 获得了一段**长度未知的执行窗口**：

```text
现实条件决定主机供电
        ↓
Linux 启动
        ↓
主程序自动运行
        ↓
camctl 获得当前供电窗口内的执行机会
        ↓
主机可能在任意时刻直接断电
        ↓
下次上电后依据持久化状态恢复、对账、继续
```

主机是否有电、是否存在外界输入机会、是否存在外界输出机会，是三个彼此独立的现实条件：

```text
主机运行窗口
    ≠ 外界输入窗口
    ≠ 外界输出窗口
```

即使主机正在运行，也不代表主程序当前一定能够从外界获得新计划；同样也不代表当前适合将 `ready` 中的文件传输出去。

主程序负责：

> 外界 ↔ 主机

包括在不规则的现实窗口中接受外界输入和对外传输数据。

camctl 负责：

> 已持久化计划 ↔ 受管设备

以及将状态报告和明确请求的普通产物发布到 `ready`。camctl 不判断当前是否属于合适的外界通信窗口。

关键运行约束：

1. 主机不是持续供电，可以在任意时刻直接断电，无优雅关机。
2. Camera 与 MCU 独立供电，主机断电期间设备可以继续运行。
3. `camctl` 正常运行时假定受管设备已经上电。
4. `camctl` **不具有设备电源管理权限**，不负责上电、下电或断电重启。
5. 主程序在调用 `camctl` 前尝试同步系统时间，但 `camctl` 仍需验证时间可信性。
6. 主程序的数据传输成本较高，而且对外输出机会没有固定规律，因此文件进入待传目录后**不意味着立即传输**；主程序会等待现实条件允许的传输时机。
7. 外界输入同样只会在现实条件允许的不规则时间窗口内发生；主程序和 camctl 都不能预知下一次输入机会。

---

# 2. 核心设计原则

## 2.1 先落盘，后产生设备副作用

需要恢复依据的状态必须在设备操作发出前持久化。

例如：

```text
pending
  ↓ 持久化
running
  ↓
发送设备指令
```

主机突然断电后，状态库必须能够判断哪些动作可能已经对设备产生过效果。

## 2.2 请求级幂等

调用方为每次逻辑计划提交生成稳定的 `request_id`。

```text
相同 request_id
    = 同一次逻辑请求的重试

新的 request_id
    = 新的执行意图
```

同一 `request_id` 的重复提交不得创建新的 plan/action 实例，也不得重新执行已经执行过的设备动作。

## 2.3 不做跨请求动作内容去重

两个不同 `request_id` 即使动作内容完全相同，也视为两个独立执行意图。

不再存在：

- action identity hash；
- master action；
- 跨计划 action dedup；
- `deduped` 动作终态。

单个 action 自身在通信重试、断电恢复中的幂等仍然保留。

## 2.4 动作之间没有成功依赖

一个动作失败不自动阻止其他动作执行。

计划不提供：

```text
depends_on
```

等依赖图能力。

每个动作仅依据：

- 自身 `scheduled_at`；
- 自身时效窗口；
- 目标设备当前条件；
- 设备操作兼容性；
- 其他明确的自身执行条件；

决定是否执行。

## 2.5 不静默推断

不能证明的设备事实必须明确记录为未知或未确认。

例如：

```text
停止命令失败
≠
录像已经停止
```

---

# 3. CLI 命令与进程协议

## 3.1 命令面

```text
camctl run [<plan-path>] [--config <path>]
camctl submit <plan-path> [--config <path>]
camctl --version
```

### `run`

完整执行会话。

不带 `plan-path` 时为裸 `run`：

```text
camctl run
```

用于驱动状态库中已经受理的任务、恢复遗留状态和生成必要报告。

### `submit`

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

## 3.2 stdout

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

## 3.3 `needs_run`

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

## 3.4 run / submit 接管不变量

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

## 3.5 `run` 生命周期

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

当 `run` 判断已经没有需要继续驱动的计划工作并准备退出时，仍必须满足第 3.4 节的接管不变量：收尾过程与并发 `submit` 之间不得出现“新计划已经受理，但既未被旧 `run` 接管、也没有触发后续 `run`”的灰区。

---

# 4. 执行计划

## 4.1 基本结构

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

## 4.2 `request_id`

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

### 同 `request_id` + 正文相同

复用原计划。

不创建：

- 新 plan；
- 新 action；
- 新 delivery；
- 新设备操作。

### 同 `request_id` + 正文不同

拒绝。

不得修改已经受理的执行意图。

### 新 `request_id`

即使正文与历史计划完全相同，也视为新的执行意图。

## 4.3 `last_report_id` 是幂等比较的例外

同一 `request_id` 重试时：

```text
last_report_id
```

允许变化。

比较“请求正文是否相同”时排除该字段。

有效的新 ACK 可以单调推进累计确认位置。

较旧 ACK 或字段缺省不得使确认位置回退。

## 4.4 名称和实例 ID

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

## 4.5 group

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

---

# 5. 动作状态模型

动作状态：

```text
pending
    │
    ├──────────────→ expired
    ├──────────────→ canceled
    │
    ▼
running
    │
    ├──────────────→ succeeded
    ├──────────────→ failed
    ├──────────────→ expired
    └──────────────→ canceled
```

终态：

```text
succeeded
failed
expired
canceled
```

终态本身不迁移。

注意：

> 动作终态与动作留下的设备事实、正式产物、待传 delivery 是不同生命周期。

例如：

```text
camera_record = failed

设备事实：
    possibly_recording = true
```

两者可以同时成立。

---

# 6. 效果登记与补偿

持续设备效果使用独立效果登记。

例如录像启动成功：

```text
recording effect
    registered
```

效果收场：

```text
registered
    ↓
compensating
    ├── compensated
    └── compensation_failed
```

“只补偿一次”的准确含义为：

> 一个设备效果只创建一个持久化补偿流程。

不是：

> 设备停止指令在物理上永远只能发送一次。

多个取消路径触达同一效果时，不创建多个独立补偿流程。

补偿执行被主机断电中断后，可以恢复同一补偿流程。

如果驱动明确保证某项收场操作可以安全重复，则即使无法确认上一次尝试结果，也允许在原有限重试预算内再次执行。

支持设备端命令去重时，同一逻辑操作的重试应复用同一设备命令身份。

通信重试耗尽后：

```text
compensation_failed
```

不进行电源重启。

---

# 7. 调度与时间语义

## 7.1 `scheduled_at`

`scheduled_at` 表示：

> 主机希望开始执行该业务设备操作的绝对 UTC 时间。

主机应尽可能按时发出真正设备指令。

不规定：

```text
≤100ms
```

等固定毫秒级性能指标。

也不将主机发令时间等同于设备真实采集时间。

## 7.2 单调钟

墙钟用于确定绝对目标时间。

等待与超时使用 monotonic clock。

正常情况下：

```text
mono_target =
    monotonic_now
    + (scheduled_at - wall_now)
```

## 7.3 时效动作

时效动作具有：

```text
max_delay_ms
```

允许执行区间：

```text
scheduled_at
    ↓
    ├──────── max_delay_ms ────────┤
```

定义：

```text
window_end = scheduled_at + max_delay_ms
```

窗口内可以继续尝试。

窗口耗尽后：

- 尚未真正执行成功 → `expired`
- 已执行但最终失败 → `failed`

### 7.3.1 过期原因

`expired` 是动作终态；为什么过期通过独立结果字段表达，不增加新的动作终态。

第一版至少区分：

```text
expiration_reason = window_missed
expiration_reason = window_exhausted
```

`window_missed` 表示：

> `run` 第一次能够在可信墙钟下对该 action 进行有效窗口判断时，执行窗口已经结束，并且数据库中没有证据证明 camctl 曾经在该执行窗口内观察过它。

`window_exhausted` 表示：

> camctl 曾经在该 action 的有效执行窗口内观察并持久化过这一事实，但直到窗口结束仍未真正执行成功。

`window_exhausted` 不要求已经发出过设备指令，因此下面的结果合法：

```text
status = expired
expiration_reason = window_exhausted
attempts = 0
```

例如 camctl 已经在窗口内知道 action 存在，但目标设备持续 busy、要求 idle 的条件始终不满足，或者调度竞争导致始终没有实际设备尝试，都可以最终得到 `window_exhausted`。

不得把无法证明的原因进一步推断为 `client_submitted_late`、`host_offline` 或 `scheduler_delay`。

### 7.3.2 窗口内有效观察

只有满足：

```text
scheduled_at <= trusted_wall_now <= window_end
```

才算 action 处于有效执行窗口。

`run` 提前加载了未来 action，并不等于已经观察过它的执行窗口。

第一次在有效窗口内观察到该 action 时，必须先持久化：

```text
first_window_observed_at
```

之后才进入设备条件检查、调度竞争和可能产生设备副作用的执行流程。

只有已经成功持久化的 `first_window_observed_at` 才能作为断电恢复后的事实依据。如果 camctl 恰好在首次内存观察与该字段成功落盘之间断电，恢复后不得根据已经丢失的内存事实推断曾经观察过执行窗口。

目标设备当时是否 ready、idle 或 compatible，不影响“窗口内有效观察”的成立；这些条件影响是否能够执行，不影响 `window_missed / window_exhausted` 的分类依据。

## 7.4 无时效动作

无时效动作允许延后。

有限重试耗尽后失败。

不无限等待，不进行断电重启。

## 7.5 动作独立

一个动作失败不会级联取消后续动作。

## 7.6 新计划发现与内存调度缓存

`run` 使用 SQLite 作为权威持久状态，同时在内存中维护可丢弃、可重建的调度缓存。

不引入：

- Unix Domain Socket 唤醒协议；
- 持久 stdin 提交协议；
- 依赖 IPC notification 才能保证正确性的事件总线。

新计划仍然通过独立的 `camctl submit` 受理。`run` 使用 `asyncio` 等待，并周期检查状态库是否出现新的已受理计划。

当前目标为：

> `run` 对新提交计划的目标最大感知延迟为 500ms。

这不是硬实时调度保证；系统仍受 Python event loop、SQLite 事务、Linux 调度和不可中断设备调用等实际条件约束。

为避免每次轮询扫描 plans/actions，状态库维护一个只在**首次成功受理新计划**时推进的单调序列。当前暂用名称：

```text
plan_seq
```

每个首次成功受理的 plan 获得新的 `plan_seq`；同一 `request_id` 的幂等重试不创建新 plan，也不推进该序列。

状态库能够轻量读取：

```text
latest_plan_seq
```

当前 `run` 在内存中保存：

```text
last_seen_plan_seq
```

正常检查：

```text
latest_plan_seq == last_seen_plan_seq
    → 没有新计划，不扫描 plans/actions

latest_plan_seq > last_seen_plan_seq
    → 增量加载新受理计划
    → 合并进入内存 scheduler
    → 更新 last_seen_plan_seq
```

`plan_seq` 与 `change_seq` 语义不同：

```text
plan_seq
    = 新计划的受理顺序 / 新计划发现水位

change_seq
    = 业务状态变化顺序 / 状态报告水位
```

不得使用 `change_seq` 代替新计划发现序列，否则 `run` 自己产生的 action/output/delivery 状态变化会造成无意义的调度缓存刷新。

## 7.7 唤醒后的统一重新决策

内存调度缓存可以包含当前 `run` 已知的时间驱动工作，例如：

- 通信 `prepare_at`；
- action `scheduled_at`；
- retry deadline；
- timeout deadline；
- `camera_record` 的 stop deadline；
- 其他运行时产生的有限 deadline。

调度器允许在最近业务 deadline 之前提前一小段时间唤醒，为 SQLite 检查、增量计划加载、调度重算和运行时抖动留下余量。具体提前量尚未最终确定。

无论一次唤醒来源于：

- 周期性新计划检查；
- 某个缓存中的 work deadline；
- 某个提前唤醒点；
- 其他内部 deadline；

在依据旧缓存决定下一项实际设备操作之前，都先进行一次轻量的 `latest_plan_seq` 检查。

统一流程：

```text
wake
  ↓
check latest_plan_seq
  ↓
有新计划？
  ├── 否 → 保留当前缓存
  └── 是 → 增量加载并合并新计划
  ↓
重新计算当前最早 work / deadline
  ↓
处理已经 due 的工作，或重新 await
```

因此：

> timer wake 只代表 scheduler 获得了一次重新决策机会，不代表唤醒前缓存中的某个 action 已经取得执行权。

提前唤醒后到真正业务 deadline 之间，不进入不可打断的“最终 sleep 模式”。调度器重新回到同一个中央等待循环，并继续同时考虑：

```text
真实业务 deadline
下一次新计划检查时间
其他运行时 deadline
```

如果这段短暂等待期间又有新计划入库，下一次最多 500ms 的计划检查仍然能够打断旧调度决策；醒来后再次检查、合并并重新排序，然后才决定真正执行哪个 due action。

---

# 8. 通信准备

## 8.1 不要求持续在线

CLI：

- 不做设备 heartbeat；
- 不要求控制通信持续保持连通；
- 不在没有设备操作需求时主动维持连接。

只有需要进行设备操作时，才准备或验证通信。

## 8.2 `prepare_lead_s`

时效设备动作允许提前准备通信。

```text
prepare_at =
    scheduled_at - prepare_lead_s
```

时序：

```text
prepare_at
    ↓
建立 / 验证通信
    ↓
通信准备完成
    ↓
等待 scheduled_at
    ↓
发送真正业务指令
```

若 `scheduled_at` 到达时通信尚未准备完成：

```text
继续有限尝试
    ↓
仍在 max_delay_ms 窗口内时
通信一旦可用立即执行
```

`prepare_lead_s`：

- 由 Driver 提供默认值；
- 允许设备本地配置覆盖；
- 执行计划不能覆盖。

它是设备/部署参数，不是业务参数。

## 8.3 不再存在独立 preflight 子系统

不再有：

- run 启动全设备 preflight；
- 固定提前 60 秒探测；
- 周期连接检查；
- preflight 失败后的电源重启。

通信准备属于具体设备操作的准备阶段，不作为独立业务动作。

---

# 9. 设备执行与操作兼容性

每台设备仍由 DeviceActor 协调设备操作。

但是：

> 一个长生命周期 action 处于 `running`，不代表它持续独占整个设备。

`camera_record` 在录像期间可以保持 `running`，同时允许其他与录像兼容的设备操作执行。

驱动需要能够表达某项操作在当前设备状态下：

- 可执行；
- 要求设备 idle；
- 与录像兼容；
- 与录像不兼容；
- 是否独占控制通道；
- 长操作是否会妨碍必要的停止操作。

框架不写死：

```text
取文件一定干扰录像
```

或：

```text
维护操作一定不能并行
```

由设备能力决定。

---

# 10. camera_record

## 10.1 基本语义

对调用方暴露一个复合动作：

```text
camera_record
```

参数：

```text
scheduled_at
duration_s
record
```

底层仍由：

```text
record_start
record_stop
```

组成。

## 10.2 `duration_s`

`duration_s` 表示：

> 从已经确认本次录像进入 recording 状态的时刻开始，至少继续录制这么长时间。

不是：

> 固定保留 `scheduled_at ~ scheduled_at + duration_s` 这个墙钟区间。

## 10.3 confirmed recording start

Driver 必须定义：

> 什么证据足以确认本次录像已经处于 recording 状态。

确认时记录：

```text
recording_confirmed_at_monotonic
```

正常停止目标：

```text
stop_target_monotonic =
    recording_confirmed_at_monotonic + duration_s
```

该时间点不是“第一帧真实时间”。

它只是一个保守锚点：

> 从这里开始可以确定录像已经进行。

因此正常情况下可能略微多录，但不主动少录。

如果设备协议不能提供可靠的录像启动确认，必须降低保证并如实报告，不能使用“发出 start 指令的时间”冒充实际录像起点。

## 10.4 正常轻微多录

由保守确认点造成的正常少量尾部余量，不自动视为异常录像。

不为了精确卡秒而强制对所有录像执行裁剪。

## 10.5 客户端排程责任

客户端应保证同一相机计划中的录像时长不发生预期冲突。

camctl 不负责自动重排录像计划。

如果由于实际启动/停止延迟产生运行时冲突：

> 已经开始的录像优先。

后一个录像：

- 在自己的启动窗口内等待；
- 相机释放后仍在窗口内 → 开始；
- 超窗 → `expired`。

不得为了赶后一个录像而提前截短已经开始的录像。

## 10.6 主机断电导致超时录像

允许主机断电期间相机继续录像并超过目标时长。

恢复后：

1. 对账本次录像；
2. 能证明目标时长已经满足 → 尝试停止；
3. 目标时长尚未满足 → 在能够可靠计算剩余时间时继续等待；
4. 无法正常停止 → 有限通信尝试；
5. 尝试耗尽 → 失败，不执行电源操作。

## 10.7 时钟异常下的保守收场

如果：

- 主机墙钟不可信；
- 能确认设备仍在进行本次录像；
- 能确认是同一段连续录像；
- 无法确定历史已经录了多久；

则额外等待：

```text
min(duration_s, recovery_wait_cap_s)
```

只使用当前 run 的 monotonic clock。

达到该上限后：

> 尝试停止录像。

即使无法证明已经录满全部 `duration_s`，也不允许为了完整性无界继续等待。

`recovery_wait_cap_s` 是异常恢复保护参数，不是正常录像 `duration_s` 的上限。

## 10.8 停止失败

停止通信尝试耗尽后：

```text
camera_record = failed
```

但是不能推断：

```text
camera is idle
```

应持久化类似：

```text
possibly_recording
```

的设备事实。

后续操作：

- 与录像兼容 → 可以继续；
- 必须要求 idle → 先按需对账。

如果后续必须 idle 的操作发现：

```text
确认仍是历史已知残留录像
```

则允许有限尝试停止。

如果：

- 无法通信；
- 无法确认录像归属；
- 停止仍失败；

则保留残留事实，当前动作按自身规则失败或过期。

不做后台周期恢复。

## 10.9 录像成功标准

`camera_record` 是否成功以业务目标为准：

> 能够确认本次原始录像包含完整目标内容。

正常停止方式本身不是唯一成功条件。

如果发生异常收场，但最终确认原视频包含完整目标时长：

```text
camera_record = succeeded
```

同时在结果中保留异常收场记录。

不得因为最终成功而隐藏：

- 通信失败；
- 延迟停止；
- 恢复过程；
- 修复过程。

## 10.10 内部异常多录修复

视频裁剪不是公开 action。

它是 `camera_record` 的内部修复机制，仅用于：

> 异常导致的明显多录。

不允许调用方通过 `obtain_action_outputs` 主动指定：

- 裁剪起点；
- 裁剪终点；
- 任意裁剪时长。

修复目标：

> 从录像内容起点开始保留至少完整 `duration_s` 内容。

原则：

```text
只能多，不能少
```

允许尾部多保留几秒。

v1 使用 `ffmpeg/ffprobe` 作为板端外部依赖，裁剪只采用**无重新编码的方式**；不为了精确边界重新编码视频。

无法安全完成无重编码修复时：

```text
repair_failed
```

保留原片，不进行转码兜底。

修复在客户端尚未请求取回时也主动执行。

## 10.11 修复与动作成功分离

如果：

```text
原片已经核验完整
内部修复失败
```

则：

```text
camera_record = succeeded
repair = failed
```

录像成功不被后处理失败反向修改。

## 10.12 空间不足

相机自身仍有空间、状态库仍可可靠写入时：

> 板端视频处理空间不足不能阻止新的录像采集。

录像继续进行并保留设备源内容。

需要板端空间的：

- 拉取；
- 核验；
- 修复；

遇到空间不足时快速失败。

不等待空间释放，不跨 run 自动接续。

如果核验尚未完成，因此无法证明视频完整：

```text
camera_record = failed
```

如果原片已经核验完整，仅修复缺空间：

```text
camera_record = succeeded
repair_failed(storage_insufficient)
```

## 10.13 camera_record 取消

在动作尚未进入终态时取消 `camera_record`：

> 放弃本次录像。

包括：

- 尚未开始；
- 正在录像；
- 已经停止但正在核验；
- 正在内部修复。

如果录像仍在进行：

- 尝试停止；
- 放弃内容；
- 协议支持时清理废弃产物。

如果录像已经停止：

- 不再次发送可能误停后续录像的通用 stop；
- 终止后续核验/修复；
- 放弃本动作产生的内容；
- 协议支持时清理废弃产物。

废弃录像不登记为正常可取回产物。

取消生效不等于物理清理一定成功。

---

# 11. MCU 动作

## 11.1 `mcu_send_command`

UART 使用 request-response 模式。

动作发送命令并等待 ACK。

按照时效动作处理。

## 11.2 `mcu_read_data`

`mcu_read_data` 发送查询命令并取得实际执行时的当前值。

它是无时效动作：

> 晚一些执行没有问题，返回执行时的当前值即可。

不要求获取原 `scheduled_at` 对应的历史值。

客户端不再提供：

```text
dest_path
```

结果文件路径由 CLI 管理。

一次成功读取产生一个独立正式产物并分配：

```text
output_id
```

读取成功并形成正式产物后，后续取回或重传必须使用这份已保存结果。

不得重新查询 MCU 后用新的当前值冒充历史产物。

---

# 12. 正式产物模型

正式产物具有稳定身份：

```text
output_id
```

产物记录至少能够表达：

- `output_id`
- `source_action_instance_id`
- 类型
- 原始文件名（如适用）
- 大小
- `sha256`
- 可用状态
- 视频时长（如适用）
- 派生关系（如适用）
- 清理状态

动作状态与产物可用性相互独立。

不能使用：

```text
source action = failed
```

直接推导：

```text
没有任何可取回产物
```

异常多录录像修复成功后产生两份不同正式产物：

```text
原片
修复成品
```

两者拥有不同 `output_id`。

异常录像原片：

> 保留到客户端明确要求清理。

修复成功不自动删除原片。

---

# 13. obtain_action_outputs

## 13.1 作用

`obtain_action_outputs` 只负责：

> 选择已有正式产物，并为本次取回生成独立 delivery。

它不负责：

- 触发录像修复；
- 重新录像；
- 修改视频；
- 隐式生成业务产物。

## 13.2 产物选择

客户端可以先通过状态报告取得：

```text
output_id
```

再提交新的取回请求。

动作引用可以指定：

```text
action_instance_id
```

或：

```text
(plan_instance_id, group)
```

动作级引用允许可选：

```text
output_ids
```

语义：

```text
不带 output_ids
    → 获取该动作全部正式产物

output_ids = 非空数组
    → 精确获取这些产物

output_ids = []
    → 参数错误
```

不得因为指定产物不可用而自动扩大选择范围。

例如：

```text
只请求修复视频
```

修复视频不可用时，不能擅自改传原片。

但调用方如果省略 `output_ids`，表示明确请求“全部正式产物”，其中包括原片。

## 13.3 部分成功

例如请求：

```text
A
B
C
```

最终：

```text
A success
B success
C failed
```

A、B 仍然交付。

C 单独报告失败原因。

不因为 C 失败扣留 A、B。

## 13.4 发布屏障

在任何 selected output 仍在重试时，不提前发布其他成功产物。

必须先等：

> 本次请求中的全部 selected outputs 都取得最终处理结果。

之后才进入发布阶段。

发布阶段允许逐文件发布。

因此：

```text
A → ready → 被主程序领取
B → 稍后才进入 ready
```

是合法的。

A、B 可以落入不同数据传输窗口。

不要求整组原子发布。

## 13.5 每个新请求产生独立 delivery

新的 `request_id` 发起新的取回时：

> 即使选择与历史完全相同的 `output_id`，也创建独立 delivery。

v1 不做：

- 在途 delivery 复用；
- 引用计数；
- 多取回请求共享同一待传文件。

同一 `request_id` 的重试则复用原请求，不创建新的 delivery。

## 13.6 取消取回

取回尚未完成时取消：

- 停止尚未开始的处理；
- 已经准备但尚未发布的文件不再发布；
- 不删除正式源产物。

文件已经发布到 `ready` 后：

> `obtain_action_outputs` 可以进入终态。

但之后仍允许针对这次取回撤回尚位于 `ready` 的 delivery 文件。

已经：

```text
ready → processing
```

的文件进入不可撤回阶段。

即使主程序尚未真正开始发送字节，也不再由 CLI 撤回。

如果一个已终态的 obtain 动作被后续 cancel：

- 历史动作终态不改变；
- 只处理其仍可撤回的 delivery。

---

# 14. 产物清理

提供明确的产物清理能力，暂名：

```text
delete_action_outputs
```

清理必须显式指定 `output_id`。

不允许：

```text
省略 output_ids = 删除全部
```

也不允许空列表表示全部。

## 14.1 清理含义

清理一个正式产物意味着：

- 删除相机源文件（如存在且受 camctl 管理）；
- 删除板端受管保留副本；
- 使该 `output_id` 后续不再可取回；
- 保留该产物曾经存在以及被清理的历史记录。

已经发布到 `ready/processing` 的合法 delivery 不因此被撤回。

## 14.2 不级联删除

原片与修复成品具有派生关系，但删除：

```text
原片
```

不会自动删除：

```text
修复成品
```

反之亦然。

只删除客户端明确指定的正式产物。

## 14.3 清理与取回并发

已有取回优先。

清理开始时：

1. 持久化删除意图；
2. 禁止新的取回请求获取该产物；
3. 等待已经获得取回资格的旧请求完成必要交付；
4. 再进行物理删除。

清理开始以后新来的 obtain 请求不能插队。

## 14.4 清理部分失败

如果：

```text
板端副本已删除
相机源文件删除失败
```

则：

- 不允许新的取回；
- 不重新开放剩余副本；
- 如实报告物理清理未完成；
- 以后只能通过新的清理请求继续清理。

## 14.5 清理取消

如果清理尚处于等待旧取回的阶段：

```text
尚未发出任何可能删除文件的操作
```

则允许取消。

取消后，如果没有其他有效删除限制：

> 恢复新的取回权限。

一旦已经发出删除操作，或者无法排除删除已经发生：

> 不再因为取消而恢复取回权限。

---

# 15. 文件交接状态机

取消 batch / manifest 模型。

目录本身是文件交接状态机：

```text
staging/
ready/
processing/
```

## 15.1 staging

CLI 正在：

- 生成文件；
- 拉取文件；
- 拷贝文件；
- 计算最终内容。

主程序不读取这里的文件。

文件必须完整准备后才能发布。

## 15.2 ready

CLI 已完成本地发布。

等待主程序在合适的传输窗口领取。

普通产物发布到 `ready` 后：

> 本次取回的本地交付责任已经完成。

## 15.3 processing

主程序在开始一次传输处理前，将当前需要处理的文件：

```text
ready → processing
```

从此由主程序接管。

该移动是普通产物的不可撤回边界。

主程序：

1. 处理 `processing` 中的文件；
2. 执行本地发送流程；
3. 本地发送流程结束后删除对应 processing 文件。

主程序**只能知道本地发送流程结束**。

不能因此推断远端客户端已经完整收到文件。

## 15.4 不再存在 batch

删除：

- `manifest.json`
- `manifest_id`
- batch directory
- `batch_path`
- `batches` 状态表
- orphan batch 恢复
- stdout 文件就绪通知

## 15.5 发布原子性

单个文件：

> 必须在 `staging` 完整生成后，再原子发布到 `ready`。

`staging`、`ready`、`processing` 应位于支持所需原子 rename/move 语义的同一文件系统范围内。

多个文件不要求整体原子发布。

---

# 16. Delivery 与文件名

每次独立取回产生：

```text
delivery_id
```

delivery 与 output 是不同身份：

```text
output_id
    = 正式业务产物

delivery_id
    = 一次具体交付
```

## 16.1 文件名优先人类可读

默认：

```text
<original-stem>-<plan-name>-<action-name>.<ext>
```

例如：

```text
VID_0001-夜间采集-主录像.mp4
IMG_0042-设备巡检-东侧拍照.jpg
```

文件名不是机器身份。

客户端不解析文件名推导 `output_id` 或 `delivery_id`。

## 16.2 历史全局唯一

实际交付 `file_name` 在整个 delivery 历史中保持唯一。

已经：

- 使用过；
- 被传输；
- 被撤回；

的名字都不再复用。

发生冲突时：

```text
<original-stem>-<plan-name>-<action-name>-<short-delivery-id>.<ext>
```

例如：

```text
VID_0001-夜间采集-主录像-d052ab.mp4
```

数据库应对最终实际 `file_name` 建立唯一约束。

## 16.3 文件名安全

来自设备的原始文件名只使用 basename/stem。

plan/action 名称参与文件名时进行安全化：

- 禁止路径分隔符；
- 禁止 NUL；
- 处理控制字符和非法文件系统字符；
- 尽量保留中文等人类可读字符。

## 16.4 报告负责机器映射

状态报告包含：

```text
delivery_id
output_id
file_name
source_action_instance_id
size
sha256
delivery 状态
```

客户端根据报告中的**完整 `file_name` 精确匹配**收到的文件。

---

# 17. 状态报告与 ACK

## 17.1 report_id

每份逻辑报告具有单调递增的：

```text
report_id
```

文件名：

```text
status-report-<report_id>.json
```

## 17.2 报告不可变

不变量：

```text
同一个 report_id
    → 内容永久不变

内容发生变化
    → 新 report_id
```

纯粹重新投递同一报告：

> 复用原 `report_id`。

重新投递本身不推进：

```text
change_seq
```

## 17.3 报告内容

报告是基于 `change_seq` 的状态快照。

应报告的业务变化包括至少：

- 请求/计划注册；
- action 状态变化；
- effect / residual device fact 变化；
- output 登记及可用状态变化；
- repair 结果；
- delivery 发布、撤回等变化；
- output 清理状态；
- cancel 结果；
- 计划退役。

报告采用：

> 区间内发生变化的实体的当前状态快照。

不是逐事件日志导出。

## 17.4 ACK

ACK 仍使用计划中的：

```text
last_report_id
```

不增加独立 ACK 命令或 ACK 文件。

其含义是：

> 客户端已经持久化并接受足以完整覆盖截至该报告 `to_wm` 的状态。

它不是：

> 客户端见过的最大 report_id。

因此客户端收到更大的增量报告，但此前存在缺口时，不应推进累计 ACK。

一份新的合并报告如果已经完整补齐缺口，则可以直接推进到新的累计确认位置。

ACK 只单调推进。

## 17.5 submit 可以独立吸收 ACK

由于 `submit` 不要求墙钟可信，因此时钟异常期间也允许：

- 受理请求；
- 接收相同 `request_id` 的重试；
- 推进有效 ACK。

## 17.6 报告生成点

正常 `run` 中：

1. 恢复/对账后；
2. `report_status` 执行时；
3. 会话收尾前。

`report_status` 只表示：

> 立即执行一次本地状态报告更新。

它不保证主程序会立即传输，更不保证客户端立即收到。

---

# 18. 报告文件与 ready/processing

状态报告是 `ready` 中唯一允许被“更新替换”的特殊文件类型。

## 18.1 ready 中至多一个待领取状态报告

如果：

```text
ready 中已有 report R1
```

且没有新的业务变化：

> 保留 R1，不重新生成。

如果出现新变化：

1. 根据当前累计 ACK 水位重新生成合并快照；
2. 在 `staging` 生成新报告 R2；
3. 完整生成后撤下 ready 中的 R1；
4. 发布 R2。

R1 的逻辑报告记录本身仍不可变。

## 18.2 processing 报告不可修改

如果主程序已经领取：

```text
processing/status-report-42.json
```

CLI 不修改、不撤回它。

此时产生新状态时，可以同时出现：

```text
processing/status-report-42.json
ready/status-report-47.json
```

这是合法状态。

## 18.3 防止频繁裸 run 产生大量重复报告

只要覆盖未确认状态的报告仍然：

- 位于 `ready`；
- 或位于 `processing`；

就不因为又启动了一次裸 `run` 而排入一份完全相同的报告。

## 18.4 本地发送结束后仍未 ACK

主程序删除 processing 文件只表示：

> 本地发送流程已经结束。

远端是否收到未知。

如果累计 ACK 仍未推进，且：

```text
ready
processing
```

都已经没有能够承担该未确认状态的报告，则下一次报告机会重新承担补投责任。

没有新业务变化：

> 可以重新投递原不可变 report_id。

出现新业务变化：

> 生成新的合并报告和新 report_id。

这样避免：

```text
每次裸 run
    → 新报告
    → 主程序重复排队传输
```

同时仍保留在没有远端接收确认时的自动补救能力。

---

# 19. 取消语义

`cancel_task` 可以按：

- `request_id`（计划级）；
- `plan_instance_id`；
- `action_instance_id`；
- `(plan_instance_id, group)`

寻址。

## 19.1 request_id 取消

允许客户端在尚未收到状态报告、还不知道 `plan_instance_id` 时：

> 使用原计划的 `request_id` 取消整个计划。

承载该 `cancel_task` 的计划自身拥有新的 `request_id`。

不能修改原计划正文来表达取消。

## 19.2 只取消已知计划

取消动作实际执行时查询目标。

目标不存在：

```text
target_not_found
```

本次取消结束。

不：

- 保存 future cancel intent；
- 等待目标未来出现；
- 阻止同 ID 计划以后首次提交。

同一个取消请求 ID 的重试复用原取消结果。

如果目标后来出现并且客户端仍希望取消：

> 使用新的取消请求 ID 再次提交取消。

## 19.3 普通动作

`pending`：

```text
→ canceled
```

`running`：

- 标记取消请求；
- 已经发出的不可安全中止设备调用先完成当前尝试；
- 不再继续新的普通重试；
- 进入取消收场。

已经终态：

> 一般不改变动作历史终态。

但 `obtain_action_outputs` 的可撤回 delivery 属于独立交付生命周期，仍可按第 13 节处理。

---

# 20. 产物保留与传输失败

主程序完成本地发送后，没有远端接收确认。

因此：

```text
processing 文件被删除
≠
客户端已经收到
```

对于状态报告：

> 由累计 ACK 驱动自动补投。

对于照片、视频、MCU 数据等普通产物：

> 不自动重传。

客户端如果未收到或完整性校验失败：

1. 根据已经掌握的 `output_id`；
2. 使用新的 `request_id`；
3. 再提交 `obtain_action_outputs`。

只要正式源产物仍然可用，即可重新产生新的独立 delivery。

---

# 21. 时钟防御

## 21.1 submit

`submit` 不进行启动墙钟可信性检查。

只验证时间字段：

- 格式；
- 结构；
- 静态合法性。

不依据当前本机时间判断：

- 动作是否已过期；
- 是否已经到 scheduled_at。

## 21.2 run

`run`：

1. 取得会话锁；
2. 打开并验证状态库；
3. 读取可信历史时间下界；
4. 检查当前墙钟；
5. 必要时延迟复检。

## 21.3 时钟正常

进入正常：

- 恢复；
- 调度；
- 执行；
- 报告。

## 21.4 时钟异常

复检仍失败时，不进入正常时间调度。

但也不再直接完全退出。

进入：

> 有限安全收场模式。

只处理已经存在并且能够证明安全的遗留收场责任。

不：

- 启动新的普通定时动作；
- 用错误墙钟判断 pending 动作已经过期；
- 依据错误墙钟推断录像已录多久。

受限收场结束后，本次 `run` 仍以时钟异常退出。

## 21.5 时钟异常期间的事件

保留实际墙钟读数，但明确：

```text
wall_time_trusted = false
```

这类时间不能更新未来启动校验所使用的可信墙钟下界。

业务顺序和报告覆盖使用：

- `change_seq`
- `report_id`
- 状态机身份

而不是依靠错误墙钟排序。

## 21.6 v1 已知边界

运行中的墙钟跳变：

> v1 不持续监测、不主动处理。

稳定但错误的外部时间源：

> 缺少外部可信基准时无法判定。

---

# 22. 设备电源权限

camctl 不提供：

```text
device_power_cycle
```

删除：

- Power capability；
- Power driver；
- Power backend；
- `escalate_power_cycle`；
- reboot budget；
- 通信失败后的自动断电升级；
- 补偿失败后的自动断电升级。

通信超过最大尝试次数仍不可用：

> 按对应动作失败处理。

---

# 23. 配置

建议配置范围：

```toml
[paths]
state_db = "..."
staging = "..."
ready = "..."
processing = "..."
log_file = "..."

[clock]
min_plausible_date = "2025-01-01"
lower_bound_tolerance_s = 5
recheck_delay_s = 5
recheck_count = 1
recovery_wait_cap_s = ...

[session]
plan_poll_interval_ms = 500
log_level = "WARNING"

[devices.cam0]
kind = "camera"
driver = "adb_xxx"

[devices.cam0.connection]
prepare_lead_s = ...

[devices.mcu0]
kind = "mcu"
driver = "serial_xxx"

[devices.mcu0.connection]
prepare_lead_s = ...
```

`plan_poll_interval_ms`：

- 控制 `run` 周期检查是否出现新受理计划的最大间隔；
- 第一版目标值为 `500ms`；
- scheduler 因其他 deadline 提前醒来时，也会额外执行一次轻量的新计划检查。

`prepare_lead_s`：

- Driver 有默认值；
- 本地设备配置可以覆盖；
- 计划不可覆盖。

计划 action policy 第一版主要保留：

```text
max_delay_ms
max_attempts
backoff_base_ms
backoff_max_ms
timeout_ms
```

不再包含电源升级相关策略。

---

# 24. 状态库

单个 SQLite 数据库。

建议至少包含逻辑对象：

```text
requests / plans
actions
effects
device_facts
outputs
deliveries
reports
events
counters
```

## plans / requests

记录：

- `request_id`
- `plan_instance_id`
- `plan_seq`
- 原始计划正文
- 用于幂等比较的 canonical body
- `created_at`
- `name`
- plan status

## actions

记录：

- action instance
- plan
- type
- device
- group
- scheduled_at
- params
- 固化 policy
- status
- attempts
- `first_window_observed_at`（时效动作首次持久化的窗口内观察事实，如适用）
- `expiration_reason`（`window_missed` / `window_exhausted`，如适用）
- result / error
- change_seq

## effects

记录持续设备效果及其补偿流程。

## device_facts

例如：

```text
possibly_recording
```

用于记录动作已经终态但设备事实仍不确定的情况。

## outputs

正式产物身份与生命周期。

## deliveries

记录：

- `delivery_id`
- `output_id`
- 来源 obtain action
- 唯一 `file_name`
- 发布历史
- 撤回历史
- 必要状态

## reports

记录：

- `report_id`
- `from_wm`
- `to_wm`
- 不可变内容身份
- 文件路径/重建依据
- 累计 ACK 情况

不再存在 `batches` 表。

## events

append-only 审计日志。

业务状态变化通过 `change_seq` 驱动状态报告。

## counters

至少保存用于分配或读取全局单调序列的计数器。

当前调度发现需要：

```text
latest_plan_seq
```

它只随首次成功受理的新 plan 推进，用于让运行中的 `run` 以极低成本判断是否需要增量加载新计划；它不代替 `change_seq`。

---

# 25. 依赖与部署

运行基础：

- Python 3.11
- `pyserial`
- `adb`
- `ffmpeg`
- `ffprobe`

视频修复只使用无重新编码处理。

不要求视频转码依赖。

---

# 26. 测试重点

第一版测试至少覆盖：

## 请求幂等

- 同 `request_id` 重复提交不重新执行；
- 同 ID 正文变化拒绝；
- 同 ID 允许更新 ACK；
- 新 ID + 相同动作仍执行。

## run / submit

- submit 不要求可信墙钟；
- `needs_run` 与旧 run 收尾竞态；
- 主程序 pending-start 模拟；
- `run` 可以跨整个主机供电窗口持续运行；
- 所有需要驱动的计划工作完成后 `run` 可以主动正常退出；
- 未来 `pending` action 阻止 `run` 因“当前无立即工作”而提前退出；
- 运行中的 `run` 持续吸收后续 `submit` 成功受理的新计划；
- 只剩 `possibly_recording` 等残留 `device_fact` 时允许正常退出。

## group

- 多动作共享 group；
- group 取回；
- group 取消；
- group 不影响调度。

## 调度

- `prepare_lead_s`；
- scheduled_at；
- max_delay；
- monotonic wait；
- 不存在 100ms 硬指标；
- `plan_poll_interval_ms = 500`；
- 未变化时只读取轻量 `latest_plan_seq`，不扫描 plans/actions；
- 新 plan 推进 `plan_seq`，同 request_id 重试不推进；
- `plan_seq` 变化后增量加载并合并内存 scheduler；
- scheduler deadline 提前唤醒时同样先检查新计划；
- 提前唤醒后的短暂 await 仍能被下一次计划检查打断；
- 每次 wake 后重新决策，不让旧缓存 action 自动取得执行权；
- `window_missed` 与 `window_exhausted` 明确区分；
- `window_exhausted` 允许 `attempts = 0`；
- 提前加载未来 action 不算窗口内观察；
- `first_window_observed_at` 必须先持久化，再进入后续调度/设备条件检查；
- 断电恢复只承认已经持久化的窗口内观察事实。

## camera_record

- confirmed recording start；
- duration 从确认点计时；
- 已开始录像保护；
- 后续录像冲突等待/过期；
- 主机断电超时录像；
- 时钟异常保守等待 cap；
- 停止失败 → `possibly_recording`；
- 后续 idle-required 操作触发按需残留收场；
- abnormal overrun 自动 repair；
- stream-copy repair；
- repair_failed 保留原片；
- 取消各生命周期阶段均放弃录像。

## MCU

- current-value delayed read；
- 无 `dest_path`；
- 结果产物独立登记；
- 已登记结果不会被后续查询替换。

## outputs

- output ID 稳定；
- 原片和 repair output 独立；
- 清理不级联。

## obtain

- 默认所有 output；
- 精确 output_ids；
- 部分失败仍交付成功项；
- 全部条目结束后才开始发布；
- 文件逐个发布；
- 每个新 request 独立 delivery；
- 取消撤回 ready；
- processing 不可撤回。

## delivery 文件名

- 人类可读名称；
- 中文保留；
- 安全化；
- 历史唯一；
- 冲突追加 short delivery ID；
- 撤回后名称也不复用。

## delete outputs

- 旧 obtain 优先；
- 新 obtain 被阻止；
- 删除部分失败仍禁止新取回；
- 未开始删除前允许取消并恢复；
- 已开始删除后取消不恢复。

## 文件交接

- staging 不可见半成品；
- staging → ready 原子发布；
- ready → processing 不可撤回；
- 主机断电恢复。

## reports

- report immutable；
- 纯重投复用 ID；
- 新状态创建新 ID；
- ready 至多一个待领取报告；
- ready 合并替换；
- processing 与新 ready 报告并存；
- 无 ACK 时本地发送结束后的再次补投；
- 累计 ACK；
- 重叠快照幂等消费。

---

# 27. 当前明确删除或撤回的旧设计

下面这些不再属于目标设计：

1. `≤100ms` 触发精度指标；
2. 跨计划 action 内容去重；
3. `deduped` 动作终态；
4. 每次提交都注册新 plan 的语义；
5. 客户端指定 MCU `dest_path`；
6. batch / manifest / `batch_path`；
7. stdout 文件就绪通知；
8. batches 状态表与 orphan batch；
9. camctl 电源管理；
10. `device_power_cycle`；
11. `escalate_power_cycle`；
12. 通信失败后的断电重启；
13. 独立全设备 preflight；
14. 固定 `preflight.lead_s = 60`；
15. 持续设备 heartbeat；
16. `scheduled_at + duration_s` 固定录像停止点；
17. 异常多录时默认只暴露裁剪成品的特殊取回逻辑；
18. 板端空间不足时跨 run 暂缓并自动恢复；
19. 动作之间的成功依赖；
20. 未知 request_id 的“预先取消”；
21. 独立 ACK 上报通道。

---

# 28. 尚未完全确认的项目

以下内容目前不应在最终规格中伪装成已经定案：

### U1. `recovery_wait_cap_s` 默认值

已经确认：

```text
等待 min(duration_s, recovery_wait_cap_s)
```

但具体默认秒数尚未确定。

### U2. `obtain_action_outputs` 部分成功时的动作整体终态

已经确认：

- 成功项必须交付；
- 失败项逐项报告；
- 不扣留成功项。

但动作整体最终采用：

```text
failed
```

还是引入其他整体表达，目前尚未由需求方最终确认。

逐产物结果应当始终是权威信息。

### U3. manifest 删除后的状态报告自身完整性验证

普通产物可以通过状态报告中的：

```text
size
sha256
```

让客户端核验。

但是状态报告文件自身已经没有 manifest 提供外部 sha256。

需要最终确认底层传输是否已经提供足够的完整性保证，或者是否需要为报告增加其他校验机制。

### U4. 时钟异常期间新提交 cancel_task 的执行资格

已经确认：

- submit 可以在时钟异常时正常受理；
- 时钟异常 run 只做有限安全收场。

但“刚刚提交、尚未执行的立即 cancel_task”是否属于有限安全收场允许执行的范围，尚未明确。

### U5. 普通正式产物的长期保留策略

已经明确：

> 异常录像原片一直保留到客户端显式清理。

其他类型正式产物是否全部采用同样的无限期显式清理策略，尚未单独确认。

### U6. 相机真实协议能力

仍需实际协议确认：

- record start ACK 的语义；
- recording status；
- 当前录像身份；
- stop 是否可安全重复；
- artifact list/fetch/delete；
- 视频文件何时稳定可读；
- 录像中允许哪些其他操作；
- 控制通道与文件通道是否互相阻塞。

这些能力决定框架实际能够提供的保证等级。

### U7. 调度提前唤醒余量

已经确认：

- scheduler 可以在最近业务 deadline 之前提前唤醒；
- 提前唤醒用于为新计划检查、增量合并、调度重算和运行时抖动留出余量；
- 提前唤醒后不会进入不可打断的最终 sleep，而是回到统一等待循环。

但具体提前量、是否暴露为配置项以及最终命名尚未确定。

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
