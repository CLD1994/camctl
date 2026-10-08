# apps/camctl/tests 运行指南

本目录包含 camctl 的单元测试（`unit/`）和组件集成测试（`integration/`，按业务模块分目录）。仓库根的 `tests/integration/` 是另一层：跨组件集成测试（真实 CLI 进程、客户端与 C 主程序的组合），与本目录分开维护。本文件只覆盖本目录；跑跨组件测试请到仓库根并阅读 `tests/integration/README.md`。

## 怎么跑

所有命令都从仓库根目录执行，用 uv 管理的 Python 3.11（部署规格版本）：

```bash
# 单元测试（约 10 秒）
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit -q

# 组件集成测试：按目录逐个跑，不要合并成一条命令
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/reporting -q
```

集成测试目录清单：acceptance、bootstrap、cancellation、capture、contracts、devices、history、host_files、logging_runtime、motor、operations、outputs、persistence、reporting、scheduling、session。

不要用系统 Python 直接跑：系统解释器缺少测试依赖（如 `referencing`），SQLite 版本也不满足运行条件。

## 运行方式的三条硬规则

**1. 集成测试分目录逐个跑。** 把多个集成目录合并进一次 pytest 进程会在 bootstrap 会话测试段停滞：曾经 capture+scheduling+bootstrap 合并跑 90 秒推进到 69% 后基本不再前进，而各目录单独跑分别只需几秒到半分钟。原因是同一 pytest 进程里跨模块的注册表身份会互相污染。全量回归的做法是用循环逐目录执行。

**2. 前台独占、顺序执行，不要并行。** 并行多个 pytest 后台作业会把长目录拖挂（acceptance 曾卡在 63% 十五分钟无输出，单独顺序重跑 41 秒全过）；后台跑单元测试同样会拖挂，且结束后状态不恢复。一次只跑一个 pytest 进程，等它结束再起下一个。

**3. 排查挂起时把输出重定向到文件。** `pytest … | tail` 这样的管道会缓冲到 pytest 完全结束才有输出，期间终端看起来像卡死，实际可能还在跑。排查时用 `pytest … > /tmp/run.log 2>&1` 直写文件再查看，才能看到实际停在哪个用例。

## 时长预期

| 目录 | 大约耗时 | 说明 |
|---|---|---|
| unit | 10 秒 | 全部在进程内，无真实 IO |
| outputs | 2 分钟 | 文件 IO 量最大 |
| host_files | 1 分钟 | 真实文件操作 |
| bootstrap | 1.5—2 分钟 | 含真实进程会话 |
| acceptance | 40 秒 | |
| 其余目录 | 数秒到半分钟 | |

全量集成（所有目录合计）约 4—5 分钟。如果某个目录远超上表耗时且无输出，先按"管道缓冲"排查，再怀疑拖挂。

## 已知的环境漂移：怎么认、怎么处理

这台 Windows 开发机上，单元全量或集成长跑时偶尔出现**随机位置的 setup 错误**（典型是 `WinError 10055` 系统套接字缓冲区不足，表现为 asyncio 事件循环创建失败、`TestRealWorkerProcess` setup 报错等）。这是本机瞬时资源压力，不是代码回归。

认定标准（三条都满足才算环境漂移）：

1. 失败用例的**位置每轮都不一样**（这轮这批、下轮另一批）；
2. 涉事用例**单独跑能通过**；
3. 必要时把改动 stash 掉再跑，**旧代码同样失败**（证明与本次改动无关）。

处理方式：等待资源回落再复跑（`netstat -an | grep -c TIME_WAIT` 归零、无残留 python 进程是好信号），必要时先清理残留进程。验收依据是**连续两轮全绿**或清进程后全绿，单轮全绿不作数。

反过来，如果失败用例**稳定复现在同一批**，那就不是环境问题——单独跑它，看真实报错。

## 写测试时容易踩的点

- **集成测试的守卫要显式注册。** 事件守卫（`register_*_guards()`）不经 import 链自动触发。新文件按所在目录的惯例补齐：outputs 侧常见 `register_operation_guards()` + `register_capture_guards()`；capture 侧常见 `register_outputs_guards()`。漏注册时写入类用例会以"未接入具名校验"失败。参考同目录既有测试文件的开头。
- **单元测试不启动真实线程或子进程**，不访问真实文件系统；线程、事件循环与进程组合放集成测试。
- **测试目录带 `__init__.py`**，同目录模块（如 fixtures）靠 pytest 的 prepend 导入。
- **替身受真实接口约束**（stub/fake/mock 按真实协作方的契约编写），这与根 AGENTS.md 的测试规范一致。
- 个别已知限制：`persistence/test_transactions.py` 的 window 守卫断言依赖单目录运行，与其他目录组合 import 时会被污染——这也是"分目录跑"的原因之一。

## 环境备忘

- 包资源由 `scripts/sync_resources.py` 生成（`integration/conftest.py` 在 pytest 启动时自动执行），`src/camctl/_resources/` 不入库。
- 会话锁在 Windows 上用 msvcrt 字节范围锁模拟目标部署的 flock（双后端同一契约）；目标机联调时需复核。
- 涉及 C 主程序（host-demo）的组合测试在仓库根 `tests/integration/`，POSIX 模块需按部署验证裁决在 WSL 构建。
