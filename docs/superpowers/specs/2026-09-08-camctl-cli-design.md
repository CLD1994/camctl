# camctl 设计规格

- 日期：2026-09-08
- 状态：已与需求方逐节确认，待最终审阅
- 读者：无前置上下文的实现者与评审者。本文只描述当前有效的目标状态。

## 1. 背景与角色

camctl 是一个 Python CLI，运行在 ARM 嵌入式 Linux 板上，充当相机与附属 MCU 的控制枢纽：接收执行计划，按计划在指定的绝对时间向设备发送指令、从设备获取数据放到指定位置、读取 MCU 数据、维护设备可用性，并把执行结果以文件形式交付给远端调用方。

系统中各方的职责边界：

```
调用方客户端（隔离主机之外）
    │  生成执行计划 JSON；经其他手段传输到隔离主机
    ▼
C 主程序（隔离主机上，纯中转，不理解业务）
    │  职责 1：把调用方的计划 JSON 持久化为文件，以文件路径为参数调用 camctl
    │  职责 2：从 camctl 的 stdout 获知待传批次目录，把其中文件传给调用方，
    │           传输成功后删除批次目录
    ▼
camctl CLI（本项目）
    │  调度与执行动作、维护状态、产出报告与产物批次
    ▼
受管设备：相机（USB + adb，自定义协议）＋ 附属 MCU（UART 串口，自定义协议）
    设备独立供电，宿主断电时设备不下电
```

宿主运行环境的关键事实与约束：

- C 主程序通过 epoll 监听 stdout 管道，通过 SIGCHLD 感知 camctl 退出；C 不会中途终止 camctl 进程。
- 宿主不是持续供电：运行周期结束时直接断电，无优雅关机。camctl 必须假设任意时刻猝死。
- 相机与 MCU 有独立供电，宿主断电期间设备保持运行，设备侧可能残留中间状态。
- C 负责在调用 camctl 前同步系统时钟，但 camctl 不得预设时钟可靠（见第 8 节）。
- C 可能在既有会话运行期间再次投递计划（并发 spawn），此时使用不同的子命令（见第 3.1 节）。
- 板载 Python 3.11.16；依赖面限制为标准库 + pyserial（纯 Python）+ adb 外部二进制。

## 2. 行为契约总览

- **输入**：argv（子命令、计划文件路径、可选配置路径）；计划 JSON 文件；配置文件（可选）；系统时钟；设备通道（adb / 串口）。
- **输出**：stdout NDJSON 消息（仅 `succeeded` / `error` 两种 kind）；outbox 批次目录（manifest.json + 载荷文件）；状态库（SQLite）；stderr 日志。
- **核心不变量**：
  1. 先落盘后动作：`running` 转换在设备操作发出**之前**落盘（断电时才有恢复对账的锚点）；终态转换在结果产生后立即落盘（fsync）。断电后已记录状态不丢。
  2. 报告至少一次投递：报告文件先产出并上 stdout 清单，后标记 reported；调用方按快照语义幂等消费。
  3. 补偿恰好一次：每个登记的效果至多触发一次补偿，由效果登记项状态机保证。
  4. 不静默解释：无法证明安全的状态（对账不能、设备不可达、时钟异常）一律如实记录并输出，不折叠为成功或跳过。
- **失败语义**：致命错误 → `error` 消息 + 专用退出码；动作级失败 → 状态机终态 + 报告如实呈现。

## 3. 对外契约

### 3.1 命令面

```
camctl run    [<plan-path>] [--config <path>]   # 完整会话；不带 plan-path 为裸 run（纯驱动既有队列）
camctl submit <plan-path>   [--config <path>]   # 快速路径：校验、入库、受理，秒级退出
camctl --version                                # 输出版本号退出
```

- C 侧选择规则：当前无存活的 camctl 子进程 → 用 `run`；已有会话在跑 → 用 `submit`。C 通过 SIGCHLD 天然掌握该信息。
- 会话锁：`flock` 排他锁（锁文件路径可配置，默认 `$HOME/.camctl/run.lock`）。`run` 撞锁 → `error` + 退出码 12，立即退出。`submit` 不受锁限制（写库走 SQLite 自身的锁与 busy_timeout）。
- `submit` 遇锁空闲：正常受理入队，`succeeded` 消息 body 携带 `needs_run: true`，告知 C 需再调用 `run`；不升级为会话。C 再次以同一计划调用 `run` 时，动作去重吸收重复提交，无副作用。
- 活跃会话通过轮询状态库中的 `queue_version` 计数（默认约 500ms）感知新入队项，不使用进程间通信。

### 3.2 stdout 协议

NDJSON：每行一个自包含 JSON 对象，UTF-8，换行结尾，写入即 flush。只有两种 kind：

| 场景 | 消息 |
|---|---|
| submit 受理成功 | `{"kind":"succeeded","body":{"needs_run":true?}}` —— `needs_run` 仅当会话锁空闲时出现；body 无内容时整体省略 |
| run 会话中批次就绪 | `{"kind":"succeeded","body":{"batch_path":"<批次目录绝对路径>"}}` |
| run 会话正常完结 | `{"kind":"succeeded"}`（裸）→ exit 0；这是明确的流结束标记 |
| 致命错误 | `{"kind":"error","body":{"code":<int>,"reason":"<机器可读原因>","details":{…}}}` → 对应退出码 |

- stdout 不承载任何业务字段（实例 id、动作状态等一律不进 stdout）；业务信息只经报告文件与 manifest 文件到达调用方。
- 各 body 的合法字段组合由单一权威 schema 注册表定义（`protocol.py`），测试精确断言。

### 3.3 退出码注册表

| 码 | 含义 | 伴随 error 消息 |
|---|---|---|
| 0 | run 正常完结 / submit 受理成功 / --version | 否 |
| 10 | argv / 用法 / 配置文件错误 | 是 |
| 11 | 计划校验失败（逐项违规详情在 details） | 是 |
| 12 | 会话锁被占（run 撞锁） | 是 |
| 13 | 时钟异常（启动校验含复检后仍失败） | 是 |
| 14 | 状态库不可用（损坏、不可写） | 尽力而为 |
| 15 | 内部错误（未预期异常） | 尽力而为 |

- 码表以枚举集中定义，唯一权威来源。10–125 为 camctl 注册区间；1–9 与 126 以上留给 shell/内核惯例（含 128+N 信号语义），不占用。
- 硬崩溃（无任何 stdout 输出、非零退出或被信号杀死）由 C 的 SIGCHLD 路径兜底感知。

### 3.4 批次目录与文件传输

所有到达调用方的文件（报告、动作产物）以**批次**为传输单元：

```
<outbox>/batches/<manifest_id>/
├── manifest.json        # 见下
└── <载荷文件…>           # 同一文件系统内用硬链接，不重复占存储
```

`manifest.json` 契约字段：`manifest_id`、`created_at`（UTC，格式见 4.1）、`files: [{name, size, sha256, origin}]`；`origin` 标明文件来源（`report` + report_id，或 `action_output` + 动作实例 id）。

- camctl 生成批次 → DB 登记（批次 id、路径、是否已上 stdout）→ 输出 `batch_path` 消息。
- C 的职责：遍历批次目录，把全部文件传给调用方，成功后删除批次目录。C 不解析 manifest.json。
- 完整性校验归调用方客户端：按 manifest.json 逐文件核 sha256。
- 重传 = 生成新批次目录（载荷硬链接或从源重拷）+ 新 `batch_path` 消息；批次自包含、幂等。
- 孤儿批次（已生成、已登记，但未及输出到 stdout 即断电）：下次会话启动时按 DB 登记清理。C 侧传输失败留下的批次由 C 负责重试或丢弃，camctl 不感知。

## 4. 执行计划

### 4.1 计划 JSON 格式

时间为 UTC，格式 `YYYY-MM-DD HH:MM:SS`（秒级，不支持小数秒）。示例：

```json
{
  "created_at": "2026-09-08 14:17:35",
  "name": "夜间采集-01",
  "last_report_id": 42,
  "actions": [
    {
      "name": "开拍",
      "type": "camera_record",
      "device_id": "cam0",
      "group": "采样-1",
      "scheduled_at": "2026-09-08 15:00:00",
      "params": { "duration_s": 60, "record": { } },
      "policy": { "max_delay_ms": 5000 }
    },
    {
      "type": "mcu_read_data",
      "device_id": "mcu0",
      "group": "采样-1",
      "scheduled_at": "2026-09-08 15:00:30",
      "params": { "command": "…", "dest_path": "/data/mcu/reading-0001.bin" }
    },
    {
      "type": "obtain_action_outputs",
      "device_id": null,
      "scheduled_at": "2026-09-08 15:05:00",
      "params": { "refs": [ { "plan_instance_id": "p-0007", "group": "采样-1" } ] }
    }
  ]
}
```

字段语义：

- `created_at`：必填。`name`：可选，仅供人类阅读。
- `last_report_id`：可选，调用方客户端实际收到的最新报告的 `report_id`（ACK 水位线，见第 12 节）；缺省表示尚未收到任何报告。
- `actions[].type`：必填，须为注册表内类型（见第 13 节）；未知类型整份拒绝。
- `actions[].device_id`：设备动作必填且须在设备注册表中存在；meta / 传输动作（`report_status`、`cancel_task`、`obtain_action_outputs`）为 null。
- `actions[].scheduled_at`：设备动作与 `obtain_action_outputs` 必填（绝对墙钟触发时刻）；`report_status`、`cancel_task` 可缺省，缺省 = 会话取得控制权后立即执行。
- `actions[].params`：按动作类型的参数 schema 校验；设备协议相关子结构（`capture` / `record` / `command` 载荷）由驱动侧 schema 校验，框架不硬编码。
- `actions[].policy`：可选，逐字段覆盖类型默认策略；可覆盖项：`max_delay_ms`、`max_attempts`、`backoff_base_ms`、`backoff_max_ms`、`escalate_power_cycle`、`timeout_ms`。时效性类别（有时效/无时效）由类型固定，不可覆盖。入队时解析固化，之后不再重算。
- `actions[].group`：可选标签（计划内唯一），见 4.3。

### 4.2 命名与实例标识

- 调用方 JSON 无状态：不含任何 CLI 分配的标识。
- DB 层 plan name 与 action name 均 NOT NULL，缺省生成：plan → `plan-{created_at}`；action → `{plan_name}-action{NO.}`（NO. 为动作在 `actions` 数组内的 1 起序号）。
- name 只是报告中的可读标签。机器寻址只认 CLI 分配的实例 id（plan instance id、action instance id）；实例 id 对调用方是**不透明字符串**，不得解析其内部结构。实例 id 仅通过报告文件到达调用方；提交与可引用之间存在一个报告往返的延迟，调用方编排自行吸收。
- 同一计划内显式 action name 不得重复（缺省生成的天然不重复）；plan name 不强制全局唯一。

### 4.3 动作组

组是轻量标签机制，不是调度实体：无独立状态机、不参与调度、不进入动作去重身份。组名在三处生效：

1. `obtain_action_outputs` 的引用单位：`refs` 可为动作实例 id，或 `(plan_instance_id, group)`；
2. `cancel_task` 的目标单位：可按组取消（组内全部非终态动作 + 组内动作登记的持续效果）；
3. 报告组织：报告 JSON 在计划分组之下按组聚合动作。

### 4.4 校验（全有或全无）

入库前整份校验，任一违规即整份拒绝（单事务，零部分入库），`error` 消息 details 列出全部违规项，退出码 11。校验项：

- schema 与字段格式（含时间格式、时间合法性）；
- 未知动作类型；
- `device_id` 不存在于设备注册表；
- 动作所需能力不在目标设备驱动声明的能力集内（快速失败在提交时刻，不等 deadline）；
- 设备动作缺 `scheduled_at`；时效动作缺必需参数（如产物命名类参数，随协议定案由驱动 schema 规定）；
- 显式 action name / group 计划内重复；
- `last_report_id` 引用了从未发出过的 report_id（调用方 bug，不猜测；状态库损毁重建导致 ACK 断链属运维级灾难，见 19 节）。

## 5. 状态库与核心状态模型

### 5.1 状态库

- 单个 SQLite 文件（默认 `$HOME/.camctl/state.db`），`journal_mode=WAL`、`synchronous=FULL`：每次提交 fsync，断电后由 WAL 重放恢复到最后一个完整事务。
- `PRAGMA user_version` 管理 schema 版本。
- 写入路径只有两条：run 会话与 submit 入库。会话内部同一事件循环天然串行；run 与 submit 跨进程并发时由 SQLite 锁 + busy_timeout 互斥。
- 主要存储对象（列为契约级要求，DDL 细节留给实现计划）：
  - `plans`：plan instance id、seq（提交顺序，单调递增）、name、created_at、content_hash、plan_json 全文、last_report_id（ACK 记录）、status、时间戳；
  - `actions`：action instance id、所属 plan、name、group、type、device_id、scheduled_at、params、固化 policy、status、attempts、result/error 详情、change_seq、reported 标志；
  - `effects`：效果登记项（见 5.4）；
  - `events`：append-only 事件日志（墙钟 + monotonic 双读数、run_id、对象引用、事件类型、详情 JSON）——业务审计的权威来源，也是时钟下界证据；
  - `reports`：report_id（单调递增）、from_wm、to_wm、文件路径、批次归属、emitted/reported/acked 状态；
  - `batches`：manifest_id、路径、已上 stdout 标志（孤儿清理依据）；
  - 计数表：`queue_version`（会话轮询感知入队）、`change_seq`（报告水位线，每个应报告的状态变化递增）。

### 5.2 动作去重

动作身份 = 规范化 `(type, device_id, scheduled_at, params)` 的哈希；policy、name、group 不参与身份。插入决策表：

| 库内同身份动作 | 处理 |
|---|---|
| 不存在 | 正常插入 |
| 存在，非终态（pending / running） | 不重复插入；记为 `deduped` 并指向主实例（报告有据可查，不出现空洞），沿用主实例已固化策略 |
| 存在，已终态（succeeded / failed / expired / canceled） | 正常插入新实例（已完结的相同动作再来一次就再执行一次，保留调用方重复交互语义） |

计划注册不做计划级去重：每次提交都注册新计划实例（分配 seq 与 instance id），执行、报告、退役以动作实例为粒度。

### 5.3 动作状态机

```
pending ──派发──▶ running ──┬─▶ succeeded
   │                       ├─▶ failed      （尝试后失败，含 reason）
   │                       ├─▶ expired     （时效动作超窗且未曾执行成功）
   │                       └─▶ canceled    （取消，见第 11 节）
   ├─▶ expired   （deadline+窗口在宕机/排队中耗尽且未执行）
   ├─▶ canceled
   └─（插入时判定 deduped，终态标记，指向主实例）
```

- 终态集合：succeeded、failed、expired、canceled、deduped。终态不再迁移。
- 每次转换立即落库（含 attempts、时间戳、change_seq 递增）；`pending → running` 必须在设备操作发出之前完成落盘。running 是唯一"断电时可能悬置"的非终态，由恢复对账处置（第 9 节）。

### 5.4 效果登记与补偿

- 动作类型可声明"成功后产生持续效果"（如录像进行中）。效果登记项状态机：`registered → compensating → compensated | compensation_failed`。
- 复合动作内部维护登记（start 相位成功 → 登记；stop 相位成功 → 注销）。登记表持久化，断电后可恢复"设备侧现在挂着什么效果"。
- 补偿的锚点是登记项而非动作：任何取消路径（计划级、组级、单动作级）触达同一登记项时，只有第一个把它推进到 `compensating` 的路径执行补偿，后到路径不重复触发——一个效果恰好一次补偿。
- 补偿操作本身按无时效动作策略执行（重试、断电升级）；最终失败 → `compensation_failed` + 原因，如实进报告。

### 5.5 计划退役

计划实例退役 = 其全部动作实例进入终态。队列中所有计划退役且无新入队项 → 会话进入收尾（第 6 节）。

## 6. 会话时序

### 6.1 run 会话

```
1. flock 会话锁        被占 → error(12) 退出
2. 启动时钟校验         异常（含延迟复检后仍异常）→ error(13) 退出（第 8 节）
3. 打开/校验状态库      不可用 → error(14) 退出
4. 提交本次计划（若带 plan-path）：校验(4.4) → 注册 → 动作展开去重入库 → 记录 ACK
5. 断电恢复扫描         涉及设备 preflight → running 态动作对账（第 9 节）
6. 孤儿批次清理         按 batches 登记清理未上 stdout 的批次
7. 开局报告            gap 检测 → 增量或合并报告 → 批次 → batch_path 消息（无新内容则跳过）
8. 驱动队列            调度执行直至全部计划退役；期间：
                       ├─ 轮询 queue_version，新入队计划即时纳入调度
                       ├─ obtain_action_outputs 完成 → 批次 → batch_path 消息
                       ├─ report_status 执行 → 即时触发一轮"报告 → 批次 → batch_path"
                       └─ cancel_task 执行 → 按第 11 节决策表生效
9. 收尾报告            同步骤 7
10. 裸 succeeded 消息 → exit 0
```

### 6.2 submit 快速路径

```
1. 打开/校验状态库      不可用 → error(14)
2. 校验计划(4.4)        违规 → error(11)
3. 注册计划、动作展开去重入库、记录 ACK（单事务，fsync）
4. 读会话锁状态：空闲 → body 带 needs_run:true；被占 → 不带（活跃会话经轮询接管执行）
5. succeeded 消息 → exit 0
```

submit 不生成报告、不生成批次、不触碰设备，秒级退出。ACK 的 gap 补救只住在会话里，由活跃会话（或下一次 run）在报告点处理。

## 7. 调度与时间语义

- 动作按绝对墙钟时间触发，触发误差目标 ≤100ms。
- 排程时做单调钟换算：`mono_target = time.monotonic() + (wall_deadline − wall_now)`；等待与超时基于 monotonic（asyncio 定时器天然基于单调钟），墙钟在等待期间被调整不影响等待时长。
- deadline 相同时，计划 seq 小者优先；同一设备的操作由设备 actor 串行化（见 13.1）。
- **有效窗口**：时效动作的 `max_delay_ms`（类型默认 + 可覆盖）定义 deadline 之后的可执行窗口。窗口内失败可重试；窗口耗尽时：未曾成功执行 → `expired`；执行过但失败 → `failed`（附各次尝试记录）。两种终态都如实呈现，不补偿、不追拍。
- 无时效动作没有窗口概念：按重试策略努力执行，直至成功或按策略耗尽（含断电升级后仍不可达 → `failed(device_unreachable)`）。

## 8. 时钟防御

前提：C 在调用前同步系统时钟；camctl 正常使用系统时间，但不预设其可靠。

**启动校验**（run 与 submit 入口，处理队列之前）：

| 检查 | 判定 |
|---|---|
| 时间下界 | 当前墙钟 < 状态库最后一条事件的墙钟时间戳 − 容差（默认 5s，容纳正常 NTP 微调）→ 异常 |
| 合理性下界 | 当前墙钟 < 配置的最小可信日期（默认 2025-01-01）→ 异常（捕捉 RTC 复位类症状） |

**延迟复检**：任一检查失败 → 延迟（默认 5s）→ 重新读取并复检；复检通过 → 继续启动（events 如实记录"首检失败、复检通过"及双读数）；仍失败 → `error(13)` 退出，不碰队列。延迟时长与复检次数可配置。该机制覆盖"C 只发起了同步请求、同步尚未完成"的窗口。

**已知边界（v1 明确不做）**：

- 运行中墙钟跳变不监测、不处置（评估为当前阶段的过度防御，后续需要再议）；
- "一致地错"的时钟（同步到了错误时间源且此后稳定）在无外部时间基准时原理上不可判定，不检测。

## 9. 断电恢复与对账

宿主断电 = camctl 任意时刻猝死。恢复依靠：状态库中已 fsync 的记录 + 设备独立供电仍在线的事实。

### 9.1 preflight（设备前置可用性检查）

- 会话启动的恢复扫描之前，对涉及设备统一做一轮 preflight：协议级可用性检查，失败按退避重试；重试次数耗尽仍不可达 → 断电重启（重启耗时须在设备配置的提前量预算内）→ 再检查；仍不可达 → 依赖该设备的恢复项按"无法判定"路径处理。
- 正常运行期间，每个时效动作的 deadline 前一个提前量（设备级配置，默认 60s）触发该设备 preflight，最大化到点可用概率；到点仍不可达 → 时效动作按窗口语义如实记录（多为 expired）。无时效动作执行前同样先确认可用，不可达走自身策略。

### 9.2 running 态动作的对账

发现 `running` 态动作（上次会话断电时悬置），必须先向目标设备对账，再决策：

```
running（断电打断）
  → 1. 设备可达（preflight，含断电重启升级）
  → 2. 对账钩子：验证动作效果在设备上是否已发生
  → 3. 按下表决策
```

| 对账结果 | 时效性 | 处理 |
|---|---|---|
| 效果已发生 | 任意 | → succeeded；含本地落地语义的动作（如 mcu_read_data 写 dest_path、obtain_action_outputs 写 outbox）若产物未落地 → 补落地（无时效，可重试） |
| 效果未发生 | 有时效，窗口内 | 重新执行 |
| 效果未发生 | 有时效，超窗口 | → expired，记录对账证据 |
| 效果未发生 | 无时效 | 回 pending 立即执行 |
| 无法判定（驱动无对账钩子 / 设备经重启仍不可达） | 任意 | → failed，reason=`unreconciled`，如实记录"结局无法证明"，不折叠进成功或过期 |

对账钩子是动作类型接口的一部分（每种动作声明"效果验证方法"），依赖驱动的 `status_query` 能力；协议未定案期间相机驱动无对账钩子，走 `unreconciled` 路径。

### 9.3 pending 态动作

`pending` 且 deadline 在宕机期间已过：从未派发，设备上不应有其效果，不经对账，直接按窗口逻辑（窗口内 → 立即执行；超窗 → expired）。未到 deadline 的照常调度。

### 9.4 报告与批次的至少一次恢复

- 动作/计划的 `reported` 标记在报告批次成功输出到 stdout（flush 完成）之后才落盘；flush 与标记之间断电 → 下次报告重复包含同批内容（快照语义天然幂等，调用方按 report_id 与实例 id 幂等消费）。
- 孤儿批次按 batches 登记在会话启动时清理（3.4）。

## 10. 失败处理与重试策略（类型默认）

| 类别 | 默认策略 |
|---|---|
| 有时效动作 | 窗口 `max_delay_ms` 默认 5000ms；窗口内失败即重试（每次尝试受 `timeout_ms` 约束）；超窗 → expired/failed（见第 7 节）；不升级断电重启（重启耗时远超窗口，无意义） |
| 无时效动作 | 指数退避 1s→30s；连续 5 次失败（`max_attempts` 默认）→ 升级断电重启 → 重启后仍 3 次失败 → failed(device_unreachable) |
| device_power_cycle | 无时效；重试；不递归升级 |
| report_status / cancel_task | 纯内部操作，不失败（状态库故障属致命错误路径） |
| obtain_action_outputs | 见 13.5 |

所有数值可被计划内 `policy` 覆盖（4.1）。每次尝试与结果都记入 events。断电重启是 camctl 的固有权限：由策略自主触发，全程记录，无需计划显式授权。

## 11. 取消语义

`cancel_task` 目标：plan instance id（计划级）/ 动作实例 id（单动作）/ (plan instance id, group)（组级）。计划级与组级取消 = 对范围内全部动作逐个应用下表 + 对范围内登记在册的持续效果逐个补偿。

| 目标动作状态 | 处理 |
|---|---|
| pending | → canceled |
| running | 标记 cancel_requested：当前设备操作物理上不可中止（指令已发出），actor 完成本次尝试后不再重试 → canceled；本次尝试的实际结果如实记入 events |
| 已终态（succeeded / failed / expired / canceled / deduped） | 取消不生效，cancel_task 结果中如实报告目标实际状态 |
| 该范围登记在册的持续效果 | 执行补偿钩子（5.4：恰好一次；按无时效策略重试/升级；结果如实记录） |

- 复合动作（camera_record）running 中被取消 → on_cancel 钩子 = 立即发停止指令 + 放弃本次内容 +（协议支持时）清理设备上废弃产物。
- 取消 stop 相位等价于让效果失去收场动作 → 触发同一登记项的补偿。
- `cancel_task` 本身的执行结果（逐项生效/不生效/补偿结果）进入报告。

## 12. 报告与 ACK 水位线

### 12.1 报告链

- 报告是文件（JSON），经批次机制传输给调用方（3.4）；stdout 不承载报告内容。
- 每份报告：`report_id`（单调递增）、覆盖区间 `(from_wm, to_wm]`（wm = change_seq 水位线；应报告的状态变化——计划注册、动作终态、计划退役、效果补偿结果——落库时递增）。
- 报告内容 = 区间内发生变更的实体的**当前状态快照**，按计划组织（计划之下按组聚合）：新注册计划（含分配的实例 id）、新终态动作（状态、attempts、时间戳、结果/错误、对账与补偿记录）、新退役计划、涉及设备的协议特征集。快照语义天然幂等：区间重叠或重复收到均可安全重放。

### 12.2 生成时机

会话内三处（内容一致，均为"自水位线以来的增量"）：

1. 开局报告（恢复扫描之后——报告内容必须是对账后的真实状态）；
2. `report_status` 动作执行时（即时触发，调用方会话中途要状态就投递它）；
3. 收尾报告（会话完结前）。

无新内容 → 跳过。生成流程：报告文件 → 批次 → `batch_path` 消息 flush 成功 → 标记 reported。

### 12.3 ACK 与漏传补救

- ACK 通道：计划 JSON 的 `last_report_id`（4.1）。入库时（run/submit 皆然）只记录 ACK（更新 reports.acked 水位），不做补救。多份计划各带 ACK 时，以 report_id 最大者为准。
- 会话在报告点统一执行 gap 检测：

```
gap = acked_wm < 最新已发出报告的 from_wm ?
├─ 无 gap → 正常增量：from = 已发出水位
└─ 有 gap → 合并报告：from = acked_wm, to = 当前水位（漏传区间 + 新增变更合并为一份，新 report_id）
抑制规则：同一 (from, to) 的合并报告已发出过且无新变更 → 跳过
```

- 分工边界：报告链有自动 ACK 补救（报告是状态账目，漏传必须自动补齐）；数据产物没有——调用方客户端自己知道收到了哪些文件，漏传时主动投递 `obtain_action_outputs` 重传（回退重拷语义保证可用）。

## 13. 动作框架与初始动作集

### 13.1 三层解耦

```
动作类型 (ActionType)     计划语义层：调用方在计划里写什么
    │ 声明所需能力 (capability)
    ▼
能力接口 (Capability)     契约层：拍照 / 开始录像 / 事务通信…
    │ 由驱动实现
    ▼
设备驱动 (Driver)         协议层：每种设备型号/协议一个实现（含模拟器实现）
```

- **ActionType 注册表**（代码内注册，非动态插件）：每个类型声明——类型名、目标设备种类、所需能力、参数 schema、默认策略（时效性/窗口/重试/断电升级）、execute 实现、对账钩子（可缺省）、持续效果声明 + 补偿钩子（可缺省）。新增动作 = 新增注册项，框架零改动。
- **驱动声明能力集**：设备配置将 device_id 绑定到驱动；驱动注册时声明其实现的能力集。入库校验据此快速失败（4.4）。
- **设备 actor**：每台设备一个 actor 任务，串行化该设备全部操作，独占传输通道，持有连接、负责超时与重连；不同设备的动作天然并行。
- **模拟器是同能力接口的另一个驱动实现**，与真机驱动受同一契约约束；协议未定案期间框架开发与测试全部跑在模拟器上。

### 13.2 初始注册表（8 类型）

| type | 设备 | 所需能力 | 时效性 | 效果/补偿 | 对账 |
|---|---|---|---|---|---|
| `camera_capture` | camera | `still_capture` | 有时效 | 无补偿（已拍照片无害，留在设备） | 依赖 `status_query`/`list_artifacts`；缺省 → unreconciled |
| `camera_record` | camera | `record_start` + `record_stop` | 有时效（两相位各自窗口） | 复合动作，见 13.3 | 依赖 `status_query`；缺省 → unreconciled |
| `mcu_send_command` | mcu | `mcu.transact` | 有时效 | 无 | MCU 协议按第 13.7 节要求实现，`status_query` 可用 |
| `mcu_read_data` | mcu | `mcu.transact` | 无时效 | 无 | 同上；响应载荷写 dest_path |
| `device_power_cycle` | 任意 | power 后端 | 无时效 | 无 | 对账 = 设备恢复可达 |
| `report_status` | —（内部） | — | 无时效，可缺省 scheduled_at（立即） | — | — |
| `cancel_task` | —（内部） | — | 无时效，可缺省 scheduled_at（立即） | — | — |
| `obtain_action_outputs` | —（内部+驱动能力） | 引用相机产物时依赖 `artifact_fetch` | 无时效 | — | — |

预留能力名（协议定案后按注册机制增量加入类型）：`query_status`、`list_artifacts`、`fetch_artifact`、连拍（设备原生不支持时以复合动作 `camera_burst` 实现）。裸 `camera_record_start` / `camera_record_stop` 不进注册表，作为复合动作的内部设备操作存在；未来确需调用方手动配对时按注册机制加回。

### 13.3 复合动作

- 对调用方暴露单一动作（如 `camera_record`，参数 `duration_s`）：`scheduled_at` 时刻发开始指令，`scheduled_at + duration_s` 时刻发结束指令；底层是两条串行设备指令（相机无复合指令的现实封装在执行层，不外泄）。
- 状态映射：`pending → running（整个录像期间）→ succeeded/failed`；running 态跨越效果持续期。两相位各自是时效动作（各有窗口），stop 相位窗口语义 = 到点必须停。
- 效果登记由复合动作内部维护（start 相位成功登记、stop 相位成功注销）；取消 running 中复合动作 → on_cancel 补偿钩子（第 11 节）。
- 断电恢复：running 态复合动作按 9.2 对账（查询设备侧效果状态）。

### 13.4 MCU 动作

UART 为主从 request-response 模型：不存在"被动读取"，读数据 = 发查询指令 + 接收响应帧。能力 `mcu.transact`（发帧 → 等回帧，带超时）由两个动作类型共享：

- `mcu_send_command`：期待 ack；params 含指令载荷（指令集定案前 schema 开放，由驱动校验）；
- `mcu_read_data`：期待数据帧；params 含查询指令 + `dest_path`（响应载荷落盘）。

拆两个类型的理由：默认策略截然不同（时效 vs 努力重试），计划可读性好。指令集定案后可注册更具体的类型作为薄封装。

### 13.5 obtain_action_outputs

- 单一幂等类型：首次调用 = 首传，重复调用 = 重传。语义 = 确保被引用的动作产物在批次中（不在 outbox 暂存则从源结果重拷）→ 生成批次 → `batch_path` 消息。
- `refs`：动作实例 id 列表，或 `(plan_instance_id, group)` 组引用；两者可混用。
- **调用方保证编排顺序**：执行时被引用动作的结果未就绪 → 立即 `failed(reference_not_ready)`，不等待不重试；事后可投递新的 obtain_action_outputs 补救。
- 被引用动作 failed/expired（无结果可传）→ 结果中逐引用如实记录 `missing`。
- 引用相机侧产物时依赖驱动 `artifact_fetch` 能力；能力缺失的行为归入降级建模专题（19 节）。

### 13.6 设备协议要求清单

框架正常运转对设备协议的要求（MCU 固件自研，按全项实现；相机侧以厂商实际给出的协议为准，缺项按降级路径处理）：

1. 每指令必有应答（ack / data / error）+ 可定义超时——否则无法区分"慢"和"死"；
2. 指令携带唯一 id，设备端幂等去重——窗口内重试、断电后重发不双重执行；
3. 指令执行状态可查询——断电对账的基础；
4. 录像类：状态可查询、可中止、废弃产物可清理——补偿钩子的基础；
5. 产物管理：确定性命名、可列表、可拉取——产物取回与识别；
6. 重启行为明确：断电重启后设备状态（录像中断？产物保留？指令队列清空？）——断电升级与对账语义依赖。

协议缺失某项时，对应机制降级为如实记录（unreconciled / no_ack / at-most-once），框架不塌、保障等级下降。降级建模的完整规则是后置专题（19 节）。

## 14. 配置

- 配置文件为**可选项**：无配置文件时全部使用内置默认值。默认路径 `$HOME/.camctl/config.toml`，`--config` 覆盖。TOML 格式（Python 3.11 `tomllib` 标准库）。
- 内置默认值：`state_db = $HOME/.camctl/state.db`、`outbox = $HOME/.camctl/outbox`、`lock = $HOME/.camctl/run.lock`、日志级别 WARNING、轮询间隔 500ms、时钟参数（容差 5s / 最小可信日期 2025-01-01 / 复检延迟 5s / 复检 1 次）、preflight 提前量 60s。
- 配置分节：

```toml
[paths]        # state_db、outbox、lock、log_file（可选）
[clock]        # min_plausible_date、lower_bound_tolerance_s、recheck_delay_s、recheck_count
[session]      # poll_interval_ms、log_level
[devices.<device_id>]
    kind       = "camera" | "mcu"
    driver     = "simulator" | "adb_<型号>" | "serial_<版本>" …
    conn       = { … }   # adb serial / tty 路径+波特率 / 模拟器行为参数
    preflight  = { lead_s = 60, attempts = … }
    power      = { backend = "simulator" | …, params = { … }, reboot_budget_s = … }
```

- 配置文件非法（语法错误、未知键、类型不符）→ 启动即 error + 退出码 10，不猜、不半用。

## 15. 日志

- stderr：行式日志（时间戳、级别、组件），级别可配，默认 WARNING 以上。
- 可选日志文件（RotatingFileHandler），默认关闭；用于断电事后诊断的非业务细节（驱动内部、异常栈）。业务审计的权威是 events 表，日志文件只是补充。

## 16. 部署与打包

- 构建：uv + hatchling 出 wheel（console_script 入口 `camctl`）。
- 部署：camctl wheel + pyserial wheel 离线拷贝上板，`pip install --no-index --find-links <dir>` 装入板上 venv（或 uv 建 venv）。无编译步骤。
- adb 二进制由环境提供，路径与调用参数入设备配置。
- 开发环境：devcontainer（已就绪），uv 管理依赖。

## 17. 项目结构

```
src/camctl/
  cli.py           # argv 解析、装配、退出码
  config.py        # 配置加载与校验（含内置默认值）
  protocol.py      # stdout 信封（succeeded/error）、NDJSON 写入器、批次目录生成、schema 注册表
  clock.py         # 启动时钟校验（含延迟复检）
  orchestrator.py  # 会话时序（6.1/6.2）
  scheduler.py     # deadline 排程（monotonic 换算）、窗口与过期判定
  state/           # StateStore：sqlite schema、水位线、去重、效果登记、批次登记
  actions/         # ActionType 注册表 + 每类型一模块（含复合动作生命周期）
  devices/
    camera/        # 能力接口、adb 驱动（协议定案后）、模拟器
    mcu/           # transact 能力、serial 驱动（协议定案后）、模拟器
    power/         # 断电重启抽象、模拟器
  reports/         # 增量报告生成、ACK 水位线、gap 合并
tests/
  unit/            # 进程内、无真实 FS/网络/子进程
  integration/     # 真实协作者、子进程、临时目录，独立执行
```

## 18. 测试策略

纪律依据仓库 CLAUDE.md：单元/集成顶层分类并分开执行；偏模拟派；替身受真实接口约束；测试从行为契约独立推导；机器协议精确断言、人类文案只断言结构与事实呈现。

### 18.1 单元测试（进程内、确定性、注入时间与 IO）

| 被测单元 | 覆盖契约 |
|---|---|
| 计划校验 | 4.4 逐违规类型 |
| 缺省命名生成 | plan-{created_at}、{plan_name}-action{NO.} |
| 动作身份哈希与去重 | 5.2 三行决策表 |
| 策略解析 | 类型默认 + 覆盖，入队固化 |
| 时钟校验 | 下界、合理性、延迟复检（注入假时钟序列：首检失败复检通过 / 双失败） |
| 调度换算 | deadline → monotonic、窗口与过期判定 |
| ACK 水位线与 gap 判定 | 增量 vs 合并的 from/to 计算、抑制规则 |
| 报告组装 | 水位线区间快照、按计划/组聚合、幂等重放安全 |
| 取消决策 | 第 11 节表逐行；补偿恰好一次（登记项状态机） |
| 复合动作相位 | camera_record 两相生命周期、stop 相位窗口、running 中取消 → on_cancel |
| stdout 信封与退出码 | protocol.py 纯函数：kind/body 合法组合、码表映射 |
| manifest.json 内容计算 | 条目结构、sha256（内存字节，不落盘） |

### 18.2 集成测试（真实协作者，独立命令执行）

| 场景 | 验证组合 |
|---|---|
| 全模拟器会话 | 6.1 完整时序；stdout NDJSON 逐行、退出码、批次目录内容精确断言 |
| submit 快速路径 | 受理、needs_run 语义、秒级退出、不产生批次 |
| 锁竞争 | 两个真实进程：run 撞锁 → 码 12 + error |
| 断电演练 | 子进程跑会话，模拟器钩子在选定点 SIGKILL；重启验证：WAL 恢复、running 对账、开局报告含遗留账目、at-least-once 重复与幂等 |
| 孤儿批次清理 | 批次落盘后、stdout flush 前杀死 → 重启清理 |
| 状态库耐久性 | 真实 sqlite 文件 + 进程猝死，已提交事务零丢失 |
| serial 传输 | pty 对（socat）上 serial 驱动 ↔ 模拟 MCU 端：transact 帧收发与超时 |
| CLI 契约矩阵 | 真实子进程视角：argv 组合 × stdout × 退出码 |

### 18.3 工具链

- pytest；`tests/unit` 与 `tests/integration` 分离，集成单独命令执行。
- ruff（lint + format）；pyright（静态类型检查，核心模块 strict）；类型标注按 3.11 基线全覆盖。
- 断言纪律：退出码、kind、body schema、manifest 字段、报告结构 = 机器协议，在最接近输出的一层精确断言一次，不跨层重复。

### 18.4 已知测试缺口

- adb 真机驱动与相机协议对账/补偿的真实组合测试，待协议定案后补；v1 期间模拟器是唯一被集成验证的相机驱动。
- serial 的 pty 集成测试验证传输层，不是未来真实 MCU 协议；协议定案后补协议层测试。

## 19. 已知边界与后置专题

已知边界（v1 明确接受）：

1. 运行中墙钟跳变不监测（8 节）；
2. "一致地错"的时钟不可检测（8 节）；
3. 相机协议未定案期间：相机动作无对账钩子（断电恢复走 unreconciled）、无真机驱动，仅模拟器；
4. 提交 → 实例 id 可引用之间存在一个报告往返延迟（4.2）；
5. 状态库损毁重建后 ACK 链断裂：计划被校验拒绝，属运维级灾难场景，不做自动恢复（4.4 / 12.3）。

后置专题（设计文档之外单独立项讨论）：

1. **相机协议降级建模**：驱动声明协议特征集（acked_commands、command_dedup、status_query、record_abort、artifact_list/fetch、cleanup），框架按特征集自动调整默认策略与保障等级（如缺 command_dedup → 时效动作降为 at-most-once），保障等级随报告输出；
2. **查询/取文件类动作**（query_status、list_artifacts、fetch_artifact）：协议定案后注册；
3. **MCU 指令集**：定案后落驱动 schema 与具体动作薄封装；
4. **断电重启硬件链路**：power 后端真机实现（当前仅抽象接口 + 模拟器）；
5. **连拍**（camera_burst）：设备原生不支持时以复合动作实现。
