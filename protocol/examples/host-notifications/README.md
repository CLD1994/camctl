# 电机通知共同夹具

[通知协议](../../host-notifications.md) · [电机动作](../../../docs/architecture/motor-control.md)

`cases.json` 是 Python、客户端和 C 的共同输入来源。每个条目的 `json` 保存完整原始 JSON 字符串，消费者须解析该字符串，不能先把数字解析为二进制浮点再判断。数组中的分类、预期值和规范字节都是手工确定的行为预期。

| 字段 | 含义 |
| --- | --- |
| `name` | 唯一的用例名称，供失败诊断关联 |
| `kind` | `notification` 校验一条通知内容；`plan` 校验完整计划及受理层次；`report` 校验状态报告的动作片段 |
| `json` | 原始 JSON 文本，保留整数、小数、指数及超出浮点精度的小数部分 |
| `valid` | 解析和对应 Schema 及精确数值规则共同确定的合法性；合法 JSON 不自动意味着合法消息 |
| `admission` | 仅计划条目提供；`accepted` 表示结构合法，`action_failed` 表示计划结构仍可受理但该动作失败，`plan_rejected` 表示整份计划结构拒绝 |
| `position` | 合法通知位置的规范十进制整数字符串，可无损交给 C `int` |
| `wire` | 合法通知的规范发送字节文本，UTF-8 编码并以 LF 结束；它与 `json` 可采用不同数字字面量，但必须表示同一位置 |

接收器只消费 `kind = notification` 的条目。合法输入 `1.0`、`1e2`、长尾零小数和精确抵消的指数均按数学值接收；CLI 对这些值输出规范十进制整数。`rounded-fraction` 和边界附近的长小数条目用于证明消费者未把浮点舍入当作整数或范围判断。`duplicate-position` 用于验证解析层，不属于单独的 Schema 断言。

`plan.json` 是可直接导入的合法计划。计划条目通过 `plan_structure` 和 `action` 分层验证；直接使用计划根 Schema 的失败不能替代受理分类。`report-admission-raw-input` 表达受理失败报告可保留原始非法参数、策略与设备字段；有效电机报告必须提供 `scheduled_at`、`policy`、`input_params`，并省略设备执行、产物、交付和额外成功结果。

`node scripts/check-protocol.mjs` 验证所有条目。检查器独立的反例测试通过 `node --test scripts/check-protocol.test.mjs` 执行。Python 使用生产包资源与本地引用注册表运行 `apps/camctl/tests/integration/contracts/test_motor_schema.py`。这些是机器契约证据，不证明通知已经发出、host 已经回调或电机已经到位。
