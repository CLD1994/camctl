# 公共机器协议

`schemas/` 保存机器可读协议的权威定义；`examples/` 保存能力说明、计划、报告和部署样例。客户端、CLI 及跨组件测试从这里读取或生成所需资源。

语义说明见[全局设计](../docs/architecture/README.md)和[报告格式](../docs/architecture/report-format.md)。报告样例的文件名含原始字节摘要，修改报告时必须同步生成准确文件名及引用。

## 协议定义与校验入口

以下文件定义计划输入、设备能力、状态报告及相关错误的公共机器表示。各组件必须按同一协议读写数据；组件接入与集成验收要求见[跨组件协议接入任务](../docs/superpowers/plans/2026-09-30-camctl-integration.md#i3-客户端请求身份时间能力及报告消费)。

| 文件 | 定义内容 |
| --- | --- |
| [执行计划 Schema](schemas/plan.schema.json) | 完整合法计划、分阶段受理用的公共结构、各动作参数及来源组合 |
| [能力说明 Schema](schemas/capabilities.schema.json) | 设备、拍摄动作及参数类型结构，明确的预览支持声明 |
| [对象 ID 定义](schemas/status-report.schema.json#/$defs/entity_id) | 数据库对象整数身份的规范十进制字符串，范围 1～9223372036854775807；单项、数组、报告 ACK 及同步起点共用 |
| [状态报告 Schema](schemas/status-report.schema.json) | 报告身份与覆盖水位、计划及动作结果、产物与交付、输入诊断、预览展示关联、逐项取消结果及动作结束后的设备执行情况 |
| [工作流程错误登记](errors/workflow-codes.json) | 预览、产物选择、交付及取消相关错误码、阶段和详情 Schema；未知驱动错误仍按公共错误结构保留 |
| [共享样例与校验边界](examples/workflows/README.md) | 正常、失败、等待及取消场景，以及 Schema 校验和业务语义检查的区别 |

字段含义分别在[计划输入](../docs/architecture/plan-input.md)、[能力说明](../docs/architecture/capabilities.md)和[报告格式](../docs/architecture/report-format.md)维护。预览及范围选择可直接查阅[输入组合](../docs/architecture/plan-input.md#取回来源筛选与预览)、[预览支持声明](../docs/architecture/capabilities.md#参数类型的预览支持)及[报告关联](../docs/architecture/report-format.md#预览与范围选择的报告契约)。基本值与动作类型复用状态报告 Schema 的公共定义，其他 Schema 通过本地引用使用；加载器须按文件名注册本目录资源，不依赖网络取回。

执行计划根 Schema 用于校验完整合法计划，不能直接充当主机的整份拒绝条件。主机先无歧义解析，再查询幂等关联；新请求使用 `plan_structure` 校验公共结构，逐动作使用 `action` 校验，ACK 独立处理。名称唯一性、实际日期、引用归属、重复自动关联、能力匹配和取消目标包含自身等业务判断，仍按语义契约完成。

运行 `node scripts/check-protocol.mjs` 校验 Schema、共享样例、报告摘要及已登记错误详情。脚本使用客户端已锁定的 Ajv；文件检查属于规格验证，不代替真实软件组件的集成测试。软件集成测试使用受接口契约约束的设备替身，真实设备联调另行安排。

## 组件接入与验收

报告 Schema、协议样例和字段说明定义第一版的目标格式。客户端生成类型、导入校验、合并和展示的任务及进度见[客户端适配计划](../docs/superpowers/plans/2026-09-30-report-client-adaptation.md)。协议规格检查与客户端兼容性验收分别执行。camctl 的字段依赖登记、历史生成和跨组件一致性仍按[历史与报告验收](../docs/camctl/database/consistency-verification.md#独立历史与报告重建)落实。
